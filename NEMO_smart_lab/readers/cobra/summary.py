"""The cobra_job reader kind's public entry points: summary, step timeline and job history."""

from NEMO_smart_lab.readers.cobra.stream import get_stream_summary
from NEMO_smart_lab.readers.cobra.db import (
    _cobra_db_path,
    _cobra_duration,
    _cobra_guid_to_str,
    _cobra_open_connection,
    _cobra_parse_datetime,
    _cobra_read_job,
    _cobra_resolve_task_id,
)


_COBRA_BAD_STATUSES = ("abort", "fail", "error")


def _cobra_summary(name, cfg, run_id=None):
    task_id = _cobra_resolve_task_id(cfg, run_id)
    data = _cobra_read_job(cfg, task_id)
    channels = [
        {"name": f"Wafer {w['name']}" if w["name"] else "Wafer", "latest_value": None, "unit": w["status"] or "", "on": None}
        for w in data["wafers"]
    ]
    status = (data["status"] or "").lower()
    is_bad = any(k in status for k in _COBRA_BAD_STATUSES)
    return {
        "name": name,
        "kind": "cobra_job",
        "run_id": data["run_id"],
        "source_file": f"Job {data['run_id']}",
        "recipe": data["recipe"] or "(unknown)",
        "last_update": _cobra_parse_datetime(data["end_time"]),
        "channels": channels,
        "any_on": False,
        "status_label": data["status"] or "Unknown",
        "status_class": "danger" if is_bad else "success",
        "run_duration_s": data["duration_s"],
        "extra": {"lot_id": data["lot_id"], "step_count": len(data["steps"]), "phase_count": len(data["phases"])},
        "stream": get_stream_summary(cfg),
    }


def get_cobra_step_timeline(cfg, run_id=None):
    """Returns (title, [(step_label, start_offset_s, duration_s), ...]) for a Gantt-style plot."""
    task_id = _cobra_resolve_task_id(cfg, run_id)
    data = _cobra_read_job(cfg, task_id)
    job_start = _cobra_parse_datetime(data["start_time"])
    bars = []
    if job_start is not None:
        for step in data["steps"]:
            s = _cobra_parse_datetime(step["startTime"])
            if s is None or step["durationSec"] is None:
                continue
            offset = (s - job_start).total_seconds()
            bars.append((f"Step {step['stepIndex']}: {step['name']}", offset, step["durationSec"]))
    title = f"Recipe: {data['recipe'] or '(unknown)'}"
    return title, bars


def _cobra_history(cfg, page, page_size, recipe=None, user_windows=None, start_date=None, end_date=None):
    db_path = _cobra_db_path(cfg)
    con = _cobra_open_connection(db_path)
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM Jobs WHERE EndDate IS NOT NULL;")
        total = cur.fetchone()[0]
        cur.execute(
            "SELECT TaskID, EndDate, Status, Recipe, StartDate FROM Jobs WHERE EndDate IS NOT NULL "
            "ORDER BY EndDate DESC LIMIT ? OFFSET ?;",
            (page_size, (page - 1) * page_size),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    history = []
    for task_id_blob, end_date, status, recipe, start_date in rows:
        status_lower = (status or "").lower()
        history.append(
            {
                "run_id": _cobra_guid_to_str(task_id_blob),
                "recipe": recipe or "(unknown)",
                "timestamp": _cobra_parse_datetime(end_date),
                "duration_s": _cobra_duration(start_date, end_date),
                "any_on": any(k in status_lower for k in _COBRA_BAD_STATUSES),
                "status_label": status or "Unknown",
                "status_class": "danger" if any(k in status_lower for k in _COBRA_BAD_STATUSES) else "success",
            }
        )
    return history, total
