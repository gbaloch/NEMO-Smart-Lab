"""
Pulls a tool's raw data down from a remote, SSH-reachable file server - configured as a
NEMO_smart_lab.models.RemoteSyncEndpoint - into a local directory, so a SmartLabTool's
local_root can be a mirror kept up to date by `sync_remote_data` instead of requiring a live
network share mount.

Deliberately not tied to any one storage provider: the "SSH keypair against one host, one base
directory per tool underneath it" pattern this implements is common to Stanford's Oak data
transfer node (https://docs.oak.stanford.edu/gateways/) and to a plain departmental Linux
fileserver alike. All the connection specifics (host/port/user/key/base path/extra SSH options)
live on the RemoteSyncEndpoint row, in the admin - nothing here or in settings.py is specific to
any one endpoint.

Password/2FA (e.g. Duo) login is not supported: an unattended sync job can't answer an
interactive prompt, so this always authenticates with the endpoint's registered SSH private key
(`-o BatchMode=yes` - fails immediately instead of ever prompting). Getting a key registered for
public-key login on the remote host is a step the site has to do out of band (for Oak: ask
Stanford Research Computing - see "Public Key Authentication on the Oak DTN", linked from the
page above).
"""

import logging
import os
import shlex
import shutil
import subprocess
import threading

logger = logging.getLogger(__name__)

# Every rsync/scp invocation funnels through _run() below, regardless of which caller spawned it
# (NEMO_smart_lab.remote_cache.ensure_cached_many's own thread pool, the dashboard's per-tool
# concurrent fetch, several browser tabs, ...) - all of them share ONE multiplexed SSH connection
# per (user, host, port) (see _DEFAULT_MULTIPLEX_OPTIONS below), since every configured tool this
# session points at the same Oak endpoint. Confirmed the hard way: enough concurrent page loads
# overlapping pushed the combined concurrent channel count on that one connection past the SSH
# server's own per-connection session cap (commonly 10, sshd's MaxSessions default) - everything
# past that queues up behind the connection instead of failing outright, which looks exactly like
# "the page is stuck loading" rather than a clean error. This semaphore caps how many rsync/scp
# subprocesses this whole process will ever have running at once, well under a typical MaxSessions,
# regardless of how many independent callers/thread pools are trying to fetch at the same time -
# excess callers simply wait their turn instead of piling up new SSH channel requests.
_CONCURRENT_TRANSFER_LIMIT = threading.Semaphore(6)

# SSH connection multiplexing, on by default: NEMO_smart_lab.remote_cache fetches many small
# files/listings on demand (one run at a time, one history page's worth at a time, ...) rather
# than one big periodic sync - without this, each fetch pays a full new SSH handshake (measured at
# ~1.5s against Oak), which multiplies badly across even a single history page (25 runs x 1.5s
# would be well over the 30s a request should ever take). This reuses one already-authenticated
# connection per (user, host, port) instead - the RemoteSyncEndpoint model's own docstring already
# suggested exactly this ("ControlPath ... plus ControlPersist yes") as something to set manually
# via extra_ssh_options; the lazy on-demand caching pattern makes it load-bearing rather than a
# nice-to-have, so it's a built-in default now. Any of these three keys the admin has set
# explicitly via extra_ssh_options is left alone - a deliberate custom Control* setup always wins.
_DEFAULT_MULTIPLEX_OPTIONS = {
    "ControlMaster": "auto",
    "ControlPersist": "300",
    "ControlPath": os.path.join(os.path.expanduser("~"), ".ssh", "smart_lab_cm_%r@%h:%p"),
}


class RemoteSyncError(Exception):
    """Raised when a sync from a RemoteSyncEndpoint can't be attempted, or fails."""


def _ssh_option_args(endpoint):
    explicit_pairs = endpoint.extra_ssh_option_pairs()
    explicit_keys = {key for key, _value in explicit_pairs}
    args = []
    for key, value in _DEFAULT_MULTIPLEX_OPTIONS.items():
        if key not in explicit_keys:
            args += ["-o", f"{key}={value}"]
    if "ControlPath" not in explicit_keys:
        os.makedirs(os.path.dirname(_DEFAULT_MULTIPLEX_OPTIONS["ControlPath"]), exist_ok=True)
    for key, value in explicit_pairs:
        args += ["-o", f"{key}={value}"]
    return args


def _remote_spec(endpoint, remote_dir):
    return f"{endpoint.username}@{endpoint.host}:{remote_dir}"


