"""Weekly maintenance/health trends (fault rate, pump-down time, MFC drift, RF health, turbo speed) and recent-run lists."""

import re

from datetime import date

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError, _average_tail, _cached_file_parse, _find_one
from NEMO_smart_lab.readers.dispatch import get_tool_history
from NEMO_smart_lab.readers.heater_log.parsing import _sibling_run_group
from NEMO_smart_lab.readers.mvd.data import _mvd_run_data, _mvd_run_data_for_dir
from NEMO_smart_lab.readers.mvd.parsing import _parse_mvd_pt
from NEMO_smart_lab.readers.mvd.pressure import _mvd_default_visible_pressure_channel, _mvd_pt_path
from NEMO_smart_lab.readers.mvd.runs import _list_mvd_run_entries, _resolve_mvd_run_dir
from NEMO_smart_lab.readers.run_names import _mvd_folder_timestamp


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


def get_fault_rate_trend(cfg, scan_limit=300):
    """Weekly fault rate over this tool's `scan_limit` most recent runs (not its whole history -
    same bounded-scan reasoning as get_recent_faulty_runs, just a larger window since this is a
    page a user visits deliberately to look for a trend, not something loaded on every overview
    page view) - a rolling "faults per week" signal that's genuinely cheap to compute *given* the
    scan already happened, since "faulty" is metadata get_tool_history's own per-run parse already
    produces (an alarm count for heater_log, a non-"Successfully completed" completion_status for
    mvd/fiji5 - see _heater_log_history/_mvd_history) - this just buckets and counts it, no
    additional fetch/parse of its own.

    Returns [{"week_start": date, "total_runs": int, "faulty_runs": int, "fault_rate": float
    (0-100)}, ...] sorted oldest week first (ready to plot left-to-right) - only weeks that
    actually have at least one run in the scanned window are included, so a long-idle stretch
    doesn't show as a misleading "0% faults" week. [] for any kind without this concept, or a tool
    with no run history at all."""
    if cfg["kind"] not in ("heater_log", "mvd"):
        return []
    # mvd/fiji5's own files are far more expensive per run than heater_log's (see
    # _MVD_SIGNAL_SCAN_LIMIT's own docstring) - get_tool_history's per-run summary for an mvd-kind
    # tool still means a full DAT parse each, so this default of 300 (fine for heater_log) would
    # otherwise turn this one call alone into the dominant cost of the whole maintenance trends
    # page (confirmed live: get_mvd_maintenance_trends' own scan_limit=60 was already applied
    # everywhere else on that page, but this function - called unconditionally, for every kind -
    # was still defaulting to 300 mvd runs on top of it).
    if cfg["kind"] == "mvd":
        scan_limit = min(scan_limit, _MVD_SIGNAL_SCAN_LIMIT)
    runs, _total = get_tool_history(cfg, page=1, page_size=scan_limit)
    buckets = {}
    for run in runs:
        timestamp = run.get("timestamp")
        if timestamp is None:
            continue
        year, week, _weekday = timestamp.isocalendar()
        week_start = date.fromisocalendar(year, week, 1)
        bucket = buckets.setdefault(week_start, {"total_runs": 0, "faulty_runs": 0})
        bucket["total_runs"] += 1
        if run.get("faulty"):
            bucket["faulty_runs"] += 1
    trend = [
        {
            "week_start": week_start,
            "total_runs": bucket["total_runs"],
            "faulty_runs": bucket["faulty_runs"],
            "fault_rate": 100.0 * bucket["faulty_runs"] / bucket["total_runs"],
        }
        for week_start, bucket in buckets.items()
    ]
    trend.sort(key=lambda entry: entry["week_start"])
    return trend


