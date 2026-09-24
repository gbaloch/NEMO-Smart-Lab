"""MVD run events (_EVT.txt)."""

import os
import re

from datetime import datetime

from NEMO_smart_lab.readers.common import (
    FAULT_KEYWORDS,
    FILE_ENCODING,
    ToolDataError,
    _cached_file_parse,
    _find_one,
)
from NEMO_smart_lab.readers.mvd.runs import _resolve_mvd_run_dir
from NEMO_smart_lab.readers.run_names import _mvd_folder_timestamp


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
