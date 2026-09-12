"""
Readers for the raw process-log formats produced by the tools configured in
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
import hashlib
import os
import re
import sqlite3
import struct
import time
import uuid
from datetime import datetime, timedelta

import numpy as np

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.remote_cache import cache

FILE_ENCODING = "latin-1"

DEFAULT_HISTORY_LIMIT = 25

# A single tool_detail page load calls both get_tool_summary() and get_chart_group_list() for the
# same run; each of those parses the same underlying file/folder from scratch independently, and
# every chart tab switch/download-as-image fires yet another separate re-parse on top of that -
# measured live: fiji5's own ~1.1s _mvd_run_data_for_dir() parse, paid twice over on one page load
# alone. Unlike remote_cache's/reservations.py's time-based TTLs (those cache "is this probably
# still true"), this caches "what does parsing this exact, byte-for-byte-unchanged file produce" -
# a pure function of the file's own content - so the cache key itself (see _file_fingerprint)
# encodes each underlying file's mtime+size; a stale hit is structurally impossible; safe to keep
# far longer than any TTL tuned for freshness, only bounded here to cap unbounded cache growth
# across many different runs being browsed over time.
PARSED_FILE_CACHE_TTL = 60 * 60 * 24


def _file_fingerprint(path):
    stat = os.stat(path)
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _cached_file_parse(kind, paths, parser):
    """Memoizes `parser()` (a zero-arg callable - a closure over whatever it actually needs to
    parse) keyed by `kind` plus every path in `paths` together with its own mtime+size - see
    PARSED_FILE_CACHE_TTL. `paths` is every file the parse actually reads (e.g. mvd's _SUM.txt
    *and* _DAT.txt), so the cache is invalidated the instant any of them changes, not just the one
    that happens to be biggest.

    Shares remote_cache's own optionally-persistent "smart_lab" cache alias (see that module's
    docstring) rather than importing Django's plain default cache directly - a deployment that
    configures that alias for a persistent backend gets this cache surviving a restart too, for
    the same reason: a large run's file (some real mvd DAT files here run past 200,000 rows) can
    take real, measurable time to parse from scratch (seconds, not milliseconds - see
    _parse_mvd_dat), so losing this to an ordinary process restart is worth avoiding when a
    deployment cares to."""
    try:
        fingerprint = "|".join(f"{p}:{_file_fingerprint(p)}" for p in paths)
    except OSError:
        # Let the real parser raise its own (more specific) ToolDataError for a missing file,
        # rather than this cache-key bookkeeping doing it first.
        return parser()
    cache_key = "smart_lab:parsed:" + hashlib.sha1(f"{kind}:{fingerprint}".encode()).hexdigest()[:24]
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = parser()
    cache.set(cache_key, result, PARSED_FILE_CACHE_TTL)
    return result


class ToolDataError(Exception):
    """Raised when a tool's configured data source, or a specific run within it, can't be found or parsed."""


def _channel_label(cfg, raw_key):
    """Returns (display_name, role, hidden, on_threshold_c) for a raw channel key, using the
    tool's admin-configured SmartLabToolChannel overrides (cfg["channel_labels"], built by
    SmartLabTool.as_source_config()) if one exists for raw_key, else (raw_key, None, False, None)
    unchanged. hidden=True means this channel is a schema slot that isn't actually wired to
    anything on this particular tool (e.g. always reads a constant 0) - callers should drop it
    entirely rather than display it, however it's named. on_threshold_c, when not None,
    overrides the tool-wide on_threshold_c for just this one channel's "is it on" check (e.g. a
    precursor jacket run at a lower steady-state temperature than the reactor/chuck zones the
    tool-wide threshold is tuned for)."""
    override = (cfg.get("channel_labels") or {}).get(raw_key)
    return override if override else (raw_key, None, False, None)


# -------------------- Veeco Fiji / Savannah "Heater Data" log files --------------------

# A run's filename ("YYYY_MM_DD-HH-MM-SS_<recipe>.txt") embeds its own start time, written once by
# the tool PC when the run began - confirmed live to be the only reliable "when did this run
# actually happen" signal. A file's mtime is NOT that: it reflects whenever the file was last
# written *to whatever filesystem is being read* - for a freshly-fetched local cache that's when it
# was pulled from Oak, and even Oak's own copy can get a fresh mtime if a batch of old data is ever
# re-uploaded/re-synced onto it out of chronological order (confirmed: an old run recently
# re-copied to Oak sorted as "the newest run" by mtime alone, despite having happened long before
# runs whose files hadn't been touched since). Every ordering/"most recent" decision and every
# displayed run-end time below is anchored to this filename timestamp (plus the run's own elapsed
# duration for the *end* time) instead, falling back to mtime only for the rare file whose name
# doesn't match this pattern at all. The trailing "-SS" seconds group is optional - confirmed live
# that savannah's own filenames omit it entirely ("2026_08_25-09-18_clear0.txt", minutes only),
# unlike fiji1/2/3's always-present seconds ("2026_09_10-17-59-31_..."); a real, previously
# unnoticed bug - savannah's filename timestamp never matched this pattern at all before, silently
# falling back to (unreliable, per this whole module's own docstring) mtime for every one of its
# runs.
_HEATER_LOG_FILENAME_RE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})-(\d{2})-(\d{2})(?:-(\d{2}))?_")


def _heater_log_filename_timestamp(name):
    m = _HEATER_LOG_FILENAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) if g is not None else 0 for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


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


def _resolve_heater_log_file(cfg, run_id):
    if run_id:
        return _heater_log_file_by_run_id(cfg, run_id)
    name, _mtime = _list_heater_log_entries(cfg)[0]
    try:
        return _heater_log_local_path(cfg, name)
    except remote_sync.RemoteSyncError as e:
        raise ToolDataError(str(e)) from e


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
    return _cached_file_parse("heater_log", [path], lambda: _parse_heater_log_uncached(path))


def _parse_heater_log_uncached(path):
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
    mfc_idx = row_length - 5  # "MFC 1" - one column before "MFC Time" in _TRAILING_COLUMNS

    def to_float(value):
        try:
            return float(value)
        except (ValueError, IndexError):
            return None

    time_s = []
    channel_series = {name: [] for name in present_heater_names}
    mfc_1_series = []
    recipe = ""
    cycles_remaining = ""
    for row in rows:
        t = to_float(row[time_idx])
        if t is None:
            continue
        time_s.append(t)
        for i, name in enumerate(present_heater_names):
            channel_series[name].append(to_float(row[heater_start_idx + i]))
        mfc_1_series.append(to_float(row[mfc_idx]) if 0 <= mfc_idx < len(row) else None)
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
        "mfc_1_series": mfc_1_series,
        "latest": latest,
    }


_HEATER_LOG_NUM_RE = re.compile(r"(\d+)\s*$")


def _heater_log_channel_num(raw_name, cfg):
    """The physical/recipe channel number for a heater_log raw_name ("Heater 11") - NOT the same
    as the trailing number in raw_name itself (that's the log file's own, tool-internal column
    numbering). Confirmed live (see SmartLabTool.recipe_channel_offset's docstring, and
    recipes._heater_channel_label which does the inverse translation for recipe files) that for
    fiji1/2/3 the log's "Heater 11" is physically/on-the-recipe channel 17, a fixed +6 offset
    (savannah's recipe_channel_offset is 0, so its log numbering already matches directly).
    Showing the raw, un-offset log number here was a real bug - it reads as a plainly wrong
    channel number to anyone comparing against a recipe file or the tool's own physical labeling."""
    m = _HEATER_LOG_NUM_RE.search(raw_name)
    return int(m.group(1)) + cfg.get("recipe_channel_offset", 0) if m else None


def _heater_log_summary(name, cfg, run_id=None):
    path = _resolve_heater_log_file(cfg, run_id)
    data = _parse_heater_log(path)
    default_threshold = cfg.get("on_threshold_c", 35.0)
    channels = []
    for channel, value in sorted(data["latest"].items()):
        raw_name = channel.strip()
        display_name, role, hidden, channel_threshold = _channel_label(cfg, raw_name)
        if hidden:
            continue
        threshold = channel_threshold if channel_threshold is not None else default_threshold
        on = value is not None and value > threshold
        channels.append(
            {
                "name": display_name,
                "raw_name": raw_name,
                "channel_num": _heater_log_channel_num(raw_name, cfg),
                "role": role,
                "latest_value": value,
                "unit": "°C",
                "on": on,
            }
        )
    channels.sort(key=lambda c: c["name"])
    # Same alarm_count _heater_log_history computes per row on the run history page - here it's
    # for this one run's own detail page, replacing the old plain ON/Idle "Status" row (which just
    # duplicated what's already visible in the per-channel table below) with the more useful "how
    # many alarms fired during this run" figure.
    alarm_count = sum(1 for _offset, _module, _text, is_fault in _heater_log_events_for_data(cfg, data) if is_fault)
    return {
        "name": name,
        "kind": "heater_log",
        "run_id": data["run_id"],
        "source_file": data["run_id"],
        "recipe": data["recipe"] or "(unknown)",
        "last_update": _heater_log_run_end(data),
        "channels": channels,
        "any_on": any(c["on"] for c in channels),
        "status_label": "ON" if any(c["on"] for c in channels) else "Idle",
        "status_class": "warning" if any(c["on"] for c in channels) else "success",
        "alarm_count": alarm_count,
        "run_duration_s": data["time_s"][-1] if data["time_s"] else None,
        "extra": {"cycles_remaining": data["cycles_remaining"]},
    }


def _has_any_value(series_dict):
    """series_dict maps name -> (x_values, y_values) - True if any series has a single non-null
    y value anywhere."""
    return any(v is not None for _x_values, y_values in series_dict.values() for v in y_values)


def _parse_simple_run_log(path, value_column_count):
    """Generic parser for the "Pressure Data"/"RF Data" per-run log format - confirmed live,
    identical across fiji1/fiji2/fiji3/savannah, and much simpler than _parse_heater_log's
    variable-heater-count handling since these always have a *fixed* number of value columns:
    leading blank, "<X> Time", `value_column_count` value columns, then the same trailing
    ["Cycles Remaining", "Recipe", "Loop"] block every one of these files has."""
    with open(path, encoding=FILE_ENCODING) as f:
        lines = [line.rstrip("\n").rstrip("\r") for line in f if line.strip()]
    if not lines:
        raise ToolDataError(f"Log file is empty: {path}")
    header = [h.strip() for h in lines[0].split("\t")]
    all_rows = [line.split("\t") for line in lines[1:]]
    if not all_rows:
        raise ToolDataError(f"Log file has no data rows: {path}")

    value_names = header[2 : 2 + value_column_count]
    time_idx = 1
    value_start_idx = 2

    def to_float(value):
        try:
            return float(value)
        except (ValueError, IndexError):
            return None

    time_s = []
    value_series = {name: [] for name in value_names}
    for row in all_rows:
        t = to_float(row[time_idx]) if len(row) > time_idx else None
        if t is None:
            continue
        time_s.append(t)
        for i, name in enumerate(value_names):
            idx = value_start_idx + i
            value_series[name].append(to_float(row[idx]) if idx < len(row) else None)

    return time_s, value_series


def _sibling_run_group(cfg, run_id, subdir, value_column_count, key, label, title, y_label):
    """A run's own timestamp-prefixed file also exists in Logfile/<subdir> - same filename as the
    Heater Data run it belongs to (confirmed live: no fuzzy matching needed, unlike Reports'
    screenshot naming quirk). Returns None (not an error) if that sibling folder/file doesn't
    exist for this run - not every tool/era of data necessarily has every folder."""
    tool = cfg.get("remote_tool")
    try:
        if tool is not None:
            path = remote_cache.ensure_cached(tool, f"Logfile/{subdir}/{run_id}")
        else:
            path = os.path.join(cfg["root"], "Logfile", subdir, run_id)
            if not os.path.isfile(path):
                return None
        time_s, value_series = _parse_simple_run_log(path, value_column_count)
    except (ToolDataError, remote_sync.RemoteSyncError):
        return None

    series = {name.strip(): (time_s, values) for name, values in value_series.items()}
    if not _has_any_value(series):
        return None
    return {"key": key, "label": label, "title": title, "x_label": "Time (s)", "y_label": y_label, "series": series}


def _heater_log_chart_groups(cfg, run_id=None):
    """Every chartable signal a heater_log-kind tool's raw data actually carries for this run:
    per-channel temperature (group 0 - unchanged from before this existed), "MFC 1" flow (sccm),
    and - confirmed live on Oak, a real gap this used to have - two more sibling per-run log
    folders every fiji1/fiji2/fiji3/savannah tool has alongside "Heater Data": "Pressure Data"
    (chamber pressure, Torr - confirmed via Setup.ini.txt's PressGauge*Units) and "RF Data"
    (forward/reflected plasma power, W)."""
    path = _resolve_heater_log_file(cfg, run_id)
    data = _parse_heater_log(path)
    title = f"Recipe: {data['recipe'] or '(unknown)'}"

    temp_series = {}
    for raw_name, values in data["channel_series"].items():
        display_name, _role, hidden, _threshold = _channel_label(cfg, raw_name.strip())
        if hidden:
            continue
        temp_series[display_name] = (data["time_s"], values)

    groups = [{"key": "temperature", "label": "Temperature (°C)", "title": title, "x_label": "Time (s)", "y_label": "Temperature (°C)", "series": temp_series}]

    # Pressure right after temperature (before MFC flow/RF power) - the two signals a viewer
    # checks first for "is this chamber behaving normally", so they shouldn't be split apart by
    # whatever other groups a given run happens to also have.
    pressure_group = _sibling_run_group(cfg, data["run_id"], "Pressure Data", 1, "pressure", "Pressure (Torr)", title, "Pressure (Torr)")
    if pressure_group:
        groups.append(pressure_group)

    mfc_series = {"MFC 1": (data["time_s"], data["mfc_1_series"])}
    if _has_any_value(mfc_series):
        groups.append({"key": "mfc_flow", "label": "MFC Flow (sccm)", "title": title, "x_label": "Time (s)", "y_label": "Flow (sccm)", "series": mfc_series})

    rf_group = _sibling_run_group(cfg, data["run_id"], "RF Data", 2, "rf_power", "RF Power (W)", title, "Power (W)")
    if rf_group:
        groups.append(rf_group)

    return groups


def _heater_log_chart_data(cfg, run_id=None):
    group = _heater_log_chart_groups(cfg, run_id)[0]
    return group["title"], group["x_label"], group["y_label"], group["series"]


# Event Files entries are logged per *program session* (created once each time the tool control
# software is restarted) - confirmed live: a single file held two separate "Run Started"/"Run
# Ended" pairs, and its own filename timestamp only marks when that session began, not any one
# run. Their naming convention ("YYMMDD_HH_MM_SS[.mmm]- Event.txt") also doesn't match Heater
# Data's own filenames ("YYYY_MM_DD-HH-MM-SS_<recipe>.txt") at all, so there's no direct filename
# lookup the way Pressure Data/RF Data have - see get_heater_log_run_events for how a specific
# run's own window is found instead.
_EVENT_FILE_NAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{2})_(\d{2})_(\d{2})(?:\.\d+)?- ?Event\.txt$", re.IGNORECASE)
_EVENT_LINE_RE = re.compile(r"^(\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d+):\s*(.*)$")


