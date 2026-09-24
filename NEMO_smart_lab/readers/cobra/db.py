"""Reading a PlasmaPro Cobra's PTIQ Jobs.db (SQLite): connection, GUID/date decoding, and loading one job."""

import os
import sqlite3
import time
import uuid
from datetime import datetime

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError


#
# Unlike the file-per-run tools above, Cobra's PTIQ control software keeps its process
# history in a SQLite database (Databases-Data/Jobs.db) that grows for the life of the tool -
# Ox-ALE and Ox-gen each have several thousand completed jobs in the sample data. A job's
# TaskID (a GUID) doubles as its run_id here; no filesystem path is involved so there's no
# path-traversal concern; it just has to be a valid GUID.
def _cobra_db_path(cfg):
    """Jobs.db is a single, continuously-growing database, not one file per run - "on demand"
    here means "keep the local cached copy fresh via a TTL-gated re-fetch right before each read",
    not per-run fetching (see NEMO_smart_lab.remote_cache's module docstring)."""
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            return remote_cache.ensure_cached(tool, "Databases-Data/Jobs.db")
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
    return os.path.join(cfg["root"], "Databases-Data", "Jobs.db")


def _cobra_open_connection(db_path, retries=3, delay=0.5):
    if not os.path.exists(db_path):
        raise ToolDataError(f"Jobs.db not found: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    last_error = None
    for _ in range(retries):
        try:
            return sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as e:
            last_error = e
            time.sleep(delay)
    raise ToolDataError(f"Jobs.db is locked or unreadable: {db_path} ({last_error})")


def _cobra_guid_to_str(blob):
    return str(uuid.UUID(bytes_le=blob)) if blob is not None else None


def _cobra_guid_to_bytes(guid_str):
    return uuid.UUID(guid_str).bytes_le


def _cobra_parse_datetime(value):
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    if "." in v:
        base, frac = v.split(".", 1)
        v = f"{base}.{(frac + '000000')[:6]}"
        fmt = "%Y-%m-%d %H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(v, fmt)
    except ValueError:
        return None


def _cobra_duration(start, end):
    s, e = _cobra_parse_datetime(start), _cobra_parse_datetime(end)
    return (e - s).total_seconds() if s is not None and e is not None else None


def _cobra_most_recent_task_id(db_path):
    con = _cobra_open_connection(db_path)
    try:
        cur = con.cursor()
        cur.execute("SELECT TaskID FROM Jobs WHERE EndDate IS NOT NULL ORDER BY EndDate DESC LIMIT 1;")
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        raise ToolDataError(f"No completed jobs found in: {db_path}")
    return _cobra_guid_to_str(row[0])


def _cobra_resolve_task_id(cfg, run_id):
    if run_id:
        return run_id
    return _cobra_most_recent_task_id(_cobra_db_path(cfg))


def _cobra_read_job(cfg, task_id):
    db_path = _cobra_db_path(cfg)
    task_bytes = _cobra_guid_to_bytes(task_id)
    con = _cobra_open_connection(db_path)
    try:
        cur = con.cursor()
        cur.execute("SELECT LotID, StartDate, EndDate, Status, Recipe FROM Jobs WHERE TaskID = ?;", (task_bytes,))
        row = cur.fetchone()
        if row is None:
            raise ToolDataError(f"Job not found: {task_id}")
        lot_id, start_time, end_time, status, recipe = row

        cur.execute("SELECT WaferID, Name, Status FROM Wafers WHERE TaskID = ?;", (task_bytes,))
        wafer_rows = cur.fetchall()

        steps, phases = [], []
        wafers = []
        for wafer_id, name, w_status in wafer_rows:
            wafers.append({"name": name, "status": w_status})
            cur.execute("SELECT ActionID, ActionType FROM WaferActions WHERE WaferID = ? ORDER BY ID;", (wafer_id,))
            for action_id, action_type in cur.fetchall():
                if action_type != "Process":
                    continue
                cur.execute(
                    "SELECT ID, StepID, StartTime, EndTime, Name FROM RecipeStepEntries WHERE ActionID = ? ORDER BY StepID;",
                    (action_id,),
                )
                for row_id, step_idx, s_start, s_end, s_name in cur.fetchall():
                    steps.append(
                        {
                            "stepIndex": step_idx,
                            "name": s_name,
                            "startTime": s_start,
                            "durationSec": _cobra_duration(s_start, s_end),
                        }
                    )
                    cur.execute(
                        "SELECT PhaseID, StartTime, EndTime, Name FROM RecipePhaseEntries WHERE StepID = ? ORDER BY PhaseID, StartTime;",
                        (row_id,),
                    )
                    for phase_id, p_start, p_end, p_name in cur.fetchall():
                        phases.append(
                            {"stepIndex": step_idx, "name": p_name, "durationSec": _cobra_duration(p_start, p_end)}
                        )
    finally:
        con.close()

    return {
        "run_id": task_id,
        "lot_id": lot_id,
        "recipe": recipe,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "duration_s": _cobra_duration(start_time, end_time),
        "wafers": wafers,
        "steps": steps,
        "phases": phases,
    }
