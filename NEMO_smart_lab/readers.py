"""
Readers for the raw process-log formats produced by the ALD tools configured in
NEMO_smart_lab.config.SMART_LAB_TOOL_SOURCES.

These are intentionally read-only and stateless: every call re-reads whatever run file/folder
is asked for at the time of the request (defaulting to the most recent one), so the plugin
always reflects current tool state without needing a separate collector process or database.
Every "run" (one file for heater_log tools, one folder for mvd tools) has a stable "run_id" -
its filename or folder name - that callers can pass back in to look up that specific run again
for the history view.
"""

import csv
import glob
import os
import re
import sqlite3
import struct
import time
import uuid
from datetime import datetime

FILE_ENCODING = "latin-1"

DEFAULT_HISTORY_LIMIT = 25


class ToolDataError(Exception):
    """Raised when a tool's configured data source, or a specific run within it, can't be found or parsed."""


# -------------------- Veeco Fiji / Savannah "Heater Data" log files --------------------


def _heater_log_dir(root):
    heater_dir = os.path.join(root, "Logfile", "Heater Data")
    if not os.path.isdir(heater_dir):
        raise ToolDataError(f"Heater Data folder not found: {heater_dir}")
    return heater_dir


def _sorted_heater_log_files(root):
    heater_dir = _heater_log_dir(root)
    files = [os.path.join(heater_dir, f) for f in os.listdir(heater_dir) if f.lower().endswith(".txt")]
    if not files:
        raise ToolDataError(f"No heater log files found in: {heater_dir}")
    return sorted(files, key=os.path.getmtime, reverse=True)


