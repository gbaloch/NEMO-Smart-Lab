"""Smart Lab views. Split by page/purpose; every view (and the helpers other modules import) is re-exported here,
so `from NEMO_smart_lab import views` / `views.tool_detail` (see urls.py) keep working."""

# flake8: noqa: F401
from NEMO_smart_lab.views.access import (
    _can_access_smart_lab,
    smart_lab_access_required,
    staging_api_key_required,
)
from NEMO_smart_lab.views.chart_data import (
    tool_base_pressure_chart,
    tool_base_pressure_csv,
    tool_base_pressure_data,
    tool_chart,
    tool_chart_data,
    tool_continuous_pressure_data,
    tool_screenshot,
    tool_stream_chart,
    tool_stream_chart_data,
)
from NEMO_smart_lab.views.dashboard import (
    dashboard,
    tool_sync_map,
)
from NEMO_smart_lab.views.data import (
    tool_config_detail,
    tool_configs,
    tool_data,
    tool_recipe_detail,
    tool_recipe_duplicates,
    tool_recipe_toggle_pin,
    tool_recipes,
)
from NEMO_smart_lab.views.helpers import (
    UNCATEGORIZED,
    _base_pressure_recipe_names_html,
    _grouped_config_files,
    _grouped_recipes,
    _named_summary_and_status,
    _page_numbers,
    _parse_date_param,
    _parse_float,
    _primary_run_user,
    _recipe_name_choices,
    _recipe_run_counts,
    _resolve,
    _tool_group_label,
)
from NEMO_smart_lab.views.history import (
    HISTORY_PAGE_SIZE_CHOICES,
    tool_history,
)
from NEMO_smart_lab.views.maintenance import (
    _maintenance_trends_context,
    tool_maintenance_trends,
)
from NEMO_smart_lab.views.tool_detail import (
    tool_detail,
)