# mvd/fiji5's DAT files are the largest single files this whole module ever parses (confirmed live
# past 200,000 rows/100,000+ runs of history) - a per-run scan for these trends costs real time
# even with _parse_mvd_dat's numpy fast path, so this stays well under get_fault_rate_trend's
# heater_log-safe default (that scan only needs each run's already-cached *summary*, never its
# full series; these need the full series every time).
_MVD_SIGNAL_SCAN_LIMIT = 60


def _mvd_weekly_signal_trend(cfg, scan_limit, extract):
    """Shared scanning engine for every mvd-kind "trend a raw signal over weeks" feature (MFC
    setpoint-vs-reading drift, RF match-network health, turbo pump speed) - scans this tool's
    `scan_limit` most recent runs, calls `extract(run_data)` (the same dict _mvd_run_data returns
    for one run) for each to pull out a single float, or None to skip a run that doesn't have this
    particular signal at all, then averages whatever's left by week.

    Returns [{"week_start": date, "value": float, "run_count": int}, ...] oldest week first -
    `run_count` is how many runs actually contributed a value that week (not every run necessarily
    has this signal), so a caller can tell a week's average apart from one based on a single fluke
    run. [] for a non-mvd-kind tool, or no run in the scanned window has this signal at all."""
    if cfg["kind"] != "mvd":
        return []
    runs, _total = get_tool_history(cfg, page=1, page_size=scan_limit)
    buckets = {}
    for run in runs:
        if run.get("timestamp") is None:
            continue
        try:
            run_data = _mvd_run_data(cfg, run["run_id"])
        except ToolDataError:
            continue
        value = extract(run_data)
        if value is None:
            continue
        year, week, _weekday = run["timestamp"].isocalendar()
        week_start = date.fromisocalendar(year, week, 1)
        buckets.setdefault(week_start, []).append(value)
    trend = [
        {"week_start": week_start, "value": sum(values) / len(values), "run_count": len(values)}
        for week_start, values in buckets.items()
    ]
    trend.sort(key=lambda entry: entry["week_start"])
    return trend


_MFC_SETPOINT_RE = re.compile(r"^MFC(\d+)_setpoint$")


_MFC_READING_RE = re.compile(r"^MFC(\d+)_reading$")


def _extract_mfc_drift(run_data):
    """Average |setpoint - reading| across every MFC channel that has both a setpoint and a
    reading column (fiji5's own DAT format - see _parse_mvd_dat; mvd's own single "MFC0(sccm)"
    column has no setpoint/reading split at all, so it never contributes here) - a positive,
    growing drift over many runs is an early "this MFC needs recalibration" signal, not something
    visible from any one run's own chart alone. None if this run has no such pair at all."""
    setpoints, readings = {}, {}
    for name, (_unit, values) in run_data["other_series"].items():
        m = _MFC_SETPOINT_RE.match(name)
        if m:
            setpoints[m.group(1)] = values
            continue
        m = _MFC_READING_RE.match(name)
        if m:
            readings[m.group(1)] = values
    deviations = []
    for num, setpoint_values in setpoints.items():
        reading_values = readings.get(num)
        if not reading_values:
            continue
        deviations.extend(
            abs(s - r) for s, r in zip(setpoint_values, reading_values) if s is not None and r is not None
        )
    return sum(deviations) / len(deviations) if deviations else None


def get_mfc_drift_trend(cfg, scan_limit=_MVD_SIGNAL_SCAN_LIMIT):
    """See _mvd_weekly_signal_trend/_extract_mfc_drift - trended average MFC setpoint-vs-reading
    deviation (sccm) over this tool's recent run history."""
    return _mvd_weekly_signal_trend(cfg, scan_limit, _extract_mfc_drift)