def _heater_log_file_by_run_id(root, run_id):
    heater_dir = _heater_log_dir(root)
    # run_id comes from a URL - only allow a bare filename within heater_dir, no path traversal.
    path = os.path.join(heater_dir, os.path.basename(run_id))
    if not os.path.isfile(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(heater_dir):
        raise ToolDataError(f"Run not found: {run_id}")
    return path


def _resolve_heater_log_file(root, run_id):
    if run_id:
        return _heater_log_file_by_run_id(root, run_id)
    return _sorted_heater_log_files(root)[0]


# Fixed trailing columns that always follow the heater temperature columns, in order.
# The heater temperature columns in between vary in count: some tools (e.g. Savannah) only
# have a subset of the 12 physical heater zones wired up, and simply omit the unused trailing
# heater columns from every data row instead of writing a placeholder - so the row is shorter
# than the header and can't be aligned to it by column name/position. Anchoring the known
# columns from both ends of the row (leading blank + "Heater Time", trailing metadata block)
# and treating whatever is left in the middle as "however many heater channels are actually
# present, in header order" handles both cases.
_TRAILING_COLUMNS = ["Program Time", "MFC 1", "MFC Time", "Cycles Remaining", "Recipe", "Loop"]


def _parse_heater_log(path):
    with open(path, encoding=FILE_ENCODING) as f:
        lines = [line.rstrip("\n").rstrip("\r") for line in f if line.strip()]
    if not lines:
        raise ToolDataError(f"Heater log file is empty: {path}")

    header = [h.strip() for h in lines[0].split("\t")]
    all_rows = [line.split("\t") for line in lines[1:]]
    if not all_rows:
        raise ToolDataError(f"Heater log file has no data rows: {path}")

    row_length = len(all_rows[0])
    rows = [r for r in all_rows if len(r) == row_length]

    heater_names_in_order = [h for h in header if h.startswith("Heater") and h != "Heater Time"]
    num_heater_cols = row_length - 2 - len(_TRAILING_COLUMNS)  # leading blank + "Heater Time"
    num_heater_cols = max(0, min(num_heater_cols, len(heater_names_in_order)))
    present_heater_names = heater_names_in_order[:num_heater_cols]

    time_idx = 1
    heater_start_idx = 2
    recipe_idx = row_length - 2
    cycles_idx = row_length - 3

    def to_float(value):
        try:
            return float(value)
        except (ValueError, IndexError):
            return None

    time_s = []
    channel_series = {name: [] for name in present_heater_names}
    recipe = ""
    cycles_remaining = ""
    for row in rows:
        t = to_float(row[time_idx])
        if t is None:
            continue
        time_s.append(t)
        for i, name in enumerate(present_heater_names):
            channel_series[name].append(to_float(row[heater_start_idx + i]))
        if 0 <= recipe_idx < len(row) and row[recipe_idx].strip():
            recipe = row[recipe_idx].strip()
        if 0 <= cycles_idx < len(row) and row[cycles_idx].strip():
            cycles_remaining = row[cycles_idx].strip()

    latest = {}
    for name, series in channel_series.items():
        non_null = [v for v in series if v is not None]
        latest[name] = non_null[-1] if non_null else None

    return {
        "path": path,
        "run_id": os.path.basename(path),
        "recipe": recipe,
        "cycles_remaining": cycles_remaining,
        "mtime": os.path.getmtime(path),
        "time_s": time_s,
        "channel_series": channel_series,
        "latest": latest,
    }


def _heater_log_summary(name, cfg, run_id=None):
    path = _resolve_heater_log_file(cfg["root"], run_id)
    data = _parse_heater_log(path)
    threshold = cfg.get("on_threshold_c", 35.0)
    channels = []
    for channel, value in sorted(data["latest"].items()):
        on = value is not None and value > threshold
        channels.append({"name": channel.strip(), "latest_value": value, "unit": "C", "on": on})
    channels.sort(key=lambda c: c["name"])
    return {
        "name": name,
        "kind": "heater_log",
        "run_id": data["run_id"],
        "source_file": data["run_id"],
        "recipe": data["recipe"] or "(unknown)",
        "last_update": datetime.fromtimestamp(data["mtime"]),
        "channels": channels,
        "any_on": any(c["on"] for c in channels),
        "status_label": "ON" if any(c["on"] for c in channels) else "Idle",
        "status_class": "warning" if any(c["on"] for c in channels) else "success",
        "run_duration_s": data["time_s"][-1] if data["time_s"] else None,
        "extra": {"cycles_remaining": data["cycles_remaining"]},
    }


def _heater_log_chart_data(cfg, run_id=None):
    path = _resolve_heater_log_file(cfg["root"], run_id)
    data = _parse_heater_log(path)
    series = {name.strip(): (data["time_s"], values) for name, values in data["channel_series"].items()}
    return f"Recipe: {data['recipe'] or '(unknown)'}", "Time (s)", "Temperature (C)", series


def _heater_log_history(cfg, page, page_size):
    # Sorting the file list is cheap (just a stat call per file, no parsing) so it's done over
    # every run; only the one page actually being displayed gets fully parsed.
    all_files = _sorted_heater_log_files(cfg["root"])
    start = (page - 1) * page_size
    threshold = cfg.get("on_threshold_c", 35.0)
    history = []
    for path in all_files[start : start + page_size]:
        try:
            data = _parse_heater_log(path)
        except ToolDataError:
            continue
        latest_values = [v for v in data["latest"].values() if v is not None]
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["recipe"] or "(unknown)",
                "timestamp": datetime.fromtimestamp(data["mtime"]),
                "duration_s": data["time_s"][-1] if data["time_s"] else None,
                "any_on": any(v > threshold for v in latest_values),
                "status_label": "ON" if any(v > threshold for v in latest_values) else "Idle",
                "status_class": "warning" if any(v > threshold for v in latest_values) else "success",
            }
        )
    return history, len(all_files)


# -------------------- Cambridge Nanotech / Veeco MVD run folders --------------------

_HEATER_LABEL_RE = re.compile(r'^HTR(\d+)\s*=\s*"([^"]*)"', re.MULTILINE)


def _mvd_data_dir(root):
    data_dir = os.path.join(root, "log", "data")
    if not os.path.isdir(data_dir):
        raise ToolDataError(f"MVD log data folder not found: {data_dir}")
    return data_dir


def _run_dir_sort_key(dirpath):
    # The run folder's own mtime reflects whenever it was last copied onto this machine
    # (e.g. all at once during an initial data sync), not when the run happened. The DAT
    # file inside it is written once, at the end of the run, so its mtime is a reliable proxy
    # for run recency even after a bulk copy.
    matches = glob.glob(os.path.join(dirpath, "*_DAT.txt"))
    return os.path.getmtime(matches[0]) if matches else os.path.getmtime(dirpath)


