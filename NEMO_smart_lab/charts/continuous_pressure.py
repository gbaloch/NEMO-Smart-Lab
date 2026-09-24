"""The tool's continuous background pressure chart JSON."""

from NEMO_smart_lab.readers import get_continuous_pressure_trend


def get_continuous_pressure_chart_json(cfg, range_key=None):
    """The tool's continuous background pressure log (see readers.get_continuous_pressure_trend),
    shaped the exact same uPlot-ready way get_base_pressure_chart_json already is (real wall-clock
    x-axis, one series per gauge) - reused directly by js/charts's smartLabRenderChart,
    no new JS needed for this chart at all. `range_key` - see readers.CONTINUOUS_PRESSURE_RANGE_DAYS
    ("24h"/"7d"/"14d"/"1m"/"6m"/"1y", or "all"/anything else for the tool's entire history).
    "chart_type": "error" (not an exception) when this tool has no continuous_pressure_subdir
    configured, or no matching files exist at all; a distinct message when it's configured but has
    no data in the selected range (see readers.get_continuous_pressure_trend's own docstring for
    why these are kept separate - e.g. "24h" right after a gap in syncing shouldn't look like "this
    tool was never set up")."""
    trend = get_continuous_pressure_trend(cfg, range_key=range_key)
    if trend is None:
        return {"chart_type": "error", "message": "No continuous pressure log configured or found for this tool."}
    range_labels = {
        "24h": "last 24 hours",
        "7d": "last 7 days",
        "14d": "last 14 days",
        "1m": "last month",
        "6m": "last 6 months",
        "1y": "last year",
    }
    # A missing range_key defaults to "24h" in readers.get_continuous_pressure_trend - mirror
    # that here so the title always matches what was actually fetched, instead of mislabeling the
    # default-load case as "all time".
    label = range_labels.get(range_key if range_key is not None else "24h", "all time")
    if not trend["timestamps"]:
        return {"chart_type": "error", "message": f"No continuous pressure data found for {label}."}
    return {
        "chart_type": "line",
        "title": f"Continuous chamber pressure ({label})",
        "x_label": "Date",
        "y_label": "Pressure",
        "time_x": True,
        "x": trend["timestamps"],
        "series": [{"name": gauge, "y": trend["series"][gauge]} for gauge in trend["gauges"]],
    }