def _extract_rf_reflected_fraction(run_data):
    """Average (reflected / forward) plasma power while the plasma is actually on (forward power
    > 5W, so a run with the RF off entirely doesn't divide noise-level readings by noise-level
    readings) - a rising fraction over time, independent of absolute power level (which varies by
    recipe), is a classic "the match network is degrading" signal. None if this run has no
    PlasmaForwardPower/PlasmaReversePower columns at all, or the plasma was never on."""
    forward = run_data["other_series"].get("PlasmaForwardPower")
    reverse = run_data["other_series"].get("PlasmaReversePower")
    if not forward or not reverse:
        return None
    _unit, forward_values = forward
    _unit2, reverse_values = reverse
    fractions = [
        r / f
        for f, r in zip(forward_values, reverse_values)
        if f is not None and r is not None and f > 5.0
    ]
    return 100.0 * sum(fractions) / len(fractions) if fractions else None


def get_rf_health_trend(cfg, scan_limit=_MVD_SIGNAL_SCAN_LIMIT):
    """See _mvd_weekly_signal_trend/_extract_rf_reflected_fraction - trended average reflected
    power as a % of forward power over this tool's recent run history."""
    return _mvd_weekly_signal_trend(cfg, scan_limit, _extract_rf_reflected_fraction)


def _extract_turbo_speed(run_data, series_name):
    entry = run_data["other_series"].get(series_name)
    if not entry:
        return None
    _unit, values = entry
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def get_turbo_speed_trend(cfg, scan_limit=_MVD_SIGNAL_SCAN_LIMIT):
    """Trended average turbo pump speed (rpm) over this tool's recent run history, for both of
    fiji5's turbo pumps (the reactor's own, and the load lock's) - a slow decline over months in
    either is an early bearing-wear signal, invisible today because it's buried per-run. Returns
    {"reactor": [...], "load_lock": [...]}, each shaped like _mvd_weekly_signal_trend's own
    return - either can be [] independently (a tool without a load lock, for instance)."""
    return {
        "reactor": _mvd_weekly_signal_trend(cfg, scan_limit, lambda rd: _extract_turbo_speed(rd, "ReactorTurboSpeed")),
        "load_lock": _mvd_weekly_signal_trend(cfg, scan_limit, lambda rd: _extract_turbo_speed(rd, "LoadLock TurboSpeed")),
    }


def _run_pressure_series(cfg, run_id):
    """(time_s, values) for the one pressure trace most representative of chamber pressure during
    this run - heater_log's own single "Pressure Data" sibling series, or mvd/fiji5's primary
    chamber gauge (see _mvd_default_visible_pressure_channel - the same "which gauge is the real
    chamber one" heuristic the run's own pressure chart tab already uses to pick what's checked by
    default) from its own PT.txt. None if this run has no pressure data at all, or this tool kind
    has no such concept."""
    if cfg["kind"] == "heater_log":
        pressure_group = _sibling_run_group(cfg, run_id, "Pressure Data", 1, "pressure", "", "", "")
        if not pressure_group:
            return None
        return next(iter(pressure_group["series"].values()))
    if cfg["kind"] == "mvd":
        try:
            run_dir = _resolve_mvd_run_dir(cfg, run_id)
        except (ToolDataError, remote_sync.RemoteSyncError):
            return None
        pt_path = _mvd_pt_path(run_dir)
        if pt_path is None:
            return None
        time_s, series = _cached_file_parse("mvd_pt", [pt_path], lambda: _parse_mvd_pt(pt_path))
        if not series:
            return None
        primary = _mvd_default_visible_pressure_channel(series) or next(iter(series))
        _unit, values = series[primary]
        return time_s, values
    return None