def _sorted_mvd_run_dirs(root):
    data_dir = _mvd_data_dir(root)
    dirs = [os.path.join(data_dir, d) for d in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, d))]
    if not dirs:
        raise ToolDataError(f"No run folders found in: {data_dir}")
    return sorted(dirs, key=_run_dir_sort_key, reverse=True)


def _mvd_run_dir_by_run_id(root, run_id):
    data_dir = _mvd_data_dir(root)
    # run_id comes from a URL - only allow a bare folder name within data_dir, no path traversal.
    path = os.path.join(data_dir, os.path.basename(run_id))
    if not os.path.isdir(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(data_dir):
        raise ToolDataError(f"Run not found: {run_id}")
    return path


def _resolve_mvd_run_dir(root, run_id):
    if run_id:
        return _mvd_run_dir_by_run_id(root, run_id)
    return _sorted_mvd_run_dirs(root)[0]


def _find_one(dirpath, suffix):
    matches = glob.glob(os.path.join(dirpath, f"*{suffix}"))
    if not matches:
        raise ToolDataError(f"No *{suffix} file found in: {dirpath}")
    return matches[0]


def _parse_mvd_summary_text(text):
    def find(key):
        m = re.search(rf"^{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    heater_labels = {num: label for num, label in _HEATER_LABEL_RE.findall(text)}
    return {
        "recipe": find("Recipe name"),
        "status": find("Recipe status"),
        "completion_status": find("Completion status"),
        "start_time": find("Start time"),
        "end_time": find("End time"),
        "run_time": find("Run time"),
        "heater_labels": heater_labels,
    }


_HTR_TEMP_RE = re.compile(r"^HTR(\d+)\(C\)$")
_HTR_DUTY_RE = re.compile(r"^HTR(\d+)\(%\)$")


def _parse_mvd_dat(path):
    with open(path, encoding=FILE_ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    time_idx = header.index("Time(sec)") if "Time(sec)" in header else 0
    temp_cols = {}
    duty_cols = {}
    for idx, col in enumerate(header):
        m = _HTR_TEMP_RE.match(col)
        if m:
            temp_cols[m.group(1)] = idx
            continue
        m = _HTR_DUTY_RE.match(col)
        if m:
            duty_cols[m.group(1)] = idx

    def col_floats(idx):
        result = []
        for row in rows:
            if idx >= len(row):
                result.append(None)
                continue
            try:
                result.append(float(row[idx]))
            except ValueError:
                result.append(None)
        return result

    time_s = col_floats(time_idx)
    temp_series = {num: col_floats(idx) for num, idx in temp_cols.items()}
    duty_series = {num: col_floats(idx) for num, idx in duty_cols.items()}
    return time_s, temp_series, duty_series


def _mvd_run_data_for_dir(run_dir):
    sum_path = _find_one(run_dir, "_SUM.txt")
    dat_path = _find_one(run_dir, "_DAT.txt")
    with open(sum_path, encoding=FILE_ENCODING) as f:
        summary = _parse_mvd_summary_text(f.read())
    time_s, temp_series, duty_series = _parse_mvd_dat(dat_path)
    return {
        "run_dir": run_dir,
        "run_id": os.path.basename(run_dir),
        "mtime": os.path.getmtime(dat_path),
        "summary": summary,
        "time_s": time_s,
        "temp_series": temp_series,
        "duty_series": duty_series,
    }


def _mvd_run_data(root, run_id=None):
    return _mvd_run_data_for_dir(_resolve_mvd_run_dir(root, run_id))


def _last_non_null(values):
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _mvd_summary(name, cfg, run_id=None):
    data = _mvd_run_data(cfg["root"], run_id)
    threshold = cfg.get("on_threshold_pct", 0.5)
    labels = data["summary"]["heater_labels"]
    channels = []
    for num in sorted(data["temp_series"], key=int):
        label = labels.get(num, "").strip() or f"Heater {num}"
        latest_temp = _last_non_null(data["temp_series"][num])
        latest_duty = _last_non_null(data["duty_series"].get(num, []))
        on = latest_duty is not None and latest_duty > threshold
        channels.append(
            {
                "name": f"{label} (HTR{num})",
                "latest_value": latest_temp,
                "unit": "C",
                "duty_pct": latest_duty,
                "on": on,
            }
        )
    return {
        "name": name,
        "kind": "mvd",
        "run_id": data["run_id"],
        "source_file": data["run_id"],
        "recipe": data["summary"]["recipe"] or "(unknown)",
        "last_update": datetime.fromtimestamp(data["mtime"]),
        "channels": channels,
        "any_on": any(c["on"] for c in channels),
        "status_label": "ON" if any(c["on"] for c in channels) else "Idle",
        "status_class": "warning" if any(c["on"] for c in channels) else "success",
        "run_duration_s": data["time_s"][-1] if data["time_s"] else None,
        "extra": {
            "status": data["summary"]["status"],
            "completion_status": data["summary"]["completion_status"],
            "run_time": data["summary"]["run_time"],
        },
    }


def _mvd_chart_data(cfg, run_id=None):
    data = _mvd_run_data(cfg["root"], run_id)
    labels = data["summary"]["heater_labels"]
    series = {}
    for num, values in data["temp_series"].items():
        label = labels.get(num, "").strip() or f"Heater {num}"
        series[f"{label} (HTR{num})"] = (data["time_s"], values)
    title = f"Recipe: {data['summary']['recipe'] or '(unknown)'}"
    return title, "Time (s)", "Temperature (C)", series


def _mvd_history(cfg, page, page_size):
    # Sorting the run list is cheap (one stat call per run, no parsing) so it's done over
    # every run; only the one page actually being displayed gets fully parsed.
    all_dirs = _sorted_mvd_run_dirs(cfg["root"])
    start = (page - 1) * page_size
    threshold = cfg.get("on_threshold_pct", 0.5)
    history = []
    for run_dir in all_dirs[start : start + page_size]:
        try:
            data = _mvd_run_data_for_dir(run_dir)
        except ToolDataError:
            continue
        latest_duties = [_last_non_null(v) for v in data["duty_series"].values()]
        latest_duties = [v for v in latest_duties if v is not None]
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["summary"]["recipe"] or "(unknown)",
                "timestamp": datetime.fromtimestamp(data["mtime"]),
                "duration_s": data["time_s"][-1] if data["time_s"] else None,
                "any_on": any(v > threshold for v in latest_duties),
                "status_label": "ON" if any(v > threshold for v in latest_duties) else "Idle",
                "status_class": "warning" if any(v > threshold for v in latest_duties) else "success",
            }
        )
    return history, len(all_dirs)


# -------------------- Oxford Instruments PlasmaPro 100 Cobra (PTIQ Jobs.db) --------------------
#
# Unlike the file-per-run tools above, Cobra's PTIQ control software keeps its process
# history in a SQLite database (Databases-Data/Jobs.db) that grows for the life of the tool -
# Ox-ALE and Ox-gen each have several thousand completed jobs in the sample data. A job's
# TaskID (a GUID) doubles as its run_id here; no filesystem path is involved so there's no
# path-traversal concern; it just has to be a valid GUID.


def _cobra_db_path(root):
    return os.path.join(root, "Databases-Data", "Jobs.db")


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


def _cobra_resolve_task_id(root, run_id):
    if run_id:
        return run_id
    return _cobra_most_recent_task_id(_cobra_db_path(root))


def _cobra_read_job(root, task_id):
    db_path = _cobra_db_path(root)
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


_COBRA_BAD_STATUSES = ("abort", "fail", "error")


def _cobra_summary(name, cfg, run_id=None):
    task_id = _cobra_resolve_task_id(cfg["root"], run_id)
    data = _cobra_read_job(cfg["root"], task_id)
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
    task_id = _cobra_resolve_task_id(cfg["root"], run_id)
    data = _cobra_read_job(cfg["root"], task_id)
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


def _cobra_history(cfg, page, page_size):
    db_path = _cobra_db_path(cfg["root"])
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


# -------------------- Optional: PTIQ live telemetry (StreamedData) for Cobra tools --------------------
#
# Separate from Jobs.db - this is the tool's raw, high-frequency process telemetry
# (PTIQ/Databases/StreamedData/<module>/<year>/<month>/<day>/<hour>/<minute>_PD.stream), a
# sequence of msgpack objects: a header, a table of ~300 channel id->name definitions, then a
# long alternating sequence of either ("m", elapsed_ms) time markers or (channel_id, value)
# updates. Optional because it needs the "msgpack" package and a "stream_root" pointing at a
# synced copy of PTIQ/Databases/StreamedData - most sites won't have that set up.
#
# Reverse-engineered quirk: PTIQ writes its float64 channel values in *little-endian* byte
# order, which is the opposite of what the msgpack spec requires (big-endian). A spec-correct
# msgpack decoder (like the "msgpack" package used here) still "succeeds" at decoding them -
# it just produces nonsense subnormal floats near zero, which is why a first pass at this data
# looked like it was full of empty/garbage values. Byte-swapping each decoded float (undo the
# decoder's big-endian read, redo it little-endian) turns them back into real, plausible
# process values (e.g. a chamber pressure channel decoded this way holds steady around 0.7,
# matching a real idle-chamber Torr reading, instead of ~1e-314).
#
# This only ever looks at whatever the single most recently modified minute-file is - it's a
# live "what's happening right now" snapshot, not a historical trace across many files (there
# can be thousands of these for a long-lived tool, and each one has to be fully decoded to be
# read at all, so scanning a long history of them isn't practical to do on every request).

_STREAM_INTERESTING_CHANNEL_SUFFIXES = (
    "RFGen1.ForwardPower.Value",
    "RFGen1.ReflectedPower.Value",
    "RFGen1.DCBias",
    "RFGen2.ForwardPower.Value",
    "RFGen2.ReflectedPower.Value",
    "RFGen2.DCBias",
    "APC.Pressure.Value",
    "ProcessGauge.Pressure.Value",
    "HIVACGauge.Pressure.Value",
)


def _latest_stream_file(stream_root, module):
    path = os.path.join(stream_root, module)
    if not os.path.isdir(path):
        raise ToolDataError(f"StreamedData module folder not found: {path}")
    # Walk year -> month -> day -> hour, taking the lexicographically-latest (i.e. most
    # recent, since these are zero-padded numeric names) entry at each level.
    for _ in range(4):
        entries = [e for e in os.listdir(path) if os.path.isdir(os.path.join(path, e))]
        if not entries:
            raise ToolDataError(f"No dated StreamedData folders found under: {path}")
        path = os.path.join(path, max(entries))
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".stream")]
    if not files:
        raise ToolDataError(f"No .stream files found in: {path}")
    return max(files, key=os.path.basename)


