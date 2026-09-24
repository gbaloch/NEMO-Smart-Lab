"""Parsers for Veeco Fiji/Savannah per-run log files (Heater Data, plus the sibling Pressure Data/RF Data logs)."""

import os
import re

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import (
    EmptyRunFileError,
    FILE_ENCODING,
    ToolDataError,
    _cached_file_parse,
    _has_any_value,
)


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
        raise EmptyRunFileError(f"Heater log file is empty: {path}")

    header = [h.strip() for h in lines[0].split("\t")]
    all_rows = [line.split("\t") for line in lines[1:]]
    if not all_rows:
        raise EmptyRunFileError(f"Heater log file has no data rows: {path}")

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
