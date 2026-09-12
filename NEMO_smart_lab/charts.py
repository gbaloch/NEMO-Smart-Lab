import matplotlib

matplotlib.use("Agg")

import csv
import io
from datetime import datetime, timedelta

import matplotlib.pyplot as plt

from NEMO_smart_lab.readers import (
    ToolDataError,
    get_base_pressure_history,
    get_chart_groups,
    get_cobra_step_timeline,
    get_eventlog_timeline,
    get_heater_log_run_events,
    get_mvd_pressure_group,
    get_mvd_run_events,
    get_stream_chart_data,
)


def _resolve_chart_group(cfg, run_id, group_key):
    """The requested group, or the first (Temperature) one if group_key is None/blank/unknown -
    unknown falls back rather than 404ing, since a stale bookmarked/cached URL for a group that
    no longer exists on a later run shouldn't break instead of just showing the primary chart."""
    groups = get_chart_groups(cfg, run_id)
    if group_key:
        group = next((g for g in groups if g["key"] == group_key), None)
        if group is not None:
            return group
    return groups[0]

# Same palette as static/NEMO_smart_lab/js/smart_lab_charts.js's SMART_LAB_CHART_COLORS, so a
# channel's line color matches between the interactive uPlot canvas and its "download as image"
# PNG counterpart instead of matplotlib's own default color cycle.
LINE_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
]


def _with_run_suffix(title, username=None, timestamp=None):
    """"<recipe title> - <username> - <run timestamp>" with whichever of username/timestamp are
    actually known appended (either, both, or neither) - see views.py's tool_detail, which
    resolves the run's username (from the same reservation/usage lookup already shown on the page)
    and its already-formatted "Run ended"/"Last update" display string once, then passes both
    through as query params rather than looking them up again per chart."""
    parts = [p for p in (username, timestamp) if p]
    return f"{title} - {' - '.join(parts)}" if parts else title


def _finish(fig, ax, legend_outside=False):
    ax.grid(True, linestyle=":", linewidth=0.5)
    if legend_outside:
        # Placed outside the axes (to the right) instead of overlaid on the plot, so it never
        # covers data - bbox_inches="tight" on save (below) is what keeps it from being clipped
        # off the edge of the saved image.
        ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=7)
    else:
        fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight" if legend_outside else None)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_group(group, username=None, timestamp=None, hide=None):
    """Line chart PNG for an already-resolved chart group dict ({"title", "x_label", "y_label",
    "series"}) - shared by _render_generic (heater_log/mvd/waferlog's regular chart groups,
    resolved via _resolve_chart_group) and mvd's own lazily-resolved pressure groups (see
    get_mvd_pressure_group - deliberately NOT part of _resolve_chart_group/get_chart_groups, so
    listing a run's tabs never pays for parsing what can be a 100,000+ row _PT.txt).

    `hide` (optional set of series names) - a channel the user has unchecked on the interactive
    uPlot legend before clicking "Download as image" (see smart_lab_charts.js's download link
    handler) is left out of the PNG the same way it's left out of the live chart, rather than the
    download silently including channels the user explicitly turned off. Colors are still assigned
    by each series' position in the *full*, unfiltered sorted list (not a renumbered filtered one),
    so a channel's color always matches its color in the interactive view regardless of what's
    hidden."""
    hide = hide or set()
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    title, x_label, y_label, series = group["title"], group["x_label"], group["y_label"], group["series"]
    plotted = False
    for i, (name, (x_values, y_values)) in enumerate(sorted(series.items())):
        if name in hide:
            continue
        points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
        if not points:
            continue
        xs, ys = zip(*points)
        color = LINE_CHART_COLORS[i % len(LINE_CHART_COLORS)]
        ax.plot(xs, ys, marker=".", markersize=2, linewidth=1, label=name, color=color)
        plotted = True
    ax.set_title(_with_run_suffix(title, username, timestamp))
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if not plotted:
        ax.text(0.5, 0.5, "No numeric data to plot", ha="center", va="center", transform=ax.transAxes)
    return _finish(fig, ax, legend_outside=plotted)