def _fix_stream_float(value):
    """Undoes msgpack's spec-correct big-endian float64 read and redoes it little-endian,
    matching how PTIQ actually wrote it. See the module-level comment above for how this was
    figured out. Non-float values (small integer state/enum codes) are passed through as-is -
    only float64 payloads exhibit this byte-order quirk."""
    if isinstance(value, float):
        return struct.unpack("<d", struct.pack(">d", value))[0]
    return value


def _parse_stream_file(path):
    try:
        import msgpack
    except ImportError as e:
        raise ToolDataError("The 'msgpack' package is required to read StreamedData files") from e

    with open(path, "rb") as f:
        data = f.read()
    unpacker = msgpack.Unpacker(raw=False)
    unpacker.feed(data)
    items = list(unpacker)
    markers, payloads = items[0::2], items[1::2]

    module_name = None
    channel_names = {}
    for marker, payload in zip(markers, payloads):
        if marker == "h" and isinstance(payload, dict):
            module_name = payload.get("m")
        elif marker == "d" and isinstance(payload, list) and len(payload) == 4 and isinstance(payload[1], str):
            channel_names[payload[0]] = payload[1]

    series = {}  # channel_id -> ([elapsed_ms, ...], [value, ...])
    elapsed_ms = 0
    for marker, payload in zip(markers, payloads):
        if marker == "m":
            elapsed_ms = payload
        elif isinstance(marker, int) and marker in channel_names:
            times, values = series.setdefault(marker, ([], []))
            times.append(elapsed_ms)
            values.append(_fix_stream_float(payload))

    return {
        "path": path,
        "run_id": os.path.basename(path),
        "mtime": os.path.getmtime(path),
        "module_name": module_name,
        "channel_names": channel_names,
        "series": series,
    }


