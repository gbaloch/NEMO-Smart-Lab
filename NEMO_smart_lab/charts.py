import matplotlib

matplotlib.use("Agg")

import io

import matplotlib.pyplot as plt

from NEMO_smart_lab.readers import (
    ToolDataError,
    get_chart_data,
    get_cobra_step_timeline,
    get_eventlog_timeline,
    get_stream_chart_data,
)


def _finish(fig, ax):
    ax.grid(True, linestyle=":", linewidth=0.5)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_generic(cfg, run_id):
    """Line chart for kinds with a continuous per-channel time series (heater_log/mvd/waferlog)."""
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    title, x_label, y_label, series = get_chart_data(cfg, run_id)
    plotted = False
    for name, (x_values, y_values) in sorted(series.items()):
        points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
        if not points:
            continue
        xs, ys = zip(*points)
        ax.plot(xs, ys, marker=".", markersize=2, linewidth=1, label=name)
        plotted = True
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if plotted:
        ax.legend(loc="upper left", fontsize=7, ncol=2)
    else:
        ax.text(0.5, 0.5, "No numeric data to plot", ha="center", va="center", transform=ax.transAxes)
    return _finish(fig, ax)


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


def _render_eventlog(cfg, run_id):
    """Scatter timeline of events per module, faults highlighted (eventlog)."""
    title, modules, points = get_eventlog_timeline(cfg, run_id)
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


_RENDERERS = {
    "cobra_job": _render_cobra,
    "eventlog": _render_eventlog,
}


def render_chart_png(cfg, run_id=None):
    try:
        renderer = _RENDERERS.get(cfg["kind"], _render_generic)
        return renderer(cfg, run_id)
    except ToolDataError as e:
        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
        ax.text(0.5, 0.5, str(e), ha="center", va="center", wrap=True, transform=ax.transAxes)
        return _finish(fig, ax)


def _line_series_json(series):
    """{"name": (x_values, y_values)} -> [{"name", "x", "y"}], dropping null y points the same
    way the matplotlib renderers do."""
    result = []
    for name, (x_values, y_values) in sorted(series.items()):
        points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
        if not points:
            continue
        xs, ys = zip(*points)
        result.append({"name": name, "x": list(xs), "y": list(ys)})
    return result


def get_chart_json(cfg, run_id=None):
    """Browser-rendered-chart counterpart to render_chart_png()/render_stream_chart_png() below -
    same reader functions, same per-kind shape, but returned as a JSON-serializable dict for
    NEMO_smart_lab.static.NEMO_smart_lab.js.smart_lab_charts.js (Chart.js) to draw an interactive
    canvas from, instead of a static server-rendered PNG. The PNG endpoints are unchanged and kept
    as a "download as image" option alongside the canvas."""
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
        title, x_label, y_label, series = get_chart_data(cfg, run_id)
        return {
            "chart_type": "line",
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "series": _line_series_json(series),
        }
    except ToolDataError as e:
        return {"chart_type": "error", "message": str(e)}


def get_stream_chart_json(cfg):
    try:
        title, x_label, y_label, series = get_stream_chart_data(cfg)
        return {
            "chart_type": "line",
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "series": _line_series_json(series),
        }
    except ToolDataError as e:
        return {"chart_type": "error", "message": str(e)}


def render_stream_chart_png(cfg):
    """Live PTIQ telemetry chart (Cobra tools only, and only if "stream_root" is configured) -
    always the single most recent minute of data, there's no history browsing for this."""
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    try:
        title, x_label, y_label, series = get_stream_chart_data(cfg)
        plotted = False
        for name, (x_values, y_values) in sorted(series.items()):
            points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
            if not points:
                continue
            xs, ys = zip(*points)
            ax.plot(xs, ys, linewidth=1, label=name)
            plotted = True
        ax.set_title(title)
        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        if plotted:
            ax.legend(loc="upper left", fontsize=7, ncol=2)
        else:
            ax.text(0.5, 0.5, "No numeric data to plot", ha="center", va="center", transform=ax.transAxes)
    except ToolDataError as e:
        ax.text(0.5, 0.5, str(e), ha="center", va="center", wrap=True, transform=ax.transAxes)
    return _finish(fig, ax)