def _local_rsync_path(path):
    """rsync's own CLI grammar treats any ":" in a path argument as a "host:path" remote-shell
    separator - harmless on POSIX, but a plain Windows local path always has one right after the
    drive letter (e.g. "C:/dev/..."), which gets misread as a second remote host, producing
    "the source and destination cannot both be remote" for what is really a purely local
    destination. Cygwin's /cygdrive/<letter>/... convention (documented in cwrsync's own
    README/cwrsync.cmd) sidesteps this. No-op on any path that isn't a Windows drive-letter path
    to begin with (Linux/macOS dev machines, or an already-POSIX-style path) - only rsync's own
    argv needs this; NEMO_smart_lab's own os.path/os.makedirs calls elsewhere keep using the plain
    Windows path unchanged."""
    path = str(path)
    drive, rest = os.path.splitdrive(path)
    if not drive:
        return path
    return f"/cygdrive/{drive[0].lower()}" + rest.replace("\\", "/")


def sync_tool_from_remote(local_root, endpoint, remote_subdir, dry_run=False, timeout=3600):
    """
    Mirrors <endpoint.base_path>/<remote_subdir>/ on the remote host down into local_root
    (created if missing).

    Uses rsync if it's on PATH (incremental - only transfers changed files, safe to run
    frequently e.g. from cron), otherwise falls back to `scp -r` (a full copy of every file on
    every run - rsync isn't installed on Windows by default, and scp has no delta-transfer
    mode). Returns a short human-readable summary string on success.

    Raises RemoteSyncError if the endpoint is missing a username/ssh_key_path, if neither rsync
    nor scp is available, or if the transfer itself fails. `timeout` defaults to a generous 3600s
    for the `sync_remote_data` management command's whole-tool mirrors; NEMO_smart_lab.remote_cache
    passes a much shorter one for its on-demand, in-request-cycle single-run fetches.
    """
    if not (endpoint.username and endpoint.ssh_key_path):
        raise RemoteSyncError(f"Remote sync endpoint '{endpoint.name}' is missing a username or ssh_key_path.")

    remote_dir = f"{endpoint.base_path.rstrip('/')}/{remote_subdir}"
    os.makedirs(local_root, exist_ok=True)

    if shutil.which("rsync"):
        return _sync_with_rsync(local_root, endpoint, remote_dir, dry_run, timeout)
    if shutil.which("scp"):
        return _sync_with_scp(local_root, endpoint, remote_dir, dry_run, timeout)
    raise RemoteSyncError("Neither rsync nor scp were found on PATH - install an OpenSSH client to sync.")


def sync_file_from_remote(local_path, endpoint, remote_relpath, dry_run=False, timeout=30):
    """
    Fetches exactly one file - <endpoint.base_path>/<remote_relpath> - to local_path (parent
    directory created if missing). Unlike sync_tool_from_remote, this is a single file, not
    directory contents, so the trailing-slash "copy contents" convention doesn't apply here - a
    plain file-to-file rsync/scp copy works directly.

    Used by NEMO_smart_lab.remote_cache for on-demand per-file caching (a single heater_log run,
    Jobs.db, CurrentEvents.csv, ...) - see that module for the TTL/freshness logic sitting on top
    of this. `timeout` is short by default since this typically runs inside a web request.
    """
    if not (endpoint.username and endpoint.ssh_key_path):
        raise RemoteSyncError(f"Remote sync endpoint '{endpoint.name}' is missing a username or ssh_key_path.")

    remote_path = f"{endpoint.base_path.rstrip('/')}/{remote_relpath.strip('/')}"
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

    if shutil.which("rsync"):
        cmd = [
            "rsync", "-az", "--partial",
            "-e", _ssh_command_str(endpoint),
            _remote_spec(endpoint, remote_path),
            _local_rsync_path(local_path),
        ]
        if dry_run:
            cmd.insert(1, "--dry-run")
        return _run(cmd, timeout)
    if shutil.which("scp"):
        if dry_run:
            return "scp fallback doesn't support --dry-run (rsync isn't installed) - nothing done."
        cmd = ["scp", "-i", endpoint.ssh_key_path, "-P", str(endpoint.port), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]
        cmd += _ssh_option_args(endpoint)
        cmd += [_remote_spec(endpoint, remote_path), str(local_path)]
        return _run(cmd, timeout)
    raise RemoteSyncError("Neither rsync nor scp were found on PATH - install an OpenSSH client to sync.")


def list_remote(endpoint, remote_relpath, timeout=30):
    """
    Lists <endpoint.base_path>/<remote_relpath>/ via `rsync --list-only` - nothing is transferred,
    this only enumerates what's there. The only way to get a remote directory listing at all when
    the endpoint's SSH account is restricted to the rsync protocol (no arbitrary command execution,
    no raw SFTP) - see NEMO_smart_lab.remote_cache, which parses the raw text this returns.

    Raises RemoteSyncError under the same conditions as sync_tool_from_remote, plus if rsync isn't
    on PATH at all (there is no scp-based fallback for listing - scp has no listing mode).
    """
    if not (endpoint.username and endpoint.ssh_key_path):
        raise RemoteSyncError(f"Remote sync endpoint '{endpoint.name}' is missing a username or ssh_key_path.")
    if not shutil.which("rsync"):
        raise RemoteSyncError("rsync was not found on PATH - directory listing needs rsync (scp has no listing mode).")

    remote_dir = f"{endpoint.base_path.rstrip('/')}/{remote_relpath.strip('/')}"
    cmd = ["rsync", "--list-only", "-e", _ssh_command_str(endpoint), _remote_spec(endpoint, remote_dir.rstrip("/") + "/")]
    return _run(cmd, timeout)