def _event_file_timestamp(name):
    m = _EVENT_FILE_NAME_RE.match(name)
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = (int(g) for g in m.groups())
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        return None


def _parse_event_file(path):
    events = []
    with open(path, encoding=FILE_ENCODING) as f:
        for line in f:
            m = _EVENT_LINE_RE.match(line.rstrip("\r\n"))
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%m/%d/%y %H:%M:%S.%f")
            except ValueError:
                continue
            events.append((ts, m.group(2).strip()))
    return events


def _list_event_files(cfg):
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/Logfile/Event Files")
        except remote_sync.RemoteSyncError as e:
            raise ToolDataError(str(e)) from e
        names = [name for name, _mtime, _size, is_dir in entries if not is_dir]
    else:
        event_dir = os.path.join(cfg["root"], "Logfile", "Event Files")
        if not os.path.isdir(event_dir):
            return []
        names = os.listdir(event_dir)
    return sorted(names, key=lambda n: _event_file_timestamp(n) or datetime.min)


# Event timestamps rarely line up exactly with a run's own [start, end] from Heater Data (a
# session's "Run Started"/"Run Ended" markers are written by a different code path on the tool PC
# than the heater log's own last-row timestamp) - widen the window slightly so those markers
# reliably fall inside it.
_EVENT_WINDOW_PAD = timedelta(minutes=1)


