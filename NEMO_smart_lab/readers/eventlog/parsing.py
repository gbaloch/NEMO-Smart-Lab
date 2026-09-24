"""Reading the KLA-DSE EventLog file and locating one process run in it."""

import csv
import os
import re
from datetime import datetime

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import FAULT_KEYWORDS, TERMINATING_EVENTS, ToolDataError


def _eventlog_path(cfg):
    """CurrentEvents.csv is a single, continuously-growing log, not one file per run - "on demand"
    here means "keep the local cached copy fresh via a TTL-gated re-fetch right before each read",
    not per-run fetching (see NEMO_smart_lab.remote_cache's module docstring)."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            path = remote_cache.ensure_cached(tool, "EventLog-Data/CurrentEvents.csv")
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
    else:
        path = os.path.join(cfg["root"], "EventLog-Data", "CurrentEvents.csv")
    if not os.path.isfile(path):
        raise ToolDataError(f"CurrentEvents.csv not found: {path}")
    return path


def _eventlog_rows(cfg):
    with open(_eventlog_path(cfg), encoding="utf-8-sig", errors="replace", newline="") as f:
        return list(csv.DictReader(f))


def _eventlog_parse_timestamp(value):
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _eventlog_process_run_indices(rows):
    """Yields (start_index, run_id) for every DO PROCESS event, newest first."""
    for i, row in enumerate(rows):
        if row.get("Event") == "DO PROCESS":
            yield i, f"{row['Date/Time']}|{row['Module']}|{row['Info']}"


def _eventlog_read_run(cfg, run_id=None):
    rows = _eventlog_rows(cfg)

    start_idx = None
    if run_id:
        for i, key in _eventlog_process_run_indices(rows):
            if key == run_id:
                start_idx = i
                break
        if start_idx is None:
            raise ToolDataError(f"Run not found: {run_id}")
    else:
        for i, key in _eventlog_process_run_indices(rows):
            start_idx = i
            run_id = key
            break
        if start_idx is None:
            raise ToolDataError(f"No process runs found in: {cfg['root']}")

    start_row = rows[start_idx]
    module_name = start_row["Module"]
    start_time = _eventlog_parse_timestamp(start_row["Date/Time"])

    m = re.search(r"Recipe\s*:\s*(.+?)\s*-\s*Wafer:\s*(.+)$", start_row.get("Info", "").strip())
    recipe, wafer = (m.group(1).strip(), m.group(2).strip()) if m else (start_row.get("Info", "").strip(), "")

    end_idx = 0
    for i in range(start_idx - 1, -1, -1):
        if rows[i]["Module"] == module_name and rows[i]["Event"] in TERMINATING_EVENTS:
            end_idx = i
            break

    end_time = _eventlog_parse_timestamp(rows[end_idx]["Date/Time"])
    duration_s = (end_time - start_time).total_seconds() if start_time and end_time else None

    events = list(reversed(rows[end_idx : start_idx + 1]))
    faults = [e for e in events if any(k in e["Event"] for k in FAULT_KEYWORDS)]

    return {
        "run_id": run_id,
        "recipe": recipe,
        "wafer": wafer,
        "module": module_name,
        "start_time": start_time,
        "end_time": end_time,
        "duration_s": duration_s,
        "events": events,
        "faults": faults,
    }
