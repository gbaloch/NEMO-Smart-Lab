import matplotlib

matplotlib.use("Agg")

import io

import matplotlib.pyplot as plt

from NEMO_smart_lab.readers import (
    ToolDataError,
    get_chart_groups,
    get_cobra_step_timeline,
    get_eventlog_timeline,
    get_heater_log_run_events,
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


def _render_generic(cfg, run_id, group_key=None):
    """Line chart for kinds with a continuous per-channel time series (heater_log/mvd/waferlog)."""
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    group = _resolve_chart_group(cfg, run_id, group_key)
    title, x_label, y_label, series = group["title"], group["x_label"], group["y_label"], group["series"]
    plotted = False
    for i, (name, (x_values, y_values)) in enumerate(sorted(series.items())):
        points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
        if not points:
            continue
        xs, ys = zip(*points)
        color = LINE_CHART_COLORS[i % len(LINE_CHART_COLORS)]
        ax.plot(xs, ys, marker=".", markersize=2, linewidth=1, label=name, color=color)
        plotted = True
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if not plotted:
        ax.text(0.5, 0.5, "No numeric data to plot", ha="center", va="center", transform=ax.transAxes)
    return _finish(fig, ax, legend_outside=plotted)


def _render_cobra(cfg, run_id):
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
    ax.set_title(f"{title} (Step Timeline)")
    ax.set_xlabel("Time Since Job Start (s)")
    return _finish(fig, ax)


def _render_scatter_timeline(title, modules, points):
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
    ax.set_title(title)
    ax.set_xlabel("Time Since Run Start (s)")
    return _finish(fig, ax)


def _render_eventlog(cfg, run_id):
    title, modules, points = get_eventlog_timeline(cfg, run_id)
    return _render_scatter_timeline(title, modules, points)


_RENDERERS = {
    "cobra_job": _render_cobra,
    "eventlog": _render_eventlog,
}


def render_chart_png(cfg, run_id=None, group_key=None):
    try:
        if cfg["kind"] in _RENDERERS:
            return _RENDERERS[cfg["kind"]](cfg, run_id)
        if cfg["kind"] == "heater_log" and group_key == "events":
            title, points = get_heater_log_run_events(cfg, run_id)
            return _render_scatter_timeline(title, ["Events"], points)
        return _render_generic(cfg, run_id, group_key)
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


def get_chart_json(cfg, run_id=None, group_key=None, start=None, end=None):
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
                "title": f"{title} (Step Timeline)",
                "x_label": "Time Since Job Start (s)",
                "bars": [{"label": label, "start": offset, "duration": duration} for label, offset, duration in bars],
            }
        if cfg["kind"] == "eventlog":
            title, modules, points = get_eventlog_timeline(cfg, run_id)
            return {
                "chart_type": "scatter",
                "title": title,
                "x_label": "Time Since Run Start (s)",
                "modules": modules,
                "points": [
                    {"x": offset, "module": module, "event": event_name, "fault": is_fault}
                    for offset, module, event_name, is_fault in points
                ],
            }
        if cfg["kind"] == "heater_log" and group_key == "events":
            # A plain list, not a chart - a scatter plot of sparse, irregularly-timed text events
            # doesn't read well as a chart; a simple chronological list does.
            title, points = get_heater_log_run_events(cfg, run_id)
            return {
                "chart_type": "list",
                "title": title,
                "items": [{"offset": offset, "text": event_name, "fault": is_fault} for offset, _module, event_name, is_fault in points],
            }
        group = _resolve_chart_group(cfg, run_id, group_key)
        line_json = _line_series_json(group["series"], start=start, end=end)
        return {
            "chart_type": "line",
            "title": group["title"],
            "x_label": group["x_label"],
            "y_label": group["y_label"],
            "x": line_json["x"],
            "series": line_json["series"],
        }
    except ToolDataError as e:
        return {"chart_type": "error", "message": str(e)}


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