def _heater_log_events_for_data(cfg, data):
    """The actual "find this run's events" work, shared by get_heater_log_run_events (a single
    run, which parses the heater log itself first) and _heater_log_history's per-row alarm count
    (which already has `data` parsed for every row anyway, so this skips re-parsing it).

    Finds the *session* file active when this run started (the last one whose own filename
    timestamp is at or before the run's start - see the module comment above for why that's
    necessary), then filters that session's events down to just this run's own padded window,
    using the run's own authoritative start/end from Heater Data rather than the event file.
    Consecutive runs sharing one session file only pay for one real fetch, not one per run -
    remote_cache.ensure_cached() already caches per remote path."""
    run_end = _heater_log_run_end(data)
    run_start = run_end - timedelta(seconds=data["time_s"][-1] if data["time_s"] else 0)

    candidates = [n for n in _list_event_files(cfg) if (_event_file_timestamp(n) or datetime.max) <= run_start]
    if not candidates:
        return []
    event_file_name = candidates[-1]

    tool = cfg.get("remote_tool")
    try:
        if tool is not None:
            event_path = remote_cache.ensure_cached(tool, f"Logfile/Event Files/{event_file_name}")
        else:
            event_path = os.path.join(cfg["root"], "Logfile", "Event Files", event_file_name)
        events = _parse_event_file(event_path)
    except (ToolDataError, remote_sync.RemoteSyncError):
        return []

    window_start, window_end = run_start - _EVENT_WINDOW_PAD, run_end + _EVENT_WINDOW_PAD
    return [
        ((ts - run_start).total_seconds(), "Events", text, any(k in text for k in FAULT_KEYWORDS))
        for ts, text in events
        if window_start <= ts <= window_end
    ]


def get_heater_log_run_events(cfg, run_id=None):
    """Scatter timeline of this run's own events (Program Started/Run Started/Run Ended/faults -
    confirmed live, identical "MM/DD/YY HH:MM:SS.mmm: <text>" format across fiji1/2/3/savannah).
    Returns (title, points) - points shaped like get_eventlog_timeline's, so the exact same
    "scatter" chart_type/renderer already built for the eventlog reader kind works unchanged here."""
    path = _resolve_heater_log_file(cfg, run_id)
    data = _parse_heater_log(path)
    title = f"Events: {data['recipe'] or '(unknown)'}"
    return title, _heater_log_events_for_data(cfg, data)


def _heater_log_history(cfg, page, page_size):
    # Listing every run's name+mtime is cheap (one cached remote listing, or a local stat per
    # file) so it's done over every run; only the one page actually being displayed gets fetched
    # (if remote) and parsed.
    all_entries = _list_heater_log_entries(cfg)
    start = (page - 1) * page_size
    page_entries = all_entries[start : start + page_size]
    tool = cfg.get("remote_tool")
    if tool is not None:
        # Pre-fetch this page's worth of runs concurrently - one at a time would serialize a full
        # SSH round trip per run (~1.5s each against Oak), badly multiplying across a page.
        remote_cache.ensure_cached_many(tool, [f"Logfile/Heater Data/{name}" for name, _mtime in page_entries])
    threshold = cfg.get("on_threshold_c", 35.0)
    history = []
    for name, _mtime in page_entries:
        try:
            path = _heater_log_local_path(cfg, name)
            data = _parse_heater_log(path)
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue
        latest_values = [v for v in data["latest"].values() if v is not None]
        alarm_count = sum(1 for _offset, _module, _text, is_fault in _heater_log_events_for_data(cfg, data) if is_fault)
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["recipe"] or "(unknown)",
                "timestamp": _heater_log_run_end(data),
                "duration_s": data["time_s"][-1] if data["time_s"] else None,
                "any_on": any(v > threshold for v in latest_values),
                "status_label": "ON" if any(v > threshold for v in latest_values) else "Idle",
                "status_class": "warning" if any(v > threshold for v in latest_values) else "success",
                "alarm_count": alarm_count,
                "faulty": alarm_count > 0,
            }
        )
    return history, len(all_entries)


def _screenshot_base_name(run_id):
    base = run_id
    while base.lower().endswith(".txt"):
        base = base[: -len(".txt")]
    return base


def _matches_screenshot_pattern(name, base):
    # Equivalent to the local glob pattern glob.escape(base) + "*.jpg*" below: starts with the
    # run's own base name, contains ".jpg" somewhere after it (covering both the plain ".jpg" and
    # the odd ".jpg.txt" spelling - see the docstring below).
    return name.startswith(base) and ".jpg" in name[len(base) :].lower()


def _heater_log_screenshot_path(cfg, run_id):
    """Best-effort path to a run's post-run report screenshot, if the tool exports one:
    Logfile/Reports/ holds a JPEG per run, named after that run's own base filename - but with an
    inconsistent extra ".txt" suffix on top of ".jpg" for some runs and not others (real JPEG
    bytes either way - confirmed by inspecting real files, not just their names). This mirrors the
    same quirk already seen in some run filenames themselves (a recipe name that already ends in
    ".txt" gets a second ".txt" appended by the heater log export), so matching by the run's own
    base name - with any number of trailing ".txt" stripped - covers both spellings.

    Reports/ is a separate remote folder from Heater Data/, so resolving the run itself doesn't
    fetch a matching report image as a side effect (unlike mvd, where a run's whole folder -
    screenshot included - is fetched as one unit) - this does its own remote listing + fetch of
    just the one matching file when a sync_endpoint is configured."""
    base = _screenshot_base_name(run_id)
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/Logfile/Reports")
        except remote_sync.RemoteSyncError:
            return None
        match = next((name for name, _mtime, _size, is_dir in entries if not is_dir and _matches_screenshot_pattern(name, base)), None)
        if not match:
            return None
        try:
            return remote_cache.ensure_cached(tool, f"Logfile/Reports/{match}")
        except remote_sync.RemoteSyncError:
            return None

    reports_dir = os.path.join(cfg["root"], "Logfile", "Reports")
    if not os.path.isdir(reports_dir):
        return None
    matches = glob.glob(os.path.join(reports_dir, glob.escape(base) + "*.jpg*"))
    return matches[0] if matches else None


# -------------------- Cambridge Nanotech / Veeco MVD run folders --------------------

_HEATER_LABEL_RE = re.compile(r'^HTR(\d+)\s*=\s*"([^"]*)"', re.MULTILINE)


def _mvd_data_dir(root):
    return os.path.join(root, "log", "data")


# A run folder's own name ("YYYYMMDD_HHMMSS_<recipe>", confirmed live for both mvd and fiji5)
# embeds its start time, same reasoning as _HEATER_LOG_FILENAME_RE above: any mtime (the folder's,
# or the DAT file's inside it) reflects whenever it was last *written to whatever filesystem is
# being read*, not necessarily when the run happened - confirmed live elsewhere that a re-upload
# out of chronological order gives a stale run a fresh mtime, which would sort it as "newest".
_MVD_FOLDER_NAME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_")


def _mvd_folder_timestamp(name):
    m = _MVD_FOLDER_NAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


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
_HTR_RAMP_RATE_RE = re.compile(r"^HTR(\d+)_RR\(C\)$")
# Every mvd/fiji5 DAT column not already claimed by one of the three patterns above still ends
# in a parenthesized unit (e.g. "MFC3_setpoint(sccm)", "PlasmaForwardPower(W)") - matched
# generically here rather than hardcoding column names, so a DAT format with more/fewer columns
# (a different mvd-kind tool, a firmware update adding a sensor) keeps working without a code
# change. A column with no parenthesized unit at all (e.g. "xAxisTorque") gets unit=None.
_UNIT_SUFFIX_RE = re.compile(r"^(.+?)\s*\(([^)]+)\)$")


