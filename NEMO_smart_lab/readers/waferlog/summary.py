"""The waferlog reader kind's public entry points: summary, chart data and run history."""

from datetime import datetime

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError, _channel_label
from NEMO_smart_lab.readers.waferlog.parsing import _parse_waferlog
from NEMO_smart_lab.readers.waferlog.runs import (
    _list_waferlog_entries,
    _resolve_waferlog_file,
    _waferlog_local_path,
)


def _waferlog_summary(name, cfg, run_id=None):
    path = _resolve_waferlog_file(cfg, run_id)
    data = _parse_waferlog(path)
    channels = []
    for i, cname in enumerate(data["channel_names"]):
        values = data["channel_data"][i]
        display_name, role, hidden, _threshold = _channel_label(cfg, cname)
        if hidden:
            continue
        channels.append(
            {
                "name": display_name,
                "raw_name": cname,
                "role": role,
                "latest_value": values[-1] if values else None,
                "unit": data["channel_units"][i] if i < len(data["channel_units"]) else "",
                "on": bool(values),
            }
        )
    return {
        "name": name,
        "kind": "waferlog",
        "run_id": data["run_id"],
        "source_file": data["run_id"],
        "recipe": data["recipe"] or "(unknown)",
        "last_update": datetime.fromtimestamp(data["mtime"]),
        "channels": channels,
        "any_on": data["rf_on"],
        "status_label": "Plasma ON" if data["rf_on"] else "Idle",
        "status_class": "warning" if data["rf_on"] else "success",
        "run_duration_s": data["duration_s"],
        "extra": {"machine_id": data["machine_id"]},
    }


def _waferlog_chart_data(cfg, run_id=None):
    path = _resolve_waferlog_file(cfg, run_id)
    data = _parse_waferlog(path)
    series = {}
    for i, cname in enumerate(data["channel_names"]):
        if data["channel_data"][i]:
            display_name, _role, hidden, _threshold = _channel_label(cfg, cname)
            if hidden:
                continue
            series[display_name] = ([t / 1000.0 for t in data["channel_time"][i]], data["channel_data"][i])
    title = f"Recipe: {data['recipe'] or '(unknown)'}"
    return title, "Time (s)", "Endpoint Signal", series


def _waferlog_history(cfg, page, page_size, recipe=None, user_windows=None, start_date=None, end_date=None):
    all_entries = _list_waferlog_entries(cfg)
    start = (page - 1) * page_size
    page_entries = all_entries[start : start + page_size]
    tool = cfg.get("remote_tool")
    if tool is not None:
        remote_cache.ensure_cached_many(tool, [f"WaferLog-Data/{name}" for name, _mtime in page_entries])
    history = []
    for name, _mtime in page_entries:
        try:
            path = _waferlog_local_path(cfg, name)
            data = _parse_waferlog(path)
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["recipe"] or "(unknown)",
                "timestamp": datetime.fromtimestamp(data["mtime"]),
                "duration_s": data["duration_s"],
                "any_on": data["rf_on"],
                "status_label": "Plasma ON" if data["rf_on"] else "Idle",
                "status_class": "warning" if data["rf_on"] else "success",
            }
        )
    return history, len(all_entries)