def _interesting_stream_channels(data):
    for channel_id, name in data["channel_names"].items():
        if any(name.endswith(suffix) for suffix in _STREAM_INTERESTING_CHANNEL_SUFFIXES):
            yield channel_id, name


def get_stream_summary(cfg):
    """Returns a small dict describing the latest StreamedData snapshot, or None if this tool
    isn't configured for it (no "stream_root") or none could be read."""
    stream_root = cfg.get("stream_root")
    if not stream_root:
        return None
    try:
        data = _parse_stream_file(_latest_stream_file(stream_root, cfg.get("stream_module", "PMC1")))
    except ToolDataError:
        return None

    channels = []
    for channel_id, name in _interesting_stream_channels(data):
        _, values = data["series"].get(channel_id, ([], []))
        channels.append({"name": name, "latest_value": values[-1] if values else None})
    channels.sort(key=lambda c: c["name"])

    return {
        "run_id": data["run_id"],
        "module_name": data["module_name"],
        "timestamp": datetime.fromtimestamp(data["mtime"]),
        "channels": channels,
    }


def get_stream_chart_data(cfg):
    stream_root = cfg.get("stream_root")
    if not stream_root:
        raise ToolDataError("No stream_root configured for this tool")
    data = _parse_stream_file(_latest_stream_file(stream_root, cfg.get("stream_module", "PMC1")))
    series = {}
    for channel_id, name in _interesting_stream_channels(data):
        times, values = data["series"].get(channel_id, ([], []))
        if times:
            series[name] = ([t / 1000.0 for t in times], values)
    title = f"Live telemetry - {data['module_name'] or ''} ({data['run_id']})"
    return title, "Time (s)", "", series