def _parse_mvd_dat(path):
    with open(path, encoding=FILE_ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    time_idx = header.index("Time(sec)") if "Time(sec)" in header else 0
    temp_cols = {}
    duty_cols = {}
    ramp_rate_cols = {}
    other_cols = {}  # base_name -> (unit_or_None, column_index)
    for idx, col in enumerate(header):
        if idx == time_idx:
            continue
        m = _HTR_TEMP_RE.match(col)
        if m:
            temp_cols[m.group(1)] = idx
            continue
        m = _HTR_DUTY_RE.match(col)
        if m:
            duty_cols[m.group(1)] = idx
            continue
        m = _HTR_RAMP_RATE_RE.match(col)
        if m:
            ramp_rate_cols[m.group(1)] = idx
            continue
        m = _UNIT_SUFFIX_RE.match(col)
        other_cols[m.group(1) if m else col] = (m.group(2) if m else None, idx)

    def col_floats_slow(idx):
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

    # Fast path: numpy's own C-level numeric-text reader (np.loadtxt), parsing the whole grid in
    # one pass instead of one Python-level float()-per-cell loop per column - measured live
    # against a real 217,000-row/83-column mvd DAT file (fiji5's largest, and far from a rare
    # outlier - several of its runs' DAT files run past 100,000 rows): ~2.6s down to ~0.4s, and
    # this file alone dominated get_recent_faulty_runs' ~37s scan of 100 runs before this fix.
    # Deliberately re-reads `path` from scratch here rather than reusing `rows` (already parsed by
    # csv.reader above, for `header` and as this fast path's own fallback) - np.loadtxt wants the
    # raw file, and the extra read is a rounding error next to the parse time it avoids paying
    # elsewhere. Only used when the whole file is a clean, uniform numeric grid - np.loadtxt raises
    # on any row of the wrong width or any cell it can't parse as a number, at which point `grid`
    # stays None and every column below falls back to the original per-cell loop, which can never
    # disagree with this fast path since it's the exact same file parsed the exact same way, just
    # one cell at a time instead of all at once - ndmin=2 keeps a single-data-row file from
    # collapsing to a 1-D array, and rows=[] (no data at all) is never handed to np.loadtxt, which
    # doesn't handle that case cleanly either way.
    grid = None
    if rows:
        try:
            grid = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.float64, ndmin=2)
        except ValueError:
            grid = None

    def col_floats(idx):
        if grid is not None:
            return grid[:, idx].tolist()
        return col_floats_slow(idx)

    time_s = col_floats(time_idx)
    temp_series = {num: col_floats(idx) for num, idx in temp_cols.items()}
    duty_series = {num: col_floats(idx) for num, idx in duty_cols.items()}
    ramp_rate_series = {num: col_floats(idx) for num, idx in ramp_rate_cols.items()}
    other_series = {name: (unit, col_floats(idx)) for name, (unit, idx) in other_cols.items()}
    return time_s, temp_series, duty_series, ramp_rate_series, other_series


def _parse_mvd_pt(path):
    """mvd/fiji5's per-run "<timestamp>_PT.txt" - pressure gauge readings, one column per gauge,
    each already carrying its own unit in the header (confirmed live: mvd has a single "Torr"
    gauge, fiji5 has four - three "Torr" and one "psia" (LVPD, its load-lock pressure gauge) - so
    this groups by unit exactly the same way _parse_mvd_dat's other_series does, rather than
    assuming one unit for the whole file). Column 0 is always time, regardless of its exact header
    text ("Time(sec)" for mvd, "Time (sec)" - with a space - for fiji5, confirmed live)."""
    with open(path, encoding=FILE_ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

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

    time_s = col_floats(0)
    series = {}
    for idx, col in enumerate(header[1:], start=1):
        m = _UNIT_SUFFIX_RE.match(col)
        name, unit = (m.group(1), m.group(2)) if m else (col, None)
        series[name] = (unit, col_floats(idx))
    return time_s, series


def _mvd_pt_path(run_dir):
    """None (not an error) if this run has no _PT.txt - not every mvd/fiji5 era of data
    necessarily has one."""
    try:
        return _find_one(run_dir, "_PT.txt")
    except ToolDataError:
        return None


def _mvd_pressure_group_list(cfg, run_id=None):
    """Cheap: reads only the PT.txt header line (a single row, not the - for fiji5 - 145,000+ data
    rows) to discover which pressure-unit tab(s) exist, without paying for a full parse just to
    render the tab strip. Mirrors _sibling_run_group's "sibling file, same run" shape but for
    mvd/fiji5's own PT.txt instead of heater_log's separate Logfile/Pressure Data folder."""
    try:
        run_dir = _resolve_mvd_run_dir(cfg, run_id)
        pt_path = _mvd_pt_path(run_dir)
        if pt_path is None:
            return []
        with open(pt_path, encoding=FILE_ENCODING, newline="") as f:
            header = next(csv.reader(f))
    except (ToolDataError, remote_sync.RemoteSyncError, StopIteration):
        return []

    units = []
    for col in header[1:]:
        m = _UNIT_SUFFIX_RE.match(col)
        unit = m.group(2) if m else "Pressure"
        if unit not in units:
            units.append(unit)
    return [{"key": _mvd_pressure_group_key(unit), "label": f"Pressure ({unit})"} for unit in units]


def _mvd_pressure_group_key(unit):
    return "pressure_" + re.sub(r"[^a-z0-9]+", "_", unit.lower()).strip("_")


# When a pressure group has several gauges (confirmed live: mvd has "Reactor"+"OptKitA", fiji5 has
# "Process"+"Chamber"+"Load Lock") only the main chamber gauge is worth showing by default - the
# rest (a load lock, an option-kit line, etc.) are secondary and just clutter the initial view.
# Checked in priority order since a tool's exact naming varies; the first channel matching the
# highest-priority keyword present wins. Everything stays available - just unchecked on uPlot's
# own legend, one click away (see smart_lab_charts.js's handling of "default_visible").
_MVD_PRIMARY_PRESSURE_KEYWORDS = ("reactor", "process", "chamber")


def _mvd_default_visible_pressure_channel(names):
    lowered = {name: name.lower() for name in names}
    for keyword in _MVD_PRIMARY_PRESSURE_KEYWORDS:
        matches = [name for name, low in lowered.items() if keyword in low]
        if len(matches) == 1:
            return matches[0]
    return None


def get_mvd_pressure_group(cfg, run_id, group_key):
    """The one pressure chart group matching `group_key` (e.g. "pressure_torr") - the actual,
    potentially-expensive full PT.txt parse (cached by content fingerprint - see
    _cached_file_parse) happens here, deliberately kept out of _mvd_chart_groups so listing a
    run's available tabs (_mvd_pressure_group_list, above) never pays for it unless this specific
    tab is actually opened. Raises ToolDataError if there's no PT.txt, or no column matches this
    unit (a stale tab key from a run that no longer has that gauge)."""
    run_dir = _resolve_mvd_run_dir(cfg, run_id)
    pt_path = _mvd_pt_path(run_dir)
    if pt_path is None:
        raise ToolDataError(f"No pressure data (_PT.txt) for this run: {run_dir}")
    time_s, series = _cached_file_parse("mvd_pt", [pt_path], lambda: _parse_mvd_pt(pt_path))

    matching = {name: values for name, (unit, values) in series.items() if _mvd_pressure_group_key(unit) == group_key}
    if not matching:
        raise ToolDataError(f"Unknown pressure group: {group_key}")
    unit = next(unit for name, (unit, _values) in series.items() if name in matching)
    title = f"Run: {os.path.basename(run_dir)}"
    primary = _mvd_default_visible_pressure_channel(matching) if len(matching) > 1 else None
    return {
        "key": group_key,
        "label": f"Pressure ({unit})",
        "title": title,
        "x_label": "Time (s)",
        "y_label": f"Pressure ({unit})",
        "series": {name: (time_s, values) for name, values in matching.items()},
        # Only the primary chamber gauge is checked by default when there's more than one on this
        # tab - see _mvd_default_visible_pressure_channel. None/absent means "show all" (either
        # only one gauge, or none of them matched a recognized "main chamber" keyword).
        "default_visible": [primary] if primary else None,
    }


_MVD_EVT_TIMESTAMP_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})\.(\d+)$")


