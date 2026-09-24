"""Live PTIQ telemetry chart (Cobra tools): JSON and PNG."""

import matplotlib

matplotlib.use("Agg")


import matplotlib.pyplot as plt

from NEMO_smart_lab.readers import ToolDataError, get_stream_chart_data
from NEMO_smart_lab.charts.common import LINE_CHART_COLORS, _finish
from NEMO_smart_lab.charts.line_json import _line_series_json


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
