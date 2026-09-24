"""Fiji/Savannah per-session Event Files: matching a run to its events and reading them."""

import os
import re

from datetime import datetime, timedelta

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import FAULT_KEYWORDS, FILE_ENCODING, ToolDataError
from NEMO_smart_lab.readers.heater_log.parsing import _parse_heater_log
from NEMO_smart_lab.readers.heater_log.runs import _heater_log_run_end, _resolve_heater_log_file


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


def _heater_log_run_start(data):
    """A run's own start time, computed the same way _heater_log_events_for_data always has -
    factored out so _heater_log_history can determine which session event file a run needs
    (see _candidate_event_file_for_run_start) without re-deriving this itself."""
    run_end = _heater_log_run_end(data)
    return run_end - timedelta(seconds=data["time_s"][-1] if data["time_s"] else 0)


def _candidate_event_file_for_run_start(cfg, run_start):
    """The session event file active when a run starting at `run_start` began - the last one whose
    own filename timestamp is at or before it (see _heater_log_events_for_data's own docstring for
    why). None if no session file qualifies. List-only (_list_event_files is a cached listing, no
    fetch/parse of any event file's own content) - safe to call for a whole page of runs just to
    find out which distinct event files it will need, before actually fetching any of them (see
    _heater_log_history)."""
    candidates = [n for n in _list_event_files(cfg) if (_event_file_timestamp(n) or datetime.max) <= run_start]
    return candidates[-1] if candidates else None


def _heater_log_events_for_data(cfg, data):
    """The actual "find this run's events" work, shared by get_heater_log_run_events (a single
    run, which parses the heater log itself first) and _heater_log_history's per-row alarm count
    (which already has `data` parsed for every row anyway, so this skips re-parsing it).

    Finds the *session* file active when this run started (the last one whose own filename
    timestamp is at or before the run's start - see the module comment above for why that's
    necessary), then filters that session's events down to just this run's own padded window,
    using the run's own authoritative start/end from Heater Data rather than the event file.
    Consecutive runs sharing one session file only pay for one real fetch, not one per run -
    remote_cache.ensure_cached() already caches per remote path (and _heater_log_history prewarms
    every distinct session file a whole page needs concurrently before calling this per-row, so
    even the FIRST run to need a given session file is normally already warm by the time it gets
    here)."""
    run_end = _heater_log_run_end(data)
    run_start = _heater_log_run_start(data)

    event_file_name = _candidate_event_file_for_run_start(cfg, run_start)
    if event_file_name is None:
        return []

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