def _parse_mvd_evt_timestamp(text):
    m = _MVD_EVT_TIMESTAMP_RE.match(text.strip())
    if not m:
        return None
    month, day, year, hour, minute, second, frac = m.groups()
    microsecond = int((frac + "000000")[:6])
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), microsecond)


_MVD_EVT_CATEGORY_RE = re.compile(r"^([A-Z][A-Z0-9_]*);\s*(.*)$")


def _parse_mvd_evt(path):
    """mvd/fiji5's per-run "<timestamp>_EVT.txt" - a plain chronological software/process event
    log (confirmed live: MFC setpoints, heater stability, recipe step transitions, valve/state
    changes - no per-run reservation/session matching needed, unlike heater_log's Event Files,
    since this file already belongs to exactly one run). Its header claims 3 columns for mvd
    ("Date and Time,EventID,EventData") and 4 for fiji5 (adds "Recipe Time (sec)" in the middle) -
    but "EventID" is never actually its own comma-delimited value in a real data row (confirmed
    live: every row's real, physical field count is exactly ONE LESS than its header's column
    count - "EventID" and "EventData" are really just one combined free-text field). Splitting on
    `len(header) - 1` commas (the header's own, literal column count) was a real, confirmed bug:
    for any message that itself contains a comma (routine on fiji5, e.g. "RECIPE; Step #0 -
    Recipe Line #0, instruction action executed: ..."), that split point landed *inside* the
    message and silently truncated everything before that embedded comma. `len(header) - 2` is
    the correct split count - it lands right after the real prefix fields (timestamp, and for
    fiji5 also "Recipe Time (sec)"), leaving the entire rest of the line - embedded commas and
    all - as one intact message.

    Most (not all - confirmed live) events start with an all-caps "<CATEGORY>; " tag of their own
    (STATUS, MFCLOOP, DIGOUT, PLASMA, RECIPE, HTRSTAB, HTRRANG, HTRSEPT, STATE, MFC, DIAGNOSE,
    ENDPT1, PULSE, ...) - pulled out into its own field (falls back to "Event" when a line has no
    such tag) so the UI can show it as its own column and group runs of same-category events
    together, the same "module" shape heater_log's own events already use (there it's always the
    literal string "Events" - heater_log's raw format has no per-line category of its own)."""
    with open(path, encoding=FILE_ENCODING) as f:
        lines = [line.rstrip("\n").rstrip("\r") for line in f if line.strip()]
    if not lines:
        return []
    num_cols = len(lines[0].split(","))
    events = []
    for line in lines[1:]:
        parts = line.split(",", max(num_cols - 2, 1))
        if len(parts) < 2:
            continue
        dt = _parse_mvd_evt_timestamp(parts[0])
        if dt is None:
            continue
        raw_message = parts[-1].strip()
        m = _MVD_EVT_CATEGORY_RE.match(raw_message)
        category, message = (m.group(1), m.group(2)) if m else ("Event", raw_message)
        # This tool family's own vocabulary (confirmed live across months of real event logs)
        # doesn't appear to use "Fault"/"Alarm" text the way heater_log-kind tools' Event Files
        # do - kept for consistency/future-proofing rather than dropped, since a firmware update
        # or a different mvd-kind tool could still use it; simply won't highlight anything today.
        is_fault = any(k in raw_message for k in FAULT_KEYWORDS)
        events.append((dt, category, message, is_fault))
    return events


def get_mvd_run_events(cfg, run_id=None):
    """(title, [(offset_s, category, event_name, is_fault), ...]) for this run's own _EVT.txt -
    same 4-field point shape as get_heater_log_run_events (there "category" is always the literal
    "Events", since heater_log's raw format has no per-line category of its own - see
    _parse_mvd_evt's docstring for what a real category looks like here). Offsets are relative to
    the run's own start time (the folder name's own embedded timestamp - the same anchor
    _mvd_run_end/_list_mvd_run_entries already use, rather than the file's first event, which can
    lag the tool's actual recipe-start moment by a few hundred ms)."""
    run_dir = _resolve_mvd_run_dir(cfg, run_id)
    run_id = os.path.basename(run_dir)
    evt_path = None
    try:
        evt_path = _find_one(run_dir, "_EVT.txt")
    except ToolDataError:
        pass
    if evt_path is None:
        return f"Run: {run_id}", []
    events = _cached_file_parse("mvd_evt", [evt_path], lambda: _parse_mvd_evt(evt_path))
    run_start = _mvd_folder_timestamp(run_id) or datetime.fromtimestamp(os.path.getmtime(evt_path))
    points = [
        ((dt - run_start).total_seconds(), category, message, is_fault) for dt, category, message, is_fault in events
    ]
    return f"Run: {run_id}", points


def _mvd_run_data_for_dir(run_dir):
    # Resolving which two files this run actually has is a cheap directory scan (_find_one) -
    # only the parse of their *contents* (_parse_mvd_dat especially, on a potentially large DAT
    # file) is expensive enough to be worth caching - see _cached_file_parse.
    sum_path = _find_one(run_dir, "_SUM.txt")
    dat_path = _find_one(run_dir, "_DAT.txt")
    return _cached_file_parse("mvd_run", [sum_path, dat_path], lambda: _mvd_run_data_for_dir_uncached(run_dir, sum_path, dat_path))


def _mvd_run_data_for_dir_uncached(run_dir, sum_path, dat_path):
    with open(sum_path, encoding=FILE_ENCODING) as f:
        summary = _parse_mvd_summary_text(f.read())
    time_s, temp_series, duty_series, ramp_rate_series, other_series = _parse_mvd_dat(dat_path)
    return {
        "run_dir": run_dir,
        "run_id": os.path.basename(run_dir),
        "mtime": os.path.getmtime(dat_path),
        "summary": summary,
        "time_s": time_s,
        "temp_series": temp_series,
        "duty_series": duty_series,
        "ramp_rate_series": ramp_rate_series,
        "other_series": other_series,
    }


def _mvd_run_data(cfg, run_id=None):
    return _mvd_run_data_for_dir(_resolve_mvd_run_dir(cfg, run_id))