def _pump_down_time_s(time_s, pressure_values, window_s=10.0, tolerance=1.5):
    """Best-effort "how long this run took to pump down/settle": the elapsed time from the run's
    own start until the pressure trace's LAST crossing down through `tolerance`x its own settled
    baseline (the last `window_s` seconds' own average - the same tail-average _average_tail
    already uses for base pressure) - reading backward from the end rather than forward from the
    start so a brief early dip below threshold (still climbing/stabilizing, not actually settled
    yet) doesn't get mistaken for the real pump-down moment.

    A heuristic relative-speed metric, not a spec'd pump-down time - meaningful for trending how a
    given recipe's pump-down behavior changes over many runs (a slowing trend suggests a
    degrading pump or a growing leak), not as an absolute number on its own. None if there's no
    settled baseline to compare against at all (too few points, or every value is None)."""
    settled = _average_tail(time_s, pressure_values, window_s)
    if settled is None:
        return None
    threshold = settled * tolerance
    last_above_index = None
    for i, v in enumerate(pressure_values):
        if v is not None and v > threshold:
            last_above_index = i
    if last_above_index is None or last_above_index + 1 >= len(time_s):
        return None
    return time_s[last_above_index + 1] - time_s[0]


def get_pump_down_time(cfg, run_id):
    """This one run's own pump-down time (see _pump_down_time_s) - shown on a run's own detail
    page alongside its other summary fields. None if this run has no usable pressure data."""
    series = _run_pressure_series(cfg, run_id)
    if series is None:
        return None
    time_s, values = series
    return _pump_down_time_s(time_s, values)


def get_pump_down_trend(cfg, scan_limit=100):
    """Trended pump-down time (see _pump_down_time_s) over this tool's `scan_limit` most recent
    runs - a slowing trend over many runs of comparable recipes suggests a degrading pump or a
    growing leak, visible well before it shows up as an outright failure. Bounded the same way
    get_fault_rate_trend is (a run's own pressure series still needs a real per-run fetch/parse,
    unlike that function's already-cached summary fields) - smaller than that default since this
    also applies to mvd-kind tools' larger files, matching _MVD_SIGNAL_SCAN_LIMIT's own reasoning.

    Returns [{"week_start": date, "value": float (seconds), "run_count": int}, ...] oldest week
    first, same shape as _mvd_weekly_signal_trend. [] for a kind with no pressure concept at all,
    or no run in the scanned window has usable pressure data."""
    if cfg["kind"] not in ("heater_log", "mvd"):
        return []
    runs, _total = get_tool_history(cfg, page=1, page_size=min(scan_limit, _MVD_SIGNAL_SCAN_LIMIT if cfg["kind"] == "mvd" else scan_limit))
    buckets = {}
    for run in runs:
        if run.get("timestamp") is None:
            continue
        series = _run_pressure_series(cfg, run["run_id"])
        if series is None:
            continue
        value = _pump_down_time_s(*series)
        if value is None:
            continue
        year, week, _weekday = run["timestamp"].isocalendar()
        week_start = date.fromisocalendar(year, week, 1)
        buckets.setdefault(week_start, []).append(value)
    trend = [
        {"week_start": week_start, "value": sum(values) / len(values), "run_count": len(values)}
        for week_start, values in buckets.items()
    ]
    trend.sort(key=lambda entry: entry["week_start"])
    return trend


def _mvd_run_maintenance_signals(run_dir):
    """The tiny handful of maintenance-trend numbers (MFC drift, RF health, both turbo speeds)
    extracted from one run's own DAT file - cached under its OWN, much smaller key, separate from
    _mvd_run_data_for_dir's full parsed series (fiji5's own DAT files run past 200,000 rows/80+
    columns - measured live that Django's local-memory cache deep-copying that whole structure
    back out on every single get() dominated get_mvd_maintenance_trends' own runtime, even on a
    warm cache, even after already cutting it down to one _mvd_run_data call per run). Caching
    just these four floats per run means a repeat view of the maintenance trends page never needs
    to touch the big series again at all - only the first computation for a given run pays that
    real cost; every view after it is copying a few floats, not a quarter-million-row array."""
    sum_path = _find_one(run_dir, "_SUM.txt")
    dat_path = _find_one(run_dir, "_DAT.txt")

    def compute():
        run_data = _mvd_run_data_for_dir(run_dir)
        return {
            "mfc_drift": _extract_mfc_drift(run_data),
            "rf_health": _extract_rf_reflected_fraction(run_data),
            "turbo_reactor": _extract_turbo_speed(run_data, "ReactorTurboSpeed"),
            "turbo_load_lock": _extract_turbo_speed(run_data, "LoadLock TurboSpeed"),
        }

    return _cached_file_parse("mvd_maintenance_signals", [sum_path, dat_path], compute)


