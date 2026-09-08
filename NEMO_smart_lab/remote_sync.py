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

logger = logging.getLogger(__name__)


class RemoteSyncError(Exception):
    """Raised when a sync from a RemoteSyncEndpoint can't be attempted, or fails."""


def _ssh_option_args(endpoint):
    args = []
    for key, value in endpoint.extra_ssh_option_pairs():
        args += ["-o", f"{key}={value}"]
    return args


def _remote_spec(endpoint, remote_dir):
    return f"{endpoint.username}@{endpoint.host}:{remote_dir}"


def sync_tool_from_remote(local_root, endpoint, remote_subdir, dry_run=False):
    """
    Mirrors <endpoint.base_path>/<remote_subdir>/ on the remote host down into local_root
    (created if missing).

    Uses rsync if it's on PATH (incremental - only transfers changed files, safe to run
    frequently e.g. from cron), otherwise falls back to `scp -r` (a full copy of every file on
    every run - rsync isn't installed on Windows by default, and scp has no delta-transfer
    mode). Returns a short human-readable summary string on success.

    Raises RemoteSyncError if the endpoint is missing a username/ssh_key_path, if neither rsync
    nor scp is available, or if the transfer itself fails.
    """
    if not (endpoint.username and endpoint.ssh_key_path):
        raise RemoteSyncError(f"Remote sync endpoint '{endpoint.name}' is missing a username or ssh_key_path.")

    remote_dir = f"{endpoint.base_path.rstrip('/')}/{remote_subdir}"
    os.makedirs(local_root, exist_ok=True)

    if shutil.which("rsync"):
        return _sync_with_rsync(local_root, endpoint, remote_dir, dry_run)
    if shutil.which("scp"):
        return _sync_with_scp(local_root, endpoint, remote_dir, dry_run)
    raise RemoteSyncError("Neither rsync nor scp were found on PATH - install an OpenSSH client to sync.")


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


def _sync_with_rsync(local_root, endpoint, remote_dir, dry_run):
    # Trailing slash on the source means "copy the contents of remote_dir", not "copy remote_dir
    # itself as a subfolder" - local_root is expected to directly contain e.g. "Logfile/", the
    # same as if it were a raw network share mounted straight from the tool PC.
    cmd = [
        "rsync", "-az", "--partial",
        "-e", _ssh_command_str(endpoint),
        _remote_spec(endpoint, remote_dir.rstrip("/") + "/"),
        str(local_root).rstrip("\\/") + "/",
    ]
    if dry_run:
        cmd.insert(1, "--dry-run")
    return _run(cmd)


def _sync_with_scp(local_root, endpoint, remote_dir, dry_run):
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
    return _run(cmd)


def _run(cmd):
    logger.info("Running remote sync command: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as e:
        raise RemoteSyncError(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise RemoteSyncError(f"{cmd[0]} timed out after {e.timeout:.0f}s") from e
    if result.returncode != 0:
        raise RemoteSyncError(f"{cmd[0]} exited {result.returncode}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip() or f"{cmd[0]} completed successfully."