def _last_non_null(values):
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _mvd_summary(name, cfg, run_id=None):
    data = _mvd_run_data(cfg, run_id)
    threshold = cfg.get("on_threshold_pct", 0.5)
    labels = data["summary"]["heater_labels"]
    channels = []
    for num in sorted(data["temp_series"], key=int):
        auto_label = labels.get(num, "").strip() or f"Heater {num}"
        # An admin-configured override (keyed by the bare HTR number, e.g. "6") wins over the
        # auto-parsed _SUM.txt label; _channel_label falls back to returning `num` itself
        # unchanged when there's no override, in which case fall back further to auto_label.
        display_name, role, hidden, _threshold = _channel_label(cfg, num)
        if hidden:
            continue
        if display_name == num:
            display_name = auto_label
        latest_temp = _last_non_null(data["temp_series"][num])
        latest_duty = _last_non_null(data["duty_series"].get(num, []))
        on = latest_duty is not None and latest_duty > threshold
        channels.append(
            {
                "name": display_name,
                # Shown as its own "#" column on the detail page (not folded into the name/role
                # subtitle text) - e.g. "Chamber" / "HTR14", not "Chamber (HTR14)". mvd's own HTR
                # numbering already matches the physical/recipe channel number directly (unlike
                # heater_log-kind tools - see _heater_log_channel_num), so channel_num needs no
                # offset here.
                "raw_name": f"HTR{num}",
                "channel_num": int(num),
                "role": role,
                "latest_value": latest_temp,
                "unit": "°C",
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
        "last_update": _mvd_run_end(data),
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


_MVD_UNIT_GROUP_LABELS = {
    "sccm": "Flow (sccm)",
    "W": "Power (W)",
    "rpm": "Speed (rpm)",
    "V": "Voltage (V)",
    "%": "Percent (other)",
}


def _mvd_chart_groups(cfg, run_id=None):
    """Every chartable signal an mvd-kind DAT file actually carries - not just heater
    temperature. mvd's own DAT format (mvd/fiji5) already encodes each column's physical unit
    in its header (e.g. "(sccm)", "(W)"), so unlike heater_log this classifies columns generically
    by that unit rather than needing per-tool column names hardcoded - see _UNIT_SUFFIX_RE."""
    data = _mvd_run_data(cfg, run_id)
    labels = data["summary"]["heater_labels"]
    title = f"Recipe: {data['summary']['recipe'] or '(unknown)'}"
    time_s = data["time_s"]

    def htr_label(num):
        auto_label = labels.get(num, "").strip() or f"Heater {num}"
        display_name, _role, hidden, _threshold = _channel_label(cfg, num)
        if hidden:
            return None
        if display_name == num:
            display_name = auto_label
        return f"{display_name} (HTR{num})"

    def htr_group(key, label, y_label, source, name_suffix=""):
        series = {}
        for num, values in source.items():
            channel_label = htr_label(num)
            if channel_label:
                series[f"{channel_label}{name_suffix}"] = (time_s, values)
        return {"key": key, "label": label, "title": title, "x_label": "Time (s)", "y_label": y_label, "series": series}

    groups = [htr_group("temperature", "Temperature (°C)", "Temperature (°C)", data["temp_series"])]
    duty_group = htr_group("duty", "Heater duty (%)", "Duty (%)", data["duty_series"])
    if _has_any_value(duty_group["series"]):
        groups.append(duty_group)
    ramp_rate_group = htr_group("ramp_rate", "Heater ramp rate (°C)", "Ramp rate (°C)", data["ramp_rate_series"], " ramp rate")
    if _has_any_value(ramp_rate_group["series"]):
        groups.append(ramp_rate_group)

    # Non-heater columns, bucketed by their own raw unit (a bare "%" here - e.g. a match-network
    # Load/Tune reading - is kept separate from "Heater duty (%)" above, since the two percentages
    # mean different things and shouldn't share an axis).
    by_unit = {}
    for name, (unit, values) in data["other_series"].items():
        by_unit.setdefault(unit, {})[name] = (time_s, values)

    for unit in ("sccm", "W", "rpm", "V", "%"):
        series = by_unit.pop(unit, None)
        if series and _has_any_value(series):
            label = _MVD_UNIT_GROUP_LABELS[unit]
            key = "percent_other" if unit == "%" else unit
            groups.append({"key": key, "label": label, "title": title, "x_label": "Time (s)", "y_label": label, "series": series})

    leftover = {name: values for series in by_unit.values() for name, values in series.items()}
    if leftover and _has_any_value(leftover):
        groups.append({"key": "other", "label": "Other", "title": title, "x_label": "Time (s)", "y_label": "Value", "series": leftover})

    return groups


def _mvd_chart_data(cfg, run_id=None):
    group = _mvd_chart_groups(cfg, run_id)[0]
    return group["title"], group["x_label"], group["y_label"], group["series"]


def _mvd_history(cfg, page, page_size):
    # Listing every run folder's name+mtime is cheap (one cached remote listing, or a local stat
    # per folder) so it's done over every run; only the one page actually being displayed gets
    # fetched (if remote) and parsed.
    all_entries = _list_mvd_run_entries(cfg)
    start = (page - 1) * page_size
    page_entries = all_entries[start : start + page_size]
    tool = cfg.get("remote_tool")
    if tool is not None:
        remote_cache.ensure_cached_many(tool, [f"log/data/{name}" for name, _mtime in page_entries], is_dir=True)
    threshold = cfg.get("on_threshold_pct", 0.5)
    history = []
    for name, _mtime in page_entries:
        try:
            run_dir = _mvd_run_local_path(cfg, name)
            data = _mvd_run_data_for_dir(run_dir)
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue
        latest_duties = [_last_non_null(v) for v in data["duty_series"].values()]
        latest_duties = [v for v in latest_duties if v is not None]
        # mvd/fiji5 have no alarm log wired into the history listing the way heater_log does -
        # its own _SUM.txt completion_status is the equivalent real signal (confirmed live across
        # two real tools: mvd itself says "Successfully completed" / "Recipe stopped - Manual
        # stop", while fiji5 - a different mvd-kind instance, evidently a different software
        # version - says "Successfully Completed" with a capital C; a real bug, caught live, was
        # comparing this case-sensitively against only the lowercase spelling, which flagged every
        # single one of fiji5's normal, successful runs as "faulty") - anything other than a clean
        # completion (case-insensitively) counts as "faulty" for the tool detail page's
        # recent-problems panel (get_recent_faulty_runs).
        completion_status = data["summary"]["completion_status"]
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["summary"]["recipe"] or "(unknown)",
                "timestamp": _mvd_run_end(data),
                "duration_s": data["time_s"][-1] if data["time_s"] else None,
                "any_on": any(v > threshold for v in latest_duties),
                "status_label": "ON" if any(v > threshold for v in latest_duties) else "Idle",
                "status_class": "warning" if any(v > threshold for v in latest_duties) else "success",
                "completion_status": completion_status,
                "faulty": bool(completion_status) and completion_status.strip().lower() != "successfully completed",
            }
        )
    return history, len(all_entries)


def _mvd_screenshot_path(run_dir):
    """A run folder holds one JPEG screenshot of the tool software's post-run report, alongside
    its *_SUM.txt/*_DAT.txt, named after the run's own timestamp prefix (e.g.
    "20260901_122212.jpg" inside "20260901_122212_Plasma Clean.../"). Not every run necessarily
    has one (older recipes/aborted runs may not), so this returns None rather than raising."""
    try:
        return _find_one(run_dir, ".jpg")
    except ToolDataError:
        return None


# -------------------- Oxford Instruments PlasmaPro 100 Cobra (PTIQ Jobs.db) --------------------
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


def _cobra_history(cfg, page, page_size):
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


def _waferlog_history(cfg, page, page_size):
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


# -------------------- KLA-DSE EventLog (Trikon/SPTS "fxPLPXTMC") --------------------
#
# CurrentEvents.csv is a single, continuously-growing, newest-first log of every module
# state change, command, and fault. A run's run_id is its "DO PROCESS" row's own
# "Date/Time|Module|Info" (guaranteed unique in practice - see mostRecentRun in the
# original Smart-Lab EventLog.py this is adapted from), since there's no simpler natural key.

TERMINATING_EVENTS = {"State Changed To Ready", "State Changed To Idle", "State Changed To Aborted"}
FAULT_KEYWORDS = ("Fault", "Alarm")


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


def _eventlog_history(cfg, page, page_size):
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

def _single_chart_group(chart_func, cfg, run_id=None):
    title, x_label, y_label, series = chart_func(cfg, run_id)
    return [{"key": "primary", "label": y_label, "title": title, "x_label": x_label, "y_label": y_label, "series": series}]


# heater_log/mvd expose every chartable signal a run's raw file actually carries (temperature,
# duty cycle, flow, power, ...) as a list of independent chart groups instead of just one. No
# currently-configured tool is waferlog kind, so there's no real data to design multi-group
# support against - it stays a single-group wrapper around its existing chart data.
_CHART_GROUP_FUNCS = {
    "heater_log": _heater_log_chart_groups,
    "mvd": _mvd_chart_groups,
    "waferlog": lambda cfg, run_id=None: _single_chart_group(_waferlog_chart_data, cfg, run_id),
}

_HISTORY_FUNCS = {
    "heater_log": _heater_log_history,
    "mvd": _mvd_history,
    "cobra_job": _cobra_history,
    "waferlog": _waferlog_history,
    "eventlog": _eventlog_history,
}


