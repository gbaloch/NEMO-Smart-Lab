"""The interactive line-chart JSON the browser's uPlot charts are drawn from, including range-scoped re-fetches for zooming."""

from NEMO_smart_lab.readers import (
    ToolDataError,
    get_cobra_step_timeline,
    get_eventlog_timeline,
    get_heater_log_run_events,
    get_mvd_pressure_group,
    get_mvd_run_events,
)
from NEMO_smart_lab.charts.common import _resolve_chart_group, _with_run_suffix


def _align_series(series):
    """{"name": (x_values, y_values)} -> (shared_x, {"name": aligned_y}), dropping any series left
    with no real values anywhere. shared_x is the sorted union of every series' own x-values -
    already identical across every series for heater_log/mvd (they all come from one shared
    time_s array by construction), but genuinely different for e.g. waferlog/the live PTIQ stream
    chart (each channel has its own independently-timestamped readings). Each series' y is
    re-aligned to shared_x with None wherever that series had nothing at a given shared x - this
    is uPlot's required data model (one shared x-array, same-length y-arrays, None marks a gap),
    unlike the old per-series-independent {x, y} shape the previous Chart.js renderer used."""
    names = [name for name in sorted(series) if series[name][1] and any(v is not None for v in series[name][1])]
    if not names:
        return [], {}

    per_series = {name: dict(zip(series[name][0], series[name][1])) for name in names}
    shared_x = sorted(set().union(*(series[name][0] for name in names)))
    aligned = {name: [per_series[name].get(x) for x in shared_x] for name in names}
    return shared_x, aligned


def _line_series_json(series, start=None, end=None):
    """{"name": (x_values, y_values)} -> {"x": [...], "series": [{"name", "y": [...]}]} - the
    uPlot-ready shape (see _align_series for why nulls are kept in place and aligned, rather than
    dropped per-series the way the old Chart.js shape did). Always the real, full-resolution data -
    uPlot renders tens of thousands of points per series smoothly, so there's no need to decimate
    server-side the way the earlier Chart.js-based renderer did.

    start/end (optional, same units as the x-axis) filter to just that range - a zoom-triggered
    re-fetch passes these to get exactly the visible window's data."""
    shared_x, aligned = _align_series(series)
    names = sorted(aligned)

    if (start is not None or end is not None) and shared_x:
        lo = start if start is not None else float("-inf")
        hi = end if end is not None else float("inf")
        keep = [i for i, x in enumerate(shared_x) if lo <= x <= hi]
        shared_x = [shared_x[i] for i in keep]
        aligned = {name: [aligned[name][i] for i in keep] for name in names}

    return {"x": shared_x, "series": [{"name": name, "y": aligned[name]} for name in names]}


def get_chart_json(cfg, run_id=None, group_key=None, start=None, end=None, username=None, timestamp=None):
    """Browser-rendered-chart counterpart to render_chart_png()/render_stream_chart_png() below -
    same reader functions, same per-kind shape, but returned as a JSON-serializable dict for
    NEMO_smart_lab.static.NEMO_smart_lab.js.charts (uPlot, for "line") to draw an
    interactive chart from, instead of a static server-rendered PNG. "gantt"/"scatter"
    (cobra_job/eventlog) have no interactive JS renderer - not configured for any current tool -
    only the PNG endpoint (unaffected, still matplotlib) covers them. The PNG endpoint is otherwise
    unchanged and kept as a "download as image" option alongside the interactive line charts.

    group_key selects which of get_chart_groups()'s independent charts to return (e.g. "mfc_flow"
    instead of the default "temperature") - omitted/unknown falls back to the first group.
    start/end (only meaningful for "line") request just that x-range - see _line_series_json;
    used for a zoom-triggered re-fetch of real, undecimated data for whatever's visible."""
    try:
        if cfg["kind"] == "cobra_job":
            title, bars = get_cobra_step_timeline(cfg, run_id)
            return {
                "chart_type": "gantt",
                "title": _with_run_suffix(f"{title} (Step Timeline)", username, timestamp),
                "x_label": "Time Since Job Start (s)",
                "bars": [{"label": label, "start": offset, "duration": duration} for label, offset, duration in bars],
            }
        if cfg["kind"] == "eventlog":
            title, modules, points = get_eventlog_timeline(cfg, run_id)
            return {
                "chart_type": "scatter",
                "title": _with_run_suffix(title, username, timestamp),
                "x_label": "Time Since Run Start (s)",
                "modules": modules,
                "points": [
                    {"x": offset, "module": module, "event": event_name, "fault": is_fault}
                    for offset, module, event_name, is_fault in points
                ],
            }
        if cfg["kind"] == "heater_log" and group_key == "events":
            # A plain list, not a chart - a scatter plot of sparse, irregularly-timed text events
            # doesn't read well as a chart; a simple chronological list does. "category" is always
            # "Events" here - heater_log's raw format has no per-line category of its own (unlike
            # mvd's real "STATUS;"/"MFCLOOP;"/etc. tags below) - kept as a field anyway so the
            # frontend list renderer (grouping/column) has one consistent shape for both kinds.
            title, points = get_heater_log_run_events(cfg, run_id)
            return {
                "chart_type": "list",
                "title": _with_run_suffix(title, username, timestamp),
                "items": [
                    {"offset": offset, "category": module, "text": event_name, "fault": is_fault}
                    for offset, module, event_name, is_fault in points
                ],
            }
        if cfg["kind"] == "mvd" and group_key == "events":
            title, points = get_mvd_run_events(cfg, run_id)
            return {
                "chart_type": "list",
                "title": _with_run_suffix(title, username, timestamp),
                "items": [
                    {"offset": offset, "category": category, "text": event_name, "fault": is_fault}
                    for offset, category, event_name, is_fault in points
                ],
            }
        if cfg["kind"] == "mvd" and group_key and group_key.startswith("pressure_"):
            group = get_mvd_pressure_group(cfg, run_id, group_key)
            line_json = _line_series_json(group["series"], start=start, end=end)
            return {
                "chart_type": "line",
                "title": _with_run_suffix(group["title"], username, timestamp),
                "x_label": group["x_label"],
                "y_label": group["y_label"],
                "x": line_json["x"],
                "series": line_json["series"],
                # Only the main chamber gauge should be checked by default when there's more than
                # one on this tab (see readers._mvd_default_visible_pressure_channel) - None/absent
                # means "show all" in js/charts's smartLabRenderUplot.
                "default_visible": group.get("default_visible"),
            }
        group = _resolve_chart_group(cfg, run_id, group_key)
        line_json = _line_series_json(group["series"], start=start, end=end)
        return {
            "chart_type": "line",
            "title": _with_run_suffix(group["title"], username, timestamp),
            "x_label": group["x_label"],
            "y_label": group["y_label"],
            "x": line_json["x"],
            "series": line_json["series"],
        }
    except ToolDataError as e:
        return {"chart_type": "error", "message": str(e)}