def _render_generic(cfg, run_id, group_key=None, username=None, timestamp=None, hide=None):
    """Line chart for kinds with a continuous per-channel time series (heater_log/mvd/waferlog)."""
    return _render_group(_resolve_chart_group(cfg, run_id, group_key), username, timestamp, hide)


def _render_cobra(cfg, run_id, username=None, timestamp=None):
    """Gantt-style horizontal bar chart of recipe step timing (cobra_job)."""
    title, bars = get_cobra_step_timeline(cfg, run_id)
    height = max(2, 0.6 * len(bars)) if bars else 3
    fig, ax = plt.subplots(figsize=(9, height), dpi=110)
    if bars:
        colors = plt.get_cmap("tab10").colors
        for i, (label, offset, duration) in enumerate(bars):
            ax.barh(i, duration, left=offset, color=colors[i % len(colors)])
            ax.text(offset, i, f" {label}", va="center", fontsize=8)
        ax.set_yticks(range(len(bars)))
        ax.set_yticklabels([f"Step {i}" for i in range(len(bars))])
        ax.invert_yaxis()
    else:
        ax.text(0.5, 0.5, "No recipe step data recorded for this job", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(_with_run_suffix(f"{title} (Step Timeline)", username, timestamp))
    ax.set_xlabel("Time Since Job Start (s)")
    return _finish(fig, ax)


def _render_scatter_timeline(title, modules, points, username=None, timestamp=None):
    """Scatter timeline of events per module, faults highlighted - shared by eventlog (its own
    reader kind) and heater_log's "Events" group (get_heater_log_run_events), same point shape."""
    height = max(3, 1.2 * len(modules)) if modules else 3
    fig, ax = plt.subplots(figsize=(10, height), dpi=110)
    if points:
        module_index = {m: i for i, m in enumerate(modules)}
        for offset, module, event_name, is_fault in points:
            color = "tab:red" if is_fault else "tab:blue"
            y = module_index[module]
            ax.scatter(offset, y, color=color, zorder=3)
            ax.annotate(event_name, (offset, y), fontsize=7, rotation=30, xytext=(2, 6), textcoords="offset points")
        ax.set_yticks(range(len(modules)))
        ax.set_yticklabels(modules)
        ax.margins(y=0.4)
    else:
        ax.text(0.5, 0.5, "No event data for this run", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(_with_run_suffix(title, username, timestamp))
    ax.set_xlabel("Time Since Run Start (s)")
    return _finish(fig, ax)


def _render_eventlog(cfg, run_id, username=None, timestamp=None):
    title, modules, points = get_eventlog_timeline(cfg, run_id)
    return _render_scatter_timeline(title, modules, points, username, timestamp)


_RENDERERS = {
    "cobra_job": _render_cobra,
    "eventlog": _render_eventlog,
}


# A scatter timeline reads fine for a handful of sparse events, but stops being a useful image
# well before it'd be a useful *list* - a run's Events tab can carry 13,000+ real entries (fiji5,
# confirmed live), and a "download as image" of that is neither requested (smart_lab_charts.js
# already hides the link past a much smaller count) nor renderable as anything but an unreadable
# smear of overlapping text. Rejected server-side too - not just hidden client-side - so a
# stale/bookmarked/hand-typed chart.png?group=events URL can't force a giant render either.
MAX_EVENTS_FOR_PNG = 10


def render_chart_png(cfg, run_id=None, group_key=None, username=None, timestamp=None, hide=None):
    try:
        if cfg["kind"] in _RENDERERS:
            return _RENDERERS[cfg["kind"]](cfg, run_id, username, timestamp)
        if cfg["kind"] == "heater_log" and group_key == "events":
            title, points = get_heater_log_run_events(cfg, run_id)
            if len(points) > MAX_EVENTS_FOR_PNG:
                raise ToolDataError(f"Too many events ({len(points)}) to render as an image - use the interactive list instead.")
            return _render_scatter_timeline(title, ["Events"], points, username, timestamp)
        if cfg["kind"] == "mvd" and group_key == "events":
            title, points = get_mvd_run_events(cfg, run_id)
            if len(points) > MAX_EVENTS_FOR_PNG:
                raise ToolDataError(f"Too many events ({len(points)}) to render as an image - use the interactive list instead.")
            modules = sorted({p[1] for p in points}) or ["Events"]
            return _render_scatter_timeline(title, modules, points, username, timestamp)
        if cfg["kind"] == "mvd" and group_key and group_key.startswith("pressure_"):
            return _render_group(get_mvd_pressure_group(cfg, run_id, group_key), username, timestamp, hide)
        return _render_generic(cfg, run_id, group_key, username, timestamp, hide)
    except ToolDataError as e:
        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
        ax.text(0.5, 0.5, str(e), ha="center", va="center", wrap=True, transform=ax.transAxes)
        return _finish(fig, ax)


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
    NEMO_smart_lab.static.NEMO_smart_lab.js.smart_lab_charts.js (uPlot, for "line") to draw an
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
                # means "show all" in smart_lab_charts.js's smartLabRenderUplot.
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


def _base_pressure_title(cfg):
    # Names which exact standby recipe(s) this trend is built from, right in the chart's own
    # title - the graph is meaningless without knowing that, and a viewer looking at just the
    # chart (or its downloaded PNG) shouldn't have to go back to the tool detail page's info table
    # to find out.
    recipes = cfg.get("base_pressure_recipe_names") or ""
    names = [r.strip() for r in recipes.split(",") if r.strip()]
    return f"Chamber base pressure ({', '.join(names)})" if names else "Chamber base pressure (standby)"


# Only offered once there's actually more than a year of history to narrow down (see
# get_base_pressure_chart_json's "full_range_days" - smart_lab_charts.js shows the dropdown only
# past that) - a short history has nothing meaningful to filter. Keyed by the <select> value used
# in tool_detail.html; anything else (missing, "all", unrecognized) means the full, unfiltered
# history, same as before this range picker existed.
BASE_PRESSURE_RANGE_DAYS = {"1y": 365, "5y": 365 * 5, "10y": 365 * 10}


def _filter_base_pressure_range(points, range_key):
    days = BASE_PRESSURE_RANGE_DAYS.get(range_key)
    if not days or not points:
        return points
    cutoff = datetime.now() - timedelta(days=days)
    return [p for p in points if p["timestamp"] >= cutoff]


def get_base_pressure_chart_json(cfg, range_key=None):
    """Chamber base pressure over time (see readers.get_base_pressure_history) - a genuinely
    different x-axis from every other chart in this file: real wall-clock time (one point per
    historical run), not "seconds since this run started". "time_x": True tells
    smart_lab_charts.js's smartLabRenderUplot to use uPlot's time-scale/date-axis mode instead of
    the plain-seconds mode every other chart here uses.

    `range_key` (see BASE_PRESSURE_RANGE_DAYS) narrows to only the last N years of an otherwise
    long history - "full_range_days" in the response (always computed off the *unfiltered* full
    history, regardless of `range_key`) is how the JS side decides whether the range dropdown is
    even worth showing in the first place."""
    all_points = get_base_pressure_history(cfg)
    if not all_points:
        return {"chart_type": "error", "message": "No base pressure history configured or recorded for this tool yet."}
    full_range_days = (all_points[-1]["timestamp"] - all_points[0]["timestamp"]).days
    points = _filter_base_pressure_range(all_points, range_key)
    if not points:
        return {"chart_type": "error", "message": "No base pressure history in the selected time range."}
    unit = points[0]["unit"]
    return {
        "chart_type": "line",
        "title": _base_pressure_title(cfg),
        "x_label": "Date",
        "y_label": f"Base pressure ({unit})" if unit else "Base pressure",
        "time_x": True,
        "x": [p["timestamp"].timestamp() for p in points],
        "series": [{"name": f"Base pressure ({unit})" if unit else "Base pressure", "y": [p["value"] for p in points]}],
        "full_range_days": full_range_days,
    }


def render_base_pressure_chart_png(cfg, range_key=None):
    """PNG "download as image" counterpart to get_base_pressure_chart_json - real wall-clock dates
    on the x-axis (matplotlib handles Python datetimes natively), unlike every other PNG chart
    here (all plain seconds-since-run-start). `range_key` matches whatever range is currently
    selected on the live chart (see smart_lab_charts.js), so downloading an image reflects what's
    actually on screen rather than always the full history."""
    points = _filter_base_pressure_range(get_base_pressure_history(cfg), range_key)
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    if not points:
        ax.text(0.5, 0.5, "No base pressure history configured or recorded for this tool yet.", ha="center", va="center", wrap=True, transform=ax.transAxes)
        return _finish(fig, ax)
    unit = points[0]["unit"]
    ax.plot(
        [p["timestamp"] for p in points], [p["value"] for p in points],
        marker=".", markersize=4, linewidth=1, color=LINE_CHART_COLORS[0],
    )
    ax.set_title(_base_pressure_title(cfg))
    ax.set_xlabel("Date")
    ax.set_ylabel(f"Base pressure ({unit})" if unit else "Base pressure")
    fig.autofmt_xdate()
    return _finish(fig, ax)


def get_stream_chart_json(cfg):
    try:
        title, x_label, y_label, series = get_stream_chart_data(cfg)
        line_json = _line_series_json(series)
        return {
            "chart_type": "line",
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "x": line_json["x"],
            "series": line_json["series"],
        }
    except ToolDataError as e:
        return {"chart_type": "error", "message": str(e)}


def render_base_pressure_csv(cfg, range_key=None):
    """"Download as CSV" counterpart to get_base_pressure_chart_json/render_base_pressure_chart_png
    - the same points (see readers.get_base_pressure_history), as plain rows instead of a plotted
    line, for a viewer who wants the actual numbers (further analysis in a spreadsheet, sharing the
    raw trend with someone else, etc.) rather than just a picture of them. One row per run, oldest
    first - matching the chart's own left-to-right order. `range_key` matches the chart's own
    currently-selected range picker (see get_base_pressure_chart_json), same reasoning as the PNG
    download - the export reflects what's actually on screen."""
    points = _filter_base_pressure_range(get_base_pressure_history(cfg), range_key)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "value", "unit", "run_id"])
    for p in points:
        writer.writerow([p["timestamp"].isoformat(), p["value"], p["unit"], p["run_id"]])
    return buf.getvalue()


def render_stream_chart_png(cfg):
    """Live PTIQ telemetry chart (Cobra tools only, and only if "stream_root" is configured) -
    always the single most recent minute of data, there's no history browsing for this."""
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    plotted = False
    try:
        title, x_label, y_label, series = get_stream_chart_data(cfg)
        for i, (name, (x_values, y_values)) in enumerate(sorted(series.items())):
            points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
            if not points:
                continue
            xs, ys = zip(*points)
            color = LINE_CHART_COLORS[i % len(LINE_CHART_COLORS)]
            ax.plot(xs, ys, linewidth=1, label=name, color=color)
            plotted = True
        ax.set_title(title)
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        if not plotted:
            ax.text(0.5, 0.5, "No numeric data to plot", ha="center", va="center", transform=ax.transAxes)
    except ToolDataError as e:
        ax.text(0.5, 0.5, str(e), ha="center", va="center", wrap=True, transform=ax.transAxes)
    return _finish(fig, ax, legend_outside=plotted)
