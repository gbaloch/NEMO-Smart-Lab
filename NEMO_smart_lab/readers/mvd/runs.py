"""Cambridge Nanotech / Veeco MVD run folders: listing, naming and resolving one by run_id."""

import glob
import os

from datetime import datetime, timedelta

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError, _find_one
from NEMO_smart_lab.readers.run_names import _mvd_folder_timestamp


def _mvd_data_dir(root):
    return os.path.join(root, "log", "data")


def _run_dir_sort_key(dirpath):
    # Kept only as the *fallback* for a folder name that doesn't match _MVD_FOLDER_NAME_RE - the
    # DAT file's own mtime (written once, at the end of the run) rather than the folder's, which
    # could just reflect whenever it was last bulk-copied onto this machine.
    matches = glob.glob(os.path.join(dirpath, "*_DAT.txt"))
    return os.path.getmtime(matches[0]) if matches else os.path.getmtime(dirpath)


def _mvd_run_end(data):
    """The run's own content-derived end time: its folder name's embedded start plus its own last
    elapsed-seconds row - falls back to the DAT file's mtime only if the folder name doesn't parse."""
    folder_ts = _mvd_folder_timestamp(data["run_id"])
    if folder_ts is not None:
        return folder_ts + timedelta(seconds=data["time_s"][-1] if data["time_s"] else 0)
    return datetime.fromtimestamp(data["mtime"])


def _mvd_run_local_path(cfg, name):
    """Local path for one mvd run folder named `name` - fetched (whole subfolder) on demand via
    remote_cache when this tool has a sync_endpoint configured, otherwise assumed already present
    under cfg["root"]/log/data exactly as before."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        return remote_cache.ensure_cached(tool, f"log/data/{name}", is_dir=True)
    return os.path.join(_mvd_data_dir(cfg["root"]), name)


def _list_mvd_run_entries(cfg):
    """Returns [(run_dir_name, sort_mtime), ...] newest first. In remote mode this uses each run
    folder's own mtime *on the remote host* from the cached listing - unlike a *locally copied*
    folder's mtime (see _run_dir_sort_key above, which is about a bulk local copy's timing, not
    about folders in general), a folder's mtime on the remote host itself is a reasonable proxy for
    run recency, since nothing else touches it after the run finishes. Local (non-remote) mode
    keeps the original *_DAT.txt-preferring behavior unchanged."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/log/data")
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
        run_names = [(name, mtime) for name, mtime, _size, is_dir in entries if is_dir]
    else:
        data_dir = _mvd_data_dir(cfg["root"])
        if not os.path.isdir(data_dir):
            raise ToolDataError(f"MVD log data folder not found: {data_dir}")
        run_names = [
            (d, datetime.fromtimestamp(_run_dir_sort_key(os.path.join(data_dir, d))))
            for d in os.listdir(data_dir)
            if os.path.isdir(os.path.join(data_dir, d))
        ]
    if not run_names:
        raise ToolDataError(f"No run folders found for: {cfg['root']}")
    return sorted(run_names, key=lambda item: _mvd_folder_timestamp(item[0]) or item[1], reverse=True)


def _mvd_run_dir_by_run_id(cfg, run_id):
    # run_id comes from a URL - only allow a bare folder name, no path traversal.
    name = os.path.basename(run_id)
    data_dir = _mvd_data_dir(cfg["root"])
    try:
        path = _mvd_run_local_path(cfg, name)
    except remote_sync.RemoteSyncError as e:
        raise ToolDataError(f"Run not found: {run_id} ({e})") from e
    if not os.path.isdir(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(data_dir):
        raise ToolDataError(f"Run not found: {run_id}")
    return path


def _resolve_mvd_run_dir(cfg, run_id):
    if run_id:
        return _mvd_run_dir_by_run_id(cfg, run_id)
    name, _mtime = _list_mvd_run_entries(cfg)[0]
    try:
        return _mvd_run_local_path(cfg, name)
    except remote_sync.RemoteSyncError as e:
        raise ToolDataError(str(e)) from e


def _mvd_screenshot_path(run_dir):
    """A run folder holds one JPEG screenshot of the tool software's post-run report, alongside
    its *_SUM.txt/*_DAT.txt, named after the run's own timestamp prefix (e.g.
    "20260901_122212.jpg" inside "20260901_122212_Plasma Clean.../"). Not every run necessarily
    has one (older recipes/aborted runs may not), so this returns None rather than raising."""
    try:
        return _find_one(run_dir, ".jpg")
    except ToolDataError:
        return None