def _mark_shared_roles(channels):
    """A channel's "role" (chuck/source_valve/delivery_line/...) is shown on the detail page as a
    small subtitle below its own name, e.g. "Cone" / "Chuck" - useful when it tells you Cone and
    another channel are both part of the same physical "Chuck" assembly. But when only one channel
    on the whole tool has a given role, the subtitle just repeats information the channel's own
    name/position already conveys (or duplicates the name outright, e.g. a channel literally named
    "Chuck" with role "chuck"). This doesn't touch "role" itself (still the real classification,
    used elsewhere e.g. per-channel on_threshold_c overrides) - it adds "role_shown", a display-only
    flag the template checks instead, true only when at least one other channel shares the role."""
    role_counts = {}
    for c in channels:
        if c.get("role") and c["role"] != "other":
            role_counts[c["role"]] = role_counts.get(c["role"], 0) + 1
    for c in channels:
        c["role_shown"] = bool(c.get("role")) and c["role"] != "other" and role_counts.get(c["role"], 0) > 1
    return channels


def get_tool_summary(name, cfg, run_id=None):
    try:
        summary = _SUMMARY_FUNCS[cfg["kind"]](name, cfg, run_id)
        if summary.get("channels"):
            _mark_shared_roles(summary["channels"])
        return summary
    except ToolDataError as e:
        return {"name": name, "kind": cfg["kind"], "run_id": run_id, "error": str(e)}


def get_run_screenshot(cfg, run_id=None):
    """Local filesystem path to a run's post-run report screenshot (heater_log's Reports/*.jpg,
    mvd's per-run-folder *.jpg), or None if this tool kind doesn't have one, the run itself can't
    be resolved, or no screenshot exists for that particular run."""
    try:
        if cfg["kind"] == "heater_log":
            path = _resolve_heater_log_file(cfg, run_id)
            return _heater_log_screenshot_path(cfg, os.path.basename(path))
        if cfg["kind"] == "mvd":
            # A run's whole folder (including its .jpg, if any) is fetched as one unit by
            # _resolve_mvd_run_dir in remote mode, so this works automatically either way.
            return _mvd_screenshot_path(_resolve_mvd_run_dir(cfg, run_id))
    except (ToolDataError, remote_sync.RemoteSyncError):
        return None
    return None


def _recipe_from_run_id(cfg, run_id):
    """The recipe name embedded in a run's own filename/foldername, stripped of its timestamp
    prefix (heater_log: "YYYY_MM_DD-HH-MM-SS_<recipe>.txt") or (mvd: "YYYYMMDD_HHMMSS_<recipe>") -
    cheap (no fetch/parse needed) name-only extraction, used by get_base_pressure_history to find
    matching runs before ever touching their actual data."""
    if cfg["kind"] == "heater_log":
        recipe = _HEATER_LOG_FILENAME_RE.sub("", run_id)
        while recipe.lower().endswith(".txt"):
            recipe = recipe[: -len(".txt")]
        return recipe
    if cfg["kind"] == "mvd":
        return _MVD_FOLDER_NAME_RE.sub("", run_id)
    return run_id


# get_base_pressure_history has no cap on how many *runs* it covers (see its own docstring) - but
# fetching a tool's entire multi-year backlog of never-before-seen files over one SSH connection,
# synchronously, inside a single HTTP request, is a different problem: confirmed live against
# fiji2/fiji3/mvd, each with well over a thousand matching historical runs and a real ~1.5s round
# trip per file not yet fetched, that this turns "open the chart for the first time" into a
# multi-minute hang. So the fetch itself - not the result - is what's bounded per request: only
# this many genuinely-missing files are fetched inline; the rest are handed to a background warm
# (remote_cache.warm_in_background) that keeps making progress across the next several page loads
# without ever blocking one. Already-local files (the overwhelming majority after the first
# request or two) are never subject to this cap - see remote_cache.ensure_cached_many's own local-
# existence fast path.
_MAX_SYNCHRONOUS_FETCHES_PER_REQUEST = 30


def _bounded_prewarm(cfg, tool, remote_relpaths, key, is_dir=False):
    missing = [p for p in remote_relpaths if not os.path.exists(os.path.join(tool.local_root, p))]
    remote_cache.ensure_cached_many(tool, missing[:_MAX_SYNCHRONOUS_FETCHES_PER_REQUEST], is_dir=is_dir)
    rest = missing[_MAX_SYNCHRONOUS_FETCHES_PER_REQUEST :]
    if rest:
        remote_cache.warm_in_background(tool, f"{key}:{cfg['kind']}", rest, is_dir=is_dir)


def get_base_pressure_history(cfg, window_s=10.0):
    """Chamber base pressure over time: for every run of any of this tool's configured, *exact*
    standby recipe(s) (SmartLabTool.base_pressure_recipe_names - deliberately a set of specific
    recipes, not a keyword, since a genuinely unrelated recipe that merely contains "standby" in
    its name could have a very different baseline pressure - see that field's docstring; several
    *real* standby variants can legitimately share this list, confirmed live that more than one
    can end in the same long settle-then-measure wait step this feature depends on - see
    recipes.suggest_base_pressure_recipes for auto-discovering which ones), averages the last
    `window_s` seconds of its pressure data (the tail of that wait step, once it's had time to
    actually settle) and returns that one number per run - a simple, real proxy for "is this
    chamber's base vacuum drifting over time" (a slow leak, a dirtying O-ring, etc. shows up as a
    rising trend here long before it's obvious from any single run).

    Returns [{"timestamp": datetime, "value": float, "unit": str, "run_id": str}, ...] oldest
    first (ready to plot left-to-right), covering every matching run in this tool's whole history
    (no cap - each file's own parse is cached by content fingerprint, so repeat page loads are
    cheap regardless of how many runs match; only the very first computation for a tool with a
    long history pays the full one-time cost of visiting each of them). Returns [] (not an error)
    if this tool has no base_pressure_recipe_names configured, or no run at all matches - this is
    an opt-in, deliberately-configured feature, not something every tool is expected to have wired
    up.

    Matches runs by their filename/foldername's own embedded recipe name (cheap - no fetch/parse
    at all for the runs that don't match) rather than each run's parsed-from-content recipe field,
    so this never pays to open a run's data just to find out it isn't a relevant one."""
    raw_targets = cfg.get("base_pressure_recipe_names")
    if not raw_targets or cfg["kind"] not in ("heater_log", "mvd"):
        return []
    targets = {t.strip().lower() for t in raw_targets.split(",") if t.strip()}
    if not targets:
        return []

    if cfg["kind"] == "heater_log":
        all_entries = _list_heater_log_entries(cfg)
        matching = [name for name, _mtime in all_entries if _recipe_from_run_id(cfg, name).strip().lower() in targets]
        tool = cfg.get("remote_tool")
        if tool is not None:
            _bounded_prewarm(cfg, tool, [f"Logfile/Pressure Data/{name}" for name in matching], "base_pressure")
        results = []
        for name in matching:
            try:
                path = (
                    remote_cache.ensure_cached(tool, f"Logfile/Pressure Data/{name}")
                    if tool is not None
                    else os.path.join(cfg["root"], "Logfile", "Pressure Data", name)
                )
                time_s, value_series = _parse_simple_run_log(path, value_column_count=1)
            except (ToolDataError, remote_sync.RemoteSyncError):
                continue
            if not time_s:
                continue
            pressure_name = next(iter(value_series))
            avg = _average_tail(time_s, value_series[pressure_name], window_s)
            if avg is None:
                continue
            run_end = (_heater_log_filename_timestamp(name) or datetime.fromtimestamp(0)) + timedelta(seconds=time_s[-1])
            results.append({"timestamp": run_end, "value": avg, "unit": "Torr", "run_id": name})
        results.sort(key=lambda r: r["timestamp"])
        return results

    # mvd/fiji5
    all_entries = _list_mvd_run_entries(cfg)
    matching = [name for name, _mtime in all_entries if _recipe_from_run_id(cfg, name).strip().lower() in targets]
    tool = cfg.get("remote_tool")
    if tool is not None:
        _bounded_prewarm(cfg, tool, [f"log/data/{name}" for name in matching], "base_pressure", is_dir=True)
    results = []
    for name in matching:
        try:
            run_dir = _mvd_run_local_path(cfg, name)
            pt_path = _mvd_pt_path(run_dir)
            if pt_path is None:
                continue
            time_s, series = _cached_file_parse("mvd_pt", [pt_path], lambda p=pt_path: _parse_mvd_pt(p))
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue
        if not time_s or not series:
            continue
        channel_names = {n: u for n, (u, _v) in series.items()}
        primary = _mvd_default_visible_pressure_channel(channel_names) or next(iter(series), None)
        if primary is None:
            continue
        unit, values = series[primary]
        avg = _average_tail(time_s, values, window_s)
        if avg is None:
            continue
        run_end = (_mvd_folder_timestamp(name) or datetime.fromtimestamp(0)) + timedelta(seconds=time_s[-1])
        results.append({"timestamp": run_end, "value": avg, "unit": unit or "", "run_id": name})
    results.sort(key=lambda r: r["timestamp"])
    return results