# -------------------- Plasma-Therm VersaLine WaferLog (hdpcvd) --------------------
#
# Same idea as the Fiji/Savannah heater logs, but each wafer run is its own file (no
# "most recently modified file in a folder" ambiguity beyond picking the newest by mtime),
# and the interesting per-run channels are the endpoint-detector traces recorded during the
# run plus each step's RF power - "on" here means plasma was actually struck, not that a
# heater is above ambient.

_GAS_KEYS = ("Ar", "CH4", "N2a", "N2b", "N2c", "N2d", "O2", "SF6", "SiH4")


def _waferlog_dir(root):
    wafer_dir = os.path.join(root, "WaferLog-Data")
    if not os.path.isdir(wafer_dir):
        raise ToolDataError(f"WaferLog-Data folder not found: {wafer_dir}")
    return wafer_dir


def _sorted_waferlog_files(root):
    wafer_dir = _waferlog_dir(root)
    files = [os.path.join(wafer_dir, f) for f in os.listdir(wafer_dir) if f.lower().endswith(".txt")]
    if not files:
        raise ToolDataError(f"No wafer log files found in: {wafer_dir}")
    return sorted(files, key=os.path.getmtime, reverse=True)


def _waferlog_file_by_run_id(root, run_id):
    wafer_dir = _waferlog_dir(root)
    path = os.path.join(wafer_dir, os.path.basename(run_id))
    if not os.path.isfile(path) or os.path.dirname(os.path.abspath(path)) != os.path.abspath(wafer_dir):
        raise ToolDataError(f"Run not found: {run_id}")
    return path


def _resolve_waferlog_file(root, run_id):
    if run_id:
        return _waferlog_file_by_run_id(root, run_id)
    return _sorted_waferlog_files(root)[0]


