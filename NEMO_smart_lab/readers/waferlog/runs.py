"""WaferLog run files: listing and resolving one by run_id."""

import os
from datetime import datetime

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError


def _waferlog_dir(root):
    return os.path.join(root, "WaferLog-Data")


def _waferlog_local_path(cfg, name):
    tool = cfg.get("remote_tool")
    if tool is not None:
        return remote_cache.ensure_cached(tool, f"WaferLog-Data/{name}")
    return os.path.join(_waferlog_dir(cfg["root"]), name)


def _list_waferlog_entries(cfg):
    """Returns [(filename, mtime), ...] newest first - see _list_heater_log_entries, identical
    reasoning (remote listing when remote_tool is set, so an unfetched run still sorts correctly)."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/WaferLog-Data")
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
        files = [(name, mtime) for name, mtime, _size, is_dir in entries if not is_dir and name.lower().endswith(".txt")]
    else:
        wafer_dir = _waferlog_dir(cfg["root"])
        if not os.path.isdir(wafer_dir):
            raise ToolDataError(f"WaferLog-Data folder not found: {wafer_dir}")
        files = [
            (f, datetime.fromtimestamp(os.path.getmtime(os.path.join(wafer_dir, f))))
            for f in os.listdir(wafer_dir)
            if f.lower().endswith(".txt")
        ]
    if not files:
        raise ToolDataError(f"No wafer log files found for: {cfg['root']}")
    return sorted(files, key=lambda item: item[1], reverse=True)


def _waferlog_file_by_run_id(cfg, run_id):
    name = os.path.basename(run_id)
    wafer_dir = _waferlog_dir(cfg["root"])
    try:
        path = _waferlog_local_path(cfg, name)
    except remote_sync.RemoteSyncError as e:
        raise ToolDataError(f"Run not found: {run_id} ({e})") from e
    if not os.path.isfile(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(wafer_dir):
        raise ToolDataError(f"Run not found: {run_id}")
    return path


def _resolve_waferlog_file(cfg, run_id):
    if run_id:
        return _waferlog_file_by_run_id(cfg, run_id)
    name, _mtime = _list_waferlog_entries(cfg)[0]
    try:
        return _waferlog_local_path(cfg, name)
    except remote_sync.RemoteSyncError as e:
        raise ToolDataError(str(e)) from e
