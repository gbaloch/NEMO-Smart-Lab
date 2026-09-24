"""Chamber base pressure over time: interactive JSON, PNG and CSV."""

import matplotlib

matplotlib.use("Agg")


import csv
import io
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

from NEMO_smart_lab.readers import get_base_pressure_history
from NEMO_smart_lab.charts.common import LINE_CHART_COLORS, _finish


def _base_pressure_title(cfg):
    # Names which exact standby recipe(s) this trend is built from, right in the chart's own
    # title - the graph is meaningless without knowing that, and a viewer looking at just the
    # chart (or its downloaded PNG) shouldn't have to go back to the tool detail page's info table
    # to find out.
    recipes = cfg.get("base_pressure_recipe_names") or ""
    names = [r.strip() for r in recipes.split(",") if r.strip()]
    return f"Chamber base pressure ({', '.join(names)})" if names else "Chamber base pressure (standby)"


# Only offered once there's actually more than a year of history to narrow down (see
# get_base_pressure_chart_json's "full_range_days" - js/charts shows the dropdown only
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
    js/charts's smartLabRenderUplot to use uPlot's time-scale/date-axis mode instead of
    the plain-seconds mode every other chart here uses.

    `range_key` (see BASE_PRESSURE_RANGE_DAYS) narrows to only the last N years of an otherwise
    long history - "full_range_days" in the response (always computed off the *unfiltered* full
    history, regardless of `range_key`) is how the JS side decides whether the range dropdown is
    even worth showing in the first place.

    "point_run_ids" (parallel to "x", one per point) is each point's own run_id - clicking a point
    (js/charts's smartLabRenderUplot) jumps straight to that specific standby run's full
    detail page, since a bare pressure number on its own isn't nearly as useful as being able to go
    look at the actual run that produced it.

    "point_recipes" (also parallel to "x") is each point's own recipe name - a tool can configure
    more than one standby recipe (SmartLabTool.base_pressure_recipe_names is a list, not a single
    name - see get_base_pressure_history's own docstring for why), so a bare pressure trend alone
    doesn't say which of them produced any given point; smartLabRenderUplot shows this as a small
    label under the chart, updated as the cursor moves over each point."""
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
        "point_run_ids": [p["run_id"] for p in points],
        "point_recipes": [p["recipe"] for p in points],
    }


def render_base_pressure_chart_png(cfg, range_key=None):
    """PNG "download as image" counterpart to get_base_pressure_chart_json - real wall-clock dates
    on the x-axis (matplotlib handles Python datetimes natively), unlike every other PNG chart
    here (all plain seconds-since-run-start). `range_key` matches whatever range is currently
    selected on the live chart (see js/charts), so downloading an image reflects what's
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


def _csv_safe(value):
    """Defuses CSV/formula injection (OWASP-standard mitigation): a cell whose text starts with
    "=", "+", "-", or "@" is treated as a formula by Excel/LibreOffice/Sheets when the file is
    opened, not as literal text - dangerous here because `recipe`/`run_id` ultimately come from
    filenames an instrument operator chose on the tool PC (see get_base_pressure_history), not
    from the NEMO user who ends up downloading and opening this CSV. Prefixing with a single
    quote forces spreadsheet apps to render the value as plain text instead of evaluating it,
    without changing what a human (or csv.reader) sees the value as."""
    text = str(value)
    return "'" + text if text and text[0] in "=+-@" else text


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
    writer.writerow(["timestamp", "value", "unit", "run_id", "recipe"])
    for p in points:
        writer.writerow([p["timestamp"].isoformat(), p["value"], p["unit"], _csv_safe(p["run_id"]), _csv_safe(p["recipe"])])
    return buf.getvalue()
