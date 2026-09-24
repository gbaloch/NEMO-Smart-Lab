"""Veeco Fiji/Savannah "Heater Data" run files: listing them and resolving one by run_id."""

import os

from datetime import datetime, timedelta

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import EmptyRunFileError, ToolDataError
from NEMO_smart_lab.readers.heater_log.parsing import _parse_heater_log
from NEMO_smart_lab.readers.run_names import _heater_log_filename_timestamp


def _heater_log_run_end(data):
    """The run's own content-derived end time: its filename's embedded start plus its own last
    elapsed-seconds row - falls back to the file's mtime only if the filename doesn't parse."""
    filename_ts = _heater_log_filename_timestamp(data["run_id"])
    if filename_ts is not None:
        return filename_ts + timedelta(seconds=data["time_s"][-1] if data["time_s"] else 0)
    return datetime.fromtimestamp(data["mtime"])


def _heater_log_dir(root):
    return os.path.join(root, "Logfile", "Heater Data")


def _heater_log_local_path(cfg, name):
    """Local path for one heater log file named `name` - fetched on demand via remote_cache when
    this tool has a sync_endpoint configured (cfg["remote_tool"], set by
    SmartLabTool.as_source_config()), otherwise assumed already present under cfg["root"] exactly
    as before."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        return remote_cache.ensure_cached(tool, f"Logfile/Heater Data/{name}")
    return os.path.join(_heater_log_dir(cfg["root"]), name)


def _list_heater_log_entries(cfg):
    """Returns [(filename, mtime), ...] newest first. Reads the remote_cache-cached *remote*
    listing when this tool has a sync_endpoint (so a run that was never locally fetched still gets
    picked up for "most recent"/history ordering - relying on local mtimes alone would silently
    miss any run that hasn't been cached yet), otherwise stats the local directory exactly as
    before."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/Logfile/Heater Data")
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
        files = [(name, mtime) for name, mtime, _size, is_dir in entries if not is_dir and name.lower().endswith(".txt")]
    else:
        heater_dir = _heater_log_dir(cfg["root"])
        if not os.path.isdir(heater_dir):
            raise ToolDataError(f"Heater Data folder not found: {heater_dir}")
        files = [
            (f, datetime.fromtimestamp(os.path.getmtime(os.path.join(heater_dir, f))))
            for f in os.listdir(heater_dir)
            if f.lower().endswith(".txt")
        ]
    if not files:
        raise ToolDataError(f"No heater log files found for: {cfg['root']}")
    return sorted(files, key=lambda item: _heater_log_filename_timestamp(item[0]) or item[1], reverse=True)


def _heater_log_file_by_run_id(cfg, run_id):
    # run_id comes from a URL - only allow a bare filename, no path traversal.
    name = os.path.basename(run_id)
    heater_dir = _heater_log_dir(cfg["root"])
    try:
        path = _heater_log_local_path(cfg, name)
    except remote_sync.RemoteSyncError as e:
        raise ToolDataError(f"Run not found: {run_id} ({e})") from e
    if not os.path.isfile(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(heater_dir):
        raise ToolDataError(f"Run not found: {run_id}")
    return path


_MAX_EMPTY_RUNS_TO_SKIP = 10


def _resolve_heater_log_file(cfg, run_id):
    if run_id:
        return _heater_log_file_by_run_id(cfg, run_id)
    # The newest file isn't always a usable run: an immediately-aborted one leaves a header-only
    # file behind (see EmptyRunFileError). Walk back to the newest one that actually has data
    # rather than making the whole tool's summary/status an error until the next real run.
    entries = _list_heater_log_entries(cfg)
    first_error = None
    for name, _mtime in entries[:_MAX_EMPTY_RUNS_TO_SKIP]:
        try:
            path = _heater_log_local_path(cfg, name)
            _parse_heater_log(path)
            return path
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
        except EmptyRunFileError as e:
            first_error = first_error or e
    raise first_error
