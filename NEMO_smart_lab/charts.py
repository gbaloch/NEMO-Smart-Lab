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

# Same palette as static/NEMO_smart_lab/js/smart_lab_charts.js's SMART_LAB_CHART_COLORS, so a
# channel's line color matches between the interactive Chart.js canvas and its "download as image"
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


def _render_generic(cfg, run_id):
    """Line chart for kinds with a continuous per-channel time series (heater_log/mvd/waferlog)."""
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    title, x_label, y_label, series = get_chart_data(cfg, run_id)
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


# A long run (e.g. a multi-hour Fiji5 DAT.txt logging every fraction of a second) can carry tens
# of thousands of points per channel; sending/rendering all of them made both the network payload
# and the browser chart itself noticeably laggy for no real visual benefit past what a chart a few
# hundred pixels wide can even distinguish. Downsample anything past this many points per series.
LINE_CHART_MAX_POINTS = 2000


def _lttb_downsample(xs, ys, threshold):
    """Largest-Triangle-Three-Buckets: reduces (xs, ys) to `threshold` points while preserving the
    visual shape (spikes, transitions) far better than naively keeping every Nth point would -
    a naive stride can silently skip straight over a brief spike; LTTB is specifically designed
    to keep whichever point in each "bucket" would visually matter most. No-op below `threshold`.
    """
    n = len(xs)
    if threshold >= n or threshold <= 2:
        return xs, ys

    sampled_x = [xs[0]]
    sampled_y = [ys[0]]
    bucket_size = (n - 2) / (threshold - 2)
    a = 0

    for i in range(threshold - 2):
        next_start = min(int((i + 2) * bucket_size) + 1, n - 1)
        next_end = min(int((i + 3) * bucket_size) + 1, n)
        next_bucket_x = xs[next_start:next_end] or [xs[-1]]
        next_bucket_y = ys[next_start:next_end] or [ys[-1]]
        avg_x = sum(next_bucket_x) / len(next_bucket_x)
        avg_y = sum(next_bucket_y) / len(next_bucket_y)

        point_ax, point_ay = xs[a], ys[a]
        bucket_start = min(int((i + 1) * bucket_size) + 1, n - 1)
        bucket_end = min(int((i + 2) * bucket_size) + 1, n)

        max_area = -1.0
        max_area_index = bucket_start
        for j in range(bucket_start, bucket_end):
            area = abs((point_ax - avg_x) * (ys[j] - point_ay) - (point_ax - xs[j]) * (avg_y - point_ay))
            if area > max_area:
                max_area = area
                max_area_index = j

        sampled_x.append(xs[max_area_index])
        sampled_y.append(ys[max_area_index])
        a = max_area_index

    sampled_x.append(xs[-1])
    sampled_y.append(ys[-1])
    return sampled_x, sampled_y


def _line_series_json(series):
    """{"name": (x_values, y_values)} -> [{"name", "x", "y", ["original_point_count"]}], dropping
    null y points the same way the matplotlib renderers do, then LTTB-downsampling anything past
    LINE_CHART_MAX_POINTS (original_point_count is only present when that happened, so the
    frontend can show a "downsampled" note)."""
    result = []
    for name, (x_values, y_values) in sorted(series.items()):
        points = [(x, y) for x, y in zip(x_values, y_values) if y is not None]
        if not points:
            continue
        xs, ys = zip(*points)
        xs, ys = list(xs), list(ys)
        entry = {"name": name}
        if len(xs) > LINE_CHART_MAX_POINTS:
            entry["original_point_count"] = len(xs)
            xs, ys = _lttb_downsample(xs, ys, LINE_CHART_MAX_POINTS)
        entry["x"] = xs
        entry["y"] = ys
        result.append(entry)
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
        series_json = _line_series_json(series)
        return {
            "chart_type": "line",
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "series": series_json,
            "downsampled": any("original_point_count" in s for s in series_json),
        }
    except ToolDataError as e:
        return {"chart_type": "error", "message": str(e)}


def get_stream_chart_json(cfg):
    try:
        title, x_label, y_label, series = get_stream_chart_data(cfg)
        series_json = _line_series_json(series)
        return {
            "chart_type": "line",
            "title": title,
            "x_label": x_label,
            "y_label": y_label,
            "series": series_json,
            "downsampled": any("original_point_count" in s for s in series_json),
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
