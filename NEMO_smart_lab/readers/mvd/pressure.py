"""MVD pressure (_PT.txt) chart groups."""

import csv
import os
import re

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.readers.common import FILE_ENCODING, ToolDataError, _cached_file_parse, _find_one
from NEMO_smart_lab.readers.mvd.parsing import _UNIT_SUFFIX_RE, _parse_mvd_pt
from NEMO_smart_lab.readers.mvd.runs import _resolve_mvd_run_dir


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
# own legend, one click away (see js/charts's handling of "default_visible").
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
