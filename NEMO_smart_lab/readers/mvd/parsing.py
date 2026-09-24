"""Low-level parsers for an MVD run's _SUM/_DAT/_PT text files."""

import csv
import re

import numpy as np

from NEMO_smart_lab.readers.common import FILE_ENCODING


_HEATER_LABEL_RE = re.compile(r'^HTR(\d+)\s*=\s*"([^"]*)"', re.MULTILINE)


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
