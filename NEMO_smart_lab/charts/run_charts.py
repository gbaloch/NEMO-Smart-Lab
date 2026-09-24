"""Static PNG charts for a single run ("Download as image"), by reader kind."""

import matplotlib

matplotlib.use("Agg")


import matplotlib.pyplot as plt

from NEMO_smart_lab.readers import (
    ToolDataError,
    get_cobra_step_timeline,
    get_eventlog_timeline,
    get_heater_log_run_events,
    get_mvd_pressure_group,
    get_mvd_run_events,
)
from NEMO_smart_lab.charts.common import _finish, _resolve_chart_group, _series_color, _with_run_suffix


def _render_group(group, username=None, timestamp=None, hide=None):
    """Line chart PNG for an already-resolved chart group dict ({"title", "x_label", "y_label",
    "series"}) - shared by _render_generic (heater_log/mvd/waferlog's regular chart groups,
    resolved via _resolve_chart_group) and mvd's own lazily-resolved pressure groups (see
    get_mvd_pressure_group - deliberately NOT part of _resolve_chart_group/get_chart_groups, so
    listing a run's tabs never pays for parsing what can be a 100,000+ row _PT.txt).

    `hide` (optional set of series names) - a channel the user has unchecked on the interactive
    uPlot legend before clicking "Download as image" (see js/charts's download link
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
        color = _series_color(name, i)
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
# confirmed live), and a "download as image" of that is neither requested (js/charts
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