def _parse_waferlog(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if not lines:
        raise ToolDataError(f"Wafer log file is empty: {path}")

    machine_id = recipe = start_time = end_time = ""
    duration_s = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("machineID:"):
            for field in line.rstrip("\n").split("\t"):
                field = field.strip()
                if field.startswith("machineID:"):
                    machine_id = field.split(":", 1)[1].strip()
                elif field.startswith("Recipe:"):
                    recipe = field.split(":", 1)[1].strip()
        elif stripped.startswith("Start:"):
            m = re.search(r"Start:\(([^)]+)\)\((\d+)\)\s*End:\(([^)]+)\)\s*\((\d+)\)", stripped)
            if m:
                start_time, end_time = m.group(1).strip(), m.group(3).strip()
                duration_s = (int(m.group(4)) - int(m.group(2))) / 1000.0

    # Step table (used only to find the last step's RF power, for the "was plasma on" flag)
    header_idx = None
    for i, line in enumerate(lines):
        if line.split("\t", 1)[0].strip() == "Step":
            header_idx = i
            break
    steps = []
    if header_idx is not None and header_idx + 1 < len(lines):
        header_fields = [h.strip() for h in lines[header_idx].rstrip("\n").split("\t")]
        unit_fields = [u.strip() for u in lines[header_idx + 1].rstrip("\n").split("\t")]
        colnames, seen = [], {}
        for h, u in zip(header_fields, unit_fields):
            col = "Step" if h == "Step" else (h if u in ("", "()", "(none)", "(Units)") else f"{h} {u}")
            seen[col] = seen.get(col, 0) + 1
            colnames.append(col if seen[col] == 1 else f"{col} #{seen[col]}")
        i = header_idx + 2
        while i < len(lines):
            raw = lines[i].rstrip("\n")
            if raw.strip() == "":
                i += 1
                continue
            if raw.strip().startswith("HistoricalData:"):
                break
            steps.append({name: val.strip() for name, val in zip(colnames, raw.split("\t"))})
            i += 1

    rf_on = False
    for step in steps:
        for key, val in step.items():
            if key.startswith("RFICPGeneratorP") or (key.startswith("RFBiasGenerator") and "(W)" in key):
                try:
                    if float(val) != 0.0:
                        rf_on = True
                except ValueError:
                    pass

    # Endpoint-detector channel history
    hd_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("HistoricalData:"):
            hd_idx = i
            break
    channel_names, channel_units, channel_time, channel_data = [], [], [], []
    if hd_idx is not None and hd_idx + 2 < len(lines):
        name_fields = lines[hd_idx + 1].rstrip("\n").split("\t")
        unit_fields = lines[hd_idx + 2].rstrip("\n").split("\t")
        num_channels = min(len(name_fields), len(unit_fields)) // 2
        channel_names = [name_fields[2 * c].strip() for c in range(num_channels)]
        channel_units = [unit_fields[2 * c + 1].strip() for c in range(num_channels)]
        channel_time = [[] for _ in range(num_channels)]
        channel_data = [[] for _ in range(num_channels)]
        i = hd_idx + 3
        while i < len(lines):
            raw = lines[i].rstrip("\n")
            if raw.strip() == "":
                i += 1
                continue
            if raw.strip().startswith("Process sequence summary"):
                break
            values = raw.split("\t")
            for c in range(num_channels):
                tcol, vcol = 2 * c, 2 * c + 1
                if vcol >= len(values):
                    continue
                traw, vraw = values[tcol].strip(), values[vcol].strip()
                if traw in ("", "---") or vraw in ("", "---", "nil"):
                    continue
                try:
                    channel_time[c].append(float(traw))
                    channel_data[c].append(float(vraw))
                except ValueError:
                    pass
            i += 1

    return {
        "path": path,
        "run_id": os.path.basename(path),
        "machine_id": machine_id,
        "recipe": recipe,
        "mtime": os.path.getmtime(path),
        "duration_s": duration_s,
        "rf_on": rf_on,
        "channel_names": channel_names,
        "channel_units": channel_units,
        "channel_time": channel_time,
        "channel_data": channel_data,
    }


def _waferlog_summary(name, cfg, run_id=None):
    path = _resolve_waferlog_file(cfg["root"], run_id)
    data = _parse_waferlog(path)
    channels = []
    for i, cname in enumerate(data["channel_names"]):
        values = data["channel_data"][i]
        channels.append(
            {
                "name": cname,
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
    path = _resolve_waferlog_file(cfg["root"], run_id)
    data = _parse_waferlog(path)
    series = {}
    for i, cname in enumerate(data["channel_names"]):
        if data["channel_data"][i]:
            series[cname] = ([t / 1000.0 for t in data["channel_time"][i]], data["channel_data"][i])
    title = f"Recipe: {data['recipe'] or '(unknown)'}"
    return title, "Time (s)", "Endpoint Signal", series


def _waferlog_history(cfg, page, page_size):
    all_files = _sorted_waferlog_files(cfg["root"])
    start = (page - 1) * page_size
    history = []
    for path in all_files[start : start + page_size]:
        try:
            data = _parse_waferlog(path)
        except ToolDataError:
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
    return history, len(all_files)


# -------------------- KLA-DSE EventLog (Trikon/SPTS "fxPLPXTMC") --------------------
#
# CurrentEvents.csv is a single, continuously-growing, newest-first log of every module
# state change, command, and fault. A run's run_id is its "DO PROCESS" row's own
# "Date/Time|Module|Info" (guaranteed unique in practice - see mostRecentRun in the
# original Smart-Lab EventLog.py this is adapted from), since there's no simpler natural key.

TERMINATING_EVENTS = {"State Changed To Ready", "State Changed To Idle", "State Changed To Aborted"}
FAULT_KEYWORDS = ("Fault", "Alarm")


def _eventlog_path(root):
    path = os.path.join(root, "EventLog-Data", "CurrentEvents.csv")
    if not os.path.isfile(path):
        raise ToolDataError(f"CurrentEvents.csv not found: {path}")
    return path


def _eventlog_rows(root):
    with open(_eventlog_path(root), encoding="utf-8-sig", errors="replace", newline="") as f:
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


def _eventlog_read_run(root, run_id=None):
    rows = _eventlog_rows(root)

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
            raise ToolDataError(f"No process runs found in: {root}")

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


def _eventlog_summary(name, cfg, run_id=None):
    data = _eventlog_read_run(cfg["root"], run_id)
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
    data = _eventlog_read_run(cfg["root"], run_id)
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


def _eventlog_history(cfg, page, page_size):
    rows = _eventlog_rows(cfg["root"])
    all_runs = list(_eventlog_process_run_indices(rows))
    total = len(all_runs)
    start = (page - 1) * page_size
    history = []
    for idx, run_id in all_runs[start : start + page_size]:
        try:
            data = _eventlog_read_run(cfg["root"], run_id)
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


# -------------------- Public dispatch --------------------

_SUMMARY_FUNCS = {
    "heater_log": _heater_log_summary,
    "mvd": _mvd_summary,
    "cobra_job": _cobra_summary,
    "waferlog": _waferlog_summary,
    "eventlog": _eventlog_summary,
}

# cobra_job and eventlog use their own dedicated chart-data shapes (get_cobra_step_timeline,
# get_eventlog_timeline) instead of the generic (title, x_label, y_label, series) shape here -
# charts.py calls those directly, they're not in this dispatch table.
_CHART_FUNCS = {
    "heater_log": _heater_log_chart_data,
    "mvd": _mvd_chart_data,
    "waferlog": _waferlog_chart_data,
}

_HISTORY_FUNCS = {
    "heater_log": _heater_log_history,
    "mvd": _mvd_history,
    "cobra_job": _cobra_history,
    "waferlog": _waferlog_history,
    "eventlog": _eventlog_history,
}


def get_tool_summary(name, cfg, run_id=None):
    try:
        return _SUMMARY_FUNCS[cfg["kind"]](name, cfg, run_id)
    except ToolDataError as e:
        return {"name": name, "kind": cfg["kind"], "run_id": run_id, "error": str(e)}


def get_chart_data(cfg, run_id=None):
    """Returns (title, x_label, y_label, {series_name: (x_values, y_values)})."""
    return _CHART_FUNCS[cfg["kind"]](cfg, run_id)


def get_tool_history(cfg, page=1, page_size=DEFAULT_HISTORY_LIMIT):
    """
    Returns (runs, total_count) for one page of runs, most recent first, as summary dicts
    (no channel series). Only the runs on the requested page are actually parsed - the full
    list of runs is only stat'd (cheap), not read, so this stays fast regardless of how many
    runs a tool has on disk.
    """
    try:
        return _HISTORY_FUNCS[cfg["kind"]](cfg, page, page_size)
    except ToolDataError:
        return [], 0