def list_remote_recursive(endpoint, remote_relpath, timeout=60):
    """Like list_remote(), but lists the *entire* tree under <endpoint.base_path>/<remote_relpath>/
    in one round trip (`rsync --list-only -r`), rather than just the immediate directory contents.

    Confirmed live against Oak's restricted rsync-only account: recursion is a client-side rsync
    flag, not a separate remote command, so it works under the same "rsync protocol only, no
    arbitrary ssh" restriction as list_remote(). Used for recipe trees (NEMO_smart_lab.recipes),
    which can nest several folders deep (e.g. a per-user folder containing sub-folders of its own)
    - walking that depth one list_remote() call per directory would multiply round trips badly, the
    same problem already solved for history pages via ensure_cached_many()'s concurrent fetching.

    Returned names are paths relative to remote_relpath (e.g. "Didem/valve three/some_recipe.txt"),
    not just a bare filename - remote_cache.py's listing parser already tolerates embedded "/".
    """
    if not (endpoint.username and endpoint.ssh_key_path):
        raise RemoteSyncError(f"Remote sync endpoint '{endpoint.name}' is missing a username or ssh_key_path.")
    if not shutil.which("rsync"):
        raise RemoteSyncError("rsync was not found on PATH - directory listing needs rsync (scp has no listing mode).")

    remote_dir = f"{endpoint.base_path.rstrip('/')}/{remote_relpath.strip('/')}"
    cmd = ["rsync", "--list-only", "-r", "-e", _ssh_command_str(endpoint), _remote_spec(endpoint, remote_dir.rstrip("/") + "/")]
    return _run(cmd, timeout)


def _ssh_command_str(endpoint):
    """The `-e` value rsync uses to invoke ssh - a single shell-quoted command string."""
    parts = [
        "ssh",
        "-i", endpoint.ssh_key_path,
        "-p", str(endpoint.port),
        "-o", "BatchMode=yes",  # never fall back to a password/2FA prompt an unattended job can't answer
        "-o", "IdentitiesOnly=yes",
    ]
    parts += _ssh_option_args(endpoint)
    return " ".join(shlex.quote(str(p)) for p in parts)


def _sync_with_rsync(local_root, endpoint, remote_dir, dry_run, timeout):
    # Trailing slash on the source means "copy the contents of remote_dir", not "copy remote_dir
    # itself as a subfolder" - local_root is expected to directly contain e.g. "Logfile/", the
    # same as if it were a raw network share mounted straight from the tool PC.
    cmd = [
        "rsync", "-az", "--partial",
        "-e", _ssh_command_str(endpoint),
        _remote_spec(endpoint, remote_dir.rstrip("/") + "/"),
        _local_rsync_path(str(local_root).rstrip("\\/")) + "/",
    ]
    if dry_run:
        cmd.insert(1, "--dry-run")
    return _run(cmd, timeout)


def _sync_with_scp(local_root, endpoint, remote_dir, dry_run, timeout):
    if dry_run:
        return "scp fallback doesn't support --dry-run (rsync isn't installed) - nothing done."
    # No trailing-slash-means-contents trick for scp, so instead ask the *remote* shell to glob
    # the directory's contents (quoted so the local shell/subprocess doesn't try to expand it) -
    # each matched entry then lands directly inside local_root, same layout as the rsync path.
    # Caveat: unlike rsync, this misses dotfiles and an entirely empty remote_dir will error.
    cmd = [
        "scp",
        "-i", endpoint.ssh_key_path,
        "-P", str(endpoint.port),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
    ]
    cmd += _ssh_option_args(endpoint)
    cmd += ["-r", _remote_spec(endpoint, remote_dir.rstrip("/") + "/*"), str(local_root)]
    return _run(cmd, timeout)


def _run(cmd, timeout=3600):
    logger.info("Running remote sync command: %s", " ".join(cmd))
    with _CONCURRENT_TRANSFER_LIMIT:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise RemoteSyncError(str(e)) from e
        except subprocess.TimeoutExpired as e:
            raise RemoteSyncError(f"{cmd[0]} timed out after {e.timeout:.0f}s") from e
    if result.returncode != 0:
        raise RemoteSyncError(f"{cmd[0]} exited {result.returncode}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip() or f"{cmd[0]} completed successfully."
