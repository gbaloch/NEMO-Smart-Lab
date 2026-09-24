"""Readers for the raw process-log formats produced by the tools configured in
NEMO_smart_lab.config.SMART_LAB_TOOL_SOURCES.

These are intentionally read-only and stateless: every call re-reads whatever run file/folder
is asked for at the time of the request (defaulting to the most recent one), so the plugin
always reflects current tool state without needing a separate collector process or database.
Every "run" (one file for heater_log tools, one folder for mvd tools) has a stable "run_id" -
its filename or folder name - that callers can pass back in to look up that specific run again
for the history view.

This package used to be one module (readers.py); it is now split by tool kind / file format - see the
submodules. Every name that module exposed is re-exported here, so `from NEMO_smart_lab.readers import ...`
keeps working unchanged.
"""

# flake8: noqa: F401
from NEMO_smart_lab.readers.base_pressure import (
    _MAX_SYNCHRONOUS_FETCHES_PER_REQUEST,
    _bounded_prewarm,
    _remote_dir_names,
    get_base_pressure_history,
)
from NEMO_smart_lab.readers.cobra import (
    _COBRA_BAD_STATUSES,
    _cobra_db_path,
    _cobra_duration,
    _cobra_guid_to_bytes,
    _cobra_guid_to_str,
    _cobra_history,
    _cobra_most_recent_task_id,
    _cobra_open_connection,
    _cobra_parse_datetime,
    _cobra_read_job,
    _cobra_resolve_task_id,
    _cobra_summary,
    get_cobra_step_timeline,
)
from NEMO_smart_lab.readers.cobra.stream import (
    _STREAM_INTERESTING_CHANNEL_SUFFIXES,
    _fix_stream_float,
    _interesting_stream_channels,
    _latest_stream_file,
    _parse_stream_file,
    get_stream_chart_data,
    get_stream_summary,
)
from NEMO_smart_lab.readers.common import (
    DEFAULT_HISTORY_LIMIT,
    EmptyRunFileError,
    FAULT_KEYWORDS,
    FILE_ENCODING,
    PARSED_FILE_CACHE_TTL,
    TERMINATING_EVENTS,
    ToolDataError,
    _average_tail,
    _cached_file_parse,
    _channel_label,
    _file_fingerprint,
    _find_one,
    _has_any_value,
    _last_non_null,
)
from NEMO_smart_lab.readers.continuous_pressure import (
    CONTINUOUS_PRESSURE_RANGE_DAYS,
    CONTINUOUS_PRESSURE_WINDOW_DAYS,
    _CONTINUOUS_PRESSURE_BUCKET_MINUTES,
    _CONTINUOUS_PRESSURE_FILENAME_RE,
    _CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT,
    _MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST,
    _continuous_pressure_filename_timestamp,
    _continuous_pressure_row_timestamp,
    _fast_continuous_pressure_timestamp,
    _parse_and_bucket_continuous_pressure_file,
    get_continuous_pressure_trend,
)
from NEMO_smart_lab.readers.dispatch import (
    _CHART_FUNCS,
    _CHART_GROUP_FUNCS,
    _HISTORY_FUNCS,
    _SUMMARY_FUNCS,
    _mark_shared_roles,
    _single_chart_group,
    get_chart_data,
    get_chart_group_list,
    get_chart_groups,
    get_run_screenshot,
    get_tool_history,
    get_tool_summary,
    tool_wide_role_counts,
)
from NEMO_smart_lab.readers.eventlog import (
    _eventlog_history,
    _eventlog_parse_timestamp,
    _eventlog_path,
    _eventlog_process_run_indices,
    _eventlog_read_run,
    _eventlog_rows,
    _eventlog_summary,
    get_eventlog_timeline,
)
from NEMO_smart_lab.readers.heater_log.config import (
    _INI_MFC1_LABEL_RE,
    _heater_log_config_mfc_label,
)
from NEMO_smart_lab.readers.heater_log.events import (
    _EVENT_FILE_NAME_RE,
    _EVENT_LINE_RE,
    _EVENT_WINDOW_PAD,
    _candidate_event_file_for_run_start,
    _event_file_timestamp,
    _heater_log_events_for_data,
    _heater_log_run_start,
    _list_event_files,
    _parse_event_file,
    get_heater_log_run_events,
)
from NEMO_smart_lab.readers.heater_log.parsing import (
    _HEATER_LOG_NUM_RE,
    _TRAILING_COLUMNS,
    _heater_log_channel_num,
    _parse_heater_log,
    _parse_heater_log_uncached,
    _parse_simple_run_log,
    _sibling_run_group,
)
from NEMO_smart_lab.readers.heater_log.runs import (
    _MAX_EMPTY_RUNS_TO_SKIP,
    _heater_log_dir,
    _heater_log_file_by_run_id,
    _heater_log_local_path,
    _heater_log_run_end,
    _list_heater_log_entries,
    _resolve_heater_log_file,
)
from NEMO_smart_lab.readers.heater_log.screenshots import (
    _heater_log_screenshot_path,
    _matches_screenshot_pattern,
    _screenshot_base_name,
)
from NEMO_smart_lab.readers.heater_log.summary import (
    _heater_log_chart_data,
    _heater_log_chart_groups,
    _heater_log_history,
    _heater_log_summary,
)
from NEMO_smart_lab.readers.mvd.config import (
    _INI_HEATER_LABEL_RE,
    _INI_MFC_LABEL_RE,
    _MFC_CHANNEL_RE,
    _mvd_config_heater_labels,
    _mvd_config_ini_text,
    _mvd_config_mfc_labels,
    _mvd_mfc_display_name,
)
from NEMO_smart_lab.readers.mvd.data import (
    _mvd_run_data,
    _mvd_run_data_for_dir,
    _mvd_run_data_for_dir_uncached,
    _mvd_run_history_summary,
)
from NEMO_smart_lab.readers.mvd.events import (
    _MVD_EVT_CATEGORY_RE,
    _MVD_EVT_TIMESTAMP_RE,
    _parse_mvd_evt,
    _parse_mvd_evt_timestamp,
    get_mvd_run_events,
)
from NEMO_smart_lab.readers.mvd.parsing import (
    _HEATER_LABEL_RE,
    _HTR_DUTY_RE,
    _HTR_RAMP_RATE_RE,
    _HTR_TEMP_RE,
    _UNIT_SUFFIX_RE,
    _parse_mvd_dat,
    _parse_mvd_pt,
    _parse_mvd_summary_text,
)
from NEMO_smart_lab.readers.mvd.pressure import (
    _MVD_PRIMARY_PRESSURE_KEYWORDS,
    _mvd_default_visible_pressure_channel,
    _mvd_pressure_group_key,
    _mvd_pressure_group_list,
    _mvd_pt_path,
    get_mvd_pressure_group,
)
from NEMO_smart_lab.readers.mvd.runs import (
    _list_mvd_run_entries,
    _mvd_data_dir,
    _mvd_run_dir_by_run_id,
    _mvd_run_end,
    _mvd_run_local_path,
    _mvd_screenshot_path,
    _resolve_mvd_run_dir,
    _run_dir_sort_key,
)
from NEMO_smart_lab.readers.mvd.summary import (
    _MVD_UNIT_GROUP_LABELS,
    _mvd_chart_data,
    _mvd_chart_groups,
    _mvd_history,
    _mvd_summary,
)
from NEMO_smart_lab.readers.run_lookup import (
    count_runs_by_recipe_name,
    count_runs_for_recipe,
    get_latest_run_id,
    get_run_page_number,
    get_run_time_range,
)
from NEMO_smart_lab.readers.run_names import (
    _HEATER_LOG_FILENAME_RE,
    _MVD_FOLDER_NAME_RE,
    _filter_run_entries,
    _heater_log_filename_timestamp,
    _mvd_folder_timestamp,
    _recipe_from_run_id,
    _run_start_timestamp,
)
from NEMO_smart_lab.readers.trends import (
    _MFC_READING_RE,
    _MFC_SETPOINT_RE,
    _MVD_SIGNAL_SCAN_LIMIT,
    _extract_mfc_drift,
    _extract_rf_reflected_fraction,
    _extract_turbo_speed,
    _mvd_run_maintenance_signals,
    _mvd_weekly_signal_trend,
    _pump_down_time_s,
    _run_pressure_series,
    get_fault_rate_trend,
    get_mfc_drift_trend,
    get_mvd_maintenance_trends,
    get_pump_down_time,
    get_pump_down_trend,
    get_recent_faulty_runs,
    get_recent_runs,
    get_rf_health_trend,
    get_turbo_speed_trend,
)
from NEMO_smart_lab.readers.waferlog import (
    _GAS_KEYS,
    _list_waferlog_entries,
    _parse_waferlog,
    _resolve_waferlog_file,
    _waferlog_chart_data,
    _waferlog_dir,
    _waferlog_file_by_run_id,
    _waferlog_history,
    _waferlog_local_path,
    _waferlog_summary,
)
