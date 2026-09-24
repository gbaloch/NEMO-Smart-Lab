"""Chart data and images for the Smart Lab pages, split by chart type. Everything the old single charts.py module
exposed is re-exported here, so `from NEMO_smart_lab.charts import ...` keeps working."""

import matplotlib

matplotlib.use("Agg")  # headless backend, must be set before pyplot is first imported anywhere


# flake8: noqa: F401
from NEMO_smart_lab.charts.base_pressure import (
    BASE_PRESSURE_RANGE_DAYS,
    _base_pressure_title,
    _csv_safe,
    _filter_base_pressure_range,
    get_base_pressure_chart_json,
    render_base_pressure_chart_png,
    render_base_pressure_csv,
)
from NEMO_smart_lab.charts.common import (
    LINE_CHART_COLORS,
    _FIXED_SERIES_COLORS,
    _finish,
    _resolve_chart_group,
    _series_color,
    _with_run_suffix,
)
from NEMO_smart_lab.charts.continuous_pressure import (
    get_continuous_pressure_chart_json,
)
from NEMO_smart_lab.charts.line_json import (
    _align_series,
    _line_series_json,
    get_chart_json,
)
from NEMO_smart_lab.charts.run_charts import (
    MAX_EVENTS_FOR_PNG,
    _RENDERERS,
    _render_cobra,
    _render_eventlog,
    _render_generic,
    _render_group,
    _render_scatter_timeline,
    render_chart_png,
)
from NEMO_smart_lab.charts.stream import (
    get_stream_chart_json,
    render_stream_chart_png,
)
