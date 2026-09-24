"""The eventlog reader kind's public entry points: summary, event timeline and run history."""

from NEMO_smart_lab.readers.common import FAULT_KEYWORDS, ToolDataError
from NEMO_smart_lab.readers.eventlog.parsing import (
    _eventlog_parse_timestamp,
    _eventlog_process_run_indices,
    _eventlog_read_run,
    _eventlog_rows,
)


def _eventlog_summary(name, cfg, run_id=None):
    data = _eventlog_read_run(cfg, run_id)
    channels = [
        {"name": f"{e['Event']} ({e['Module']})", "latest_value": None, "unit": e["Info"], "on": True}
        for e in data["faults"]
    ]
    return {
        "name": name,
        "kind": "eventlog",
        "run_id": data["run_id"],
        "source_file": f"{data['module']} @ {data['start_time']}" if data["start_time"] else data["run_id"],
        "recipe": data["recipe"] or "(unknown)",
        "last_update": data["end_time"],
        "channels": channels,
        "any_on": bool(data["faults"]),
        "status_label": f"{len(data['faults'])} fault(s)" if data["faults"] else "Clean run",
        "status_class": "danger" if data["faults"] else "success",
        "run_duration_s": data["duration_s"],
        "extra": {"wafer": data["wafer"], "module": data["module"], "event_count": len(data["events"])},
    }


def get_eventlog_timeline(cfg, run_id=None):
    """Returns (title, modules, [(offset_s, module, event_name, is_fault), ...]) for a scatter plot."""
    data = _eventlog_read_run(cfg, run_id)
    points = []
    modules = sorted({e["Module"] for e in data["events"]})
    if data["start_time"] is not None:
        for e in data["events"]:
            t = _eventlog_parse_timestamp(e["Date/Time"])
            if t is None:
                continue
            offset = (t - data["start_time"]).total_seconds()
            is_fault = any(k in e["Event"] for k in FAULT_KEYWORDS)
            points.append((offset, e["Module"], e["Event"], is_fault))
    title = f"Event Timeline: {data['recipe'] or '(unknown)'} ({data['wafer']})" if data["wafer"] else f"Event Timeline: {data['recipe'] or '(unknown)'}"
    return title, modules, points


def _eventlog_history(cfg, page, page_size, recipe=None, user_windows=None, start_date=None, end_date=None):
    rows = _eventlog_rows(cfg)
    all_runs = list(_eventlog_process_run_indices(rows))
    total = len(all_runs)
    start = (page - 1) * page_size
    history = []
    for idx, run_id in all_runs[start : start + page_size]:
        try:
            data = _eventlog_read_run(cfg, run_id)
        except ToolDataError:
            continue
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["recipe"] or "(unknown)",
                "timestamp": data["end_time"],
                "duration_s": data["duration_s"],
                "any_on": bool(data["faults"]),
                "status_label": f"{len(data['faults'])} fault(s)" if data["faults"] else "Clean run",
                "status_class": "danger" if data["faults"] else "success",
            }
        )
    return history, total