def get_latest_run_id(cfg):
    """This tool's own most recent run's id (filename/foldername) - a cheap, listing-only lookup
    (no fetch/parse). Used by the tool detail page to tell whether an explicit ?run=<id> happens
    to be the tool's actual latest run (reached via the overview page's own "View full details"
    link) rather than a genuinely earlier one, so the "Viewing a past run" banner isn't shown for
    it. None if this tool kind has no linear per-run list at all (only heater_log/mvd do -
    cobra_job/eventlog are single, continuously-growing files), or it currently has zero runs (the
    underlying listing raises ToolDataError for "no runs at all", swallowed here into None since a
    tool with nothing to show yet isn't a real error for this particular check)."""
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return None
    except ToolDataError:
        return None
    return entries[0][0] if entries else None


def get_run_page_number(cfg, run_id, page_size):
    """Which page of get_tool_history(cfg, page_size=page_size) this specific run_id falls on
    (1-indexed) - a cheap, listing-only lookup (no fetch/parse), same shape as get_latest_run_id.
    Used by the "View run history" link on a past run's own detail page, so it jumps straight to
    the page that run is actually on instead of always landing on page 1 and leaving the viewer to
    go hunting for it. None if this tool kind has no linear per-run list at all, or run_id isn't
    found in it (e.g. a stale/bad ?run= value)."""
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return None
    except ToolDataError:
        return None
    names = [name for name, _mtime in entries]
    try:
        index = names.index(run_id)
    except ValueError:
        return None
    return index // page_size + 1


def get_recent_faulty_runs(cfg, scan_limit=30, limit=5):
    """The most recent runs (out of this tool's `scan_limit` most recent, not its whole history -
    a bounded, cheap-enough window) that show a real sign of trouble - an alarm for heater_log-kind
    tools, or a non-"Successfully completed" completion status for mvd/fiji5 (see
    _heater_log_history/_mvd_history's own "faulty" field for exactly what counts) - for the tool
    detail page's own "recent problems" panel. Returns up to `limit` of them, newest first.

    `scan_limit` was 100 - measured live against fiji5 (real mvd DAT files run past 200,000 rows
    there), each unparsed run in the scan window costs a real, non-trivial parse (even after
    _parse_mvd_dat's own numpy fast path - see its docstring), and this panel only needs enough of
    a "recent" window to be a useful at-a-glance signal, not an exhaustive audit (the full history
    is one click away on /history/) - 30 keeps that same "recent window" spirit while keeping a
    cold-cache tool overview page load from being dominated by this one panel.
    [] (not an error) for any kind without this concept, or a tool with no faulty runs in the
    scanned window."""
    if cfg["kind"] not in ("heater_log", "mvd"):
        return []
    runs, _total = get_tool_history(cfg, page=1, page_size=scan_limit)
    return [r for r in runs if r.get("faulty")][:limit]


def get_recent_runs(cfg, limit=5):
    """This tool's `limit` most recent runs (any status, not just faulty ones - see
    get_recent_faulty_runs for that), newest first - for the tool overview page's own "Recent
    runs" panel, shown side by side with "Recently updated recipes" in the same shape (a short
    linked list with a muted timestamp underneath each entry). [] (not an error) for any kind
    without this concept."""
    if cfg["kind"] not in ("heater_log", "mvd"):
        return []
    runs, _total = get_tool_history(cfg, page=1, page_size=limit)
    return runs[:limit]


def _average_tail(time_s, values, window_s):
    """Mean of whichever values fall in the last `window_s` seconds of a run's own time_s array -
    None (not 0) if that window has no real (non-null) readings at all, so a run with no usable
    tail data is skipped entirely rather than plotted as a misleading zero."""
    if not time_s:
        return None
    cutoff = time_s[-1] - window_s
    tail = [v for t, v in zip(time_s, values) if t >= cutoff and v is not None]
    return sum(tail) / len(tail) if tail else None


def get_chart_data(cfg, run_id=None):
    """Returns (title, x_label, y_label, {series_name: (x_values, y_values)}) - always exactly
    get_chart_groups()[0] (the Temperature group), kept as its own function for every existing
    caller that only ever wanted the one chart this used to be the only option."""
    return _CHART_FUNCS[cfg["kind"]](cfg, run_id)


def get_chart_groups(cfg, run_id=None):
    """Every independent chart this tool's raw data supports for this run - group 0 is always
    exactly what get_chart_data() returns. cobra_job/eventlog aren't in here at all (gantt/scatter
    aren't part of this "line chart groups" concept - charts.py calls their dedicated functions
    directly, same as get_chart_data never covered them either)."""
    return _CHART_GROUP_FUNCS[cfg["kind"]](cfg, run_id)


def get_chart_group_list(cfg, run_id=None):
    """[{"key", "label"}, ...] - the cheap part of get_chart_groups(), for a caller (views.py's
    tool_detail) that only needs to know how many chart panels to render and their keys/labels,
    not ship every group's full series data up front.

    cobra_job/eventlog (gantt/scatter, not part of the "line chart groups" concept - see
    get_chart_groups) get a single placeholder entry instead, so a template that renders one
    <canvas> per entry still renders exactly the one canvas those kinds have always had, with an
    empty group key that get_chart_json/render_chart_png's group_key param already treats as
    "use the default" (both kinds ignore group_key entirely, same as before groups existed).

    Swallows ToolDataError the same way get_tool_history() does - a caller building a page around
    an already-successfully-fetched summary shouldn't have this one extra, purely-cosmetic parse
    take the whole page down if it happens to fail; it just renders zero chart panels instead."""
    if cfg["kind"] not in _CHART_GROUP_FUNCS:
        return [{"key": "", "label": "Chart"}]
    try:
        groups = [{"key": g["key"], "label": g["label"]} for g in get_chart_groups(cfg, run_id)]
    except ToolDataError:
        return []

    # "Events" (see get_heater_log_run_events/get_mvd_run_events) is a scatter timeline/plain
    # list, not a line-series group, so it deliberately isn't part of
    # get_chart_groups()/_CHART_GROUP_FUNCS above - charts.py special cases this one key the same
    # way it already special cases cobra_job/eventlog's chart types.
    if cfg["kind"] == "heater_log":
        try:
            _title, points = get_heater_log_run_events(cfg, run_id)
        except ToolDataError:
            points = []
        if points:
            groups.append({"key": "events", "label": "Events"})
    elif cfg["kind"] == "mvd":
        # Pressure (_PT.txt) - listed cheaply (header-only, see _mvd_pressure_group_list) so this
        # never pays for a full parse of what can be a 100,000+ row file just to render the tab
        # strip; the real parse only happens if/when that tab is actually opened
        # (get_mvd_pressure_group). Inserted right after "Temperature" (group 0 is always that -
        # see _mvd_chart_groups) rather than appended at the end, same "pressure belongs right
        # next to temperature" placement as heater_log's own _heater_log_chart_groups.
        groups[1:1] = _mvd_pressure_group_list(cfg, run_id)
        # Events (_EVT.txt) - already fetched locally as part of this run's folder sync either
        # way (see _mvd_run_local_path), and typically small enough that checking for content here
        # isn't worth special-casing further - the parse is cached, so this doesn't duplicate work
        # once the tab is actually opened.
        try:
            _title, points = get_mvd_run_events(cfg, run_id)
        except ToolDataError:
            points = []
        if points:
            groups.append({"key": "events", "label": "Events"})

    return groups


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