def get_mvd_maintenance_trends(cfg, scan_limit=_MVD_SIGNAL_SCAN_LIMIT):
    """MFC drift, RF match-network health, both turbo pump speeds, AND pump-down time, all
    computed together from ONE shared scan over this tool's `scan_limit` most recent runs.

    Deliberately built on the CHEAP listing (_list_mvd_run_entries + each run's own free
    filename-embedded start timestamp - see _mvd_folder_timestamp) rather than get_tool_history
    (which would fully parse every run just to build a summary this function doesn't need), and
    reads the four MFC/RF/turbo signals via _mvd_run_maintenance_signals - see that function's own
    docstring for why the extraction itself is cached separately from, and much smaller than, the
    full parsed series.

    Returns {"mfc_drift": [...], "rf_health": [...], "turbo_reactor": [...],
    "turbo_load_lock": [...], "pump_down": [...]}, each shaped like _mvd_weekly_signal_trend's own
    return. [] for every key on a non-mvd-kind tool."""
    empty = {"mfc_drift": [], "rf_health": [], "turbo_reactor": [], "turbo_load_lock": [], "pump_down": []}
    if cfg["kind"] != "mvd":
        return empty

    try:
        entries = _list_mvd_run_entries(cfg)
    except ToolDataError:
        return empty

    tool = cfg.get("remote_tool")
    if tool is not None:
        # Prefetch every scanned run's whole folder CONCURRENTLY before the loop below touches any
        # of them - _resolve_mvd_run_dir's own ensure_cached(..., is_dir=True) is a real rsync round
        # trip per run when remote, and this scan covers up to scan_limit (60) runs. Fetching them
        # one at a time (the original approach) serialized up to 60 sequential round trips into this
        # one page load - confirmed live as a major, previously-hidden chunk of the mvd/fiji5
        # "Health" page's own latency, on top of the DAT-file parsing cost _MVD_SIGNAL_SCAN_LIMIT's
        # own docstring already accounts for. Already-local runs (the overwhelming majority after
        # the first view) cost nothing extra either way - see ensure_cached_many's own fast path.
        remote_cache.ensure_cached_many(tool, [f"log/data/{name}" for name, _mtime in entries[:scan_limit]], is_dir=True)

    buckets = {key: {} for key in empty}
    for name, _mtime in entries[:scan_limit]:
        run_start = _mvd_folder_timestamp(name)
        if run_start is None:
            continue
        year, week, _weekday = run_start.isocalendar()
        week_start = date.fromisocalendar(year, week, 1)

        try:
            run_dir = _resolve_mvd_run_dir(cfg, name)
            signals = _mvd_run_maintenance_signals(run_dir)
        except (ToolDataError, remote_sync.RemoteSyncError):
            signals = None
        if signals is not None:
            for key in ("mfc_drift", "rf_health", "turbo_reactor", "turbo_load_lock"):
                value = signals.get(key)
                if value is not None:
                    buckets[key].setdefault(week_start, []).append(value)

        series = _run_pressure_series(cfg, name)
        if series is not None:
            value = _pump_down_time_s(*series)
            if value is not None:
                buckets["pump_down"].setdefault(week_start, []).append(value)

    result = {}
    for key, week_buckets in buckets.items():
        trend = [
            {"week_start": week_start, "value": sum(values) / len(values), "run_count": len(values)}
            for week_start, values in week_buckets.items()
        ]
        trend.sort(key=lambda entry: entry["week_start"])
        result[key] = trend
    return result


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
