"""The heater_log reader kind's public entry points: summary, chart groups/data and run history."""

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError, _channel_label, _has_any_value
from NEMO_smart_lab.readers.heater_log.config import _heater_log_config_mfc_label
from NEMO_smart_lab.readers.heater_log.events import (
    _candidate_event_file_for_run_start,
    _heater_log_events_for_data,
    _heater_log_run_start,
)
from NEMO_smart_lab.readers.heater_log.parsing import (
    _heater_log_channel_num,
    _parse_heater_log,
    _sibling_run_group,
)
from NEMO_smart_lab.readers.heater_log.runs import (
    _heater_log_local_path,
    _heater_log_run_end,
    _list_heater_log_entries,
    _resolve_heater_log_file,
)
from NEMO_smart_lab.readers.run_names import _filter_run_entries


def _heater_log_summary(name, cfg, run_id=None):
    path = _resolve_heater_log_file(cfg, run_id)
    data = _parse_heater_log(path)
    default_threshold = cfg.get("on_threshold_c", 35.0)
    channels = []
    for channel, value in sorted(data["latest"].items()):
        raw_name = channel.strip()
        display_name, role, hidden, channel_threshold = _channel_label(cfg, raw_name)
        if hidden:
            continue
        threshold = channel_threshold if channel_threshold is not None else default_threshold
        on = value is not None and value > threshold
        channels.append(
            {
                "name": display_name,
                "raw_name": raw_name,
                "channel_num": _heater_log_channel_num(raw_name, cfg),
                "role": role,
                "latest_value": value,
                "unit": "°C",
                "on": on,
            }
        )
    channels.sort(key=lambda c: c["name"])
    # Same alarm_count _heater_log_history computes per row on the run history page - here it's
    # for this one run's own detail page, replacing the old plain ON/Idle "Status" row (which just
    # duplicated what's already visible in the per-channel table below) with the more useful "how
    # many alarms fired during this run" figure.
    alarm_count = sum(1 for _offset, _module, _text, is_fault in _heater_log_events_for_data(cfg, data) if is_fault)
    return {
        "name": name,
        "kind": "heater_log",
        "run_id": data["run_id"],
        "source_file": data["run_id"],
        "recipe": data["recipe"] or "(unknown)",
        "last_update": _heater_log_run_end(data),
        "channels": channels,
        "any_on": any(c["on"] for c in channels),
        "status_label": "ON" if any(c["on"] for c in channels) else "Idle",
        "status_class": "warning" if any(c["on"] for c in channels) else "success",
        "alarm_count": alarm_count,
        "run_duration_s": data["time_s"][-1] if data["time_s"] else None,
        "extra": {"cycles_remaining": data["cycles_remaining"]},
    }


def _heater_log_chart_groups(cfg, run_id=None):
    """Every chartable signal a heater_log-kind tool's raw data actually carries for this run:
    per-channel temperature (group 0 - unchanged from before this existed), "MFC 1" flow (sccm),
    and - confirmed live on Oak, a real gap this used to have - two more sibling per-run log
    folders every fiji1/fiji2/fiji3/savannah tool has alongside "Heater Data": "Pressure Data"
    (chamber pressure, Torr - confirmed via Setup.ini.txt's PressGauge*Units) and "RF Data"
    (forward/reflected plasma power, W)."""
    path = _resolve_heater_log_file(cfg, run_id)
    data = _parse_heater_log(path)
    title = f"Recipe: {data['recipe'] or '(unknown)'}"

    temp_series = {}
    for raw_name, values in data["channel_series"].items():
        display_name, _role, hidden, _threshold = _channel_label(cfg, raw_name.strip())
        if hidden:
            continue
        temp_series[display_name] = (data["time_s"], values)

    groups = [{"key": "temperature", "label": "Temperature (°C)", "title": title, "x_label": "Time (s)", "y_label": "Temperature (°C)", "series": temp_series}]

    # Pressure right after temperature (before MFC flow/RF power) - the two signals a viewer
    # checks first for "is this chamber behaving normally", so they shouldn't be split apart by
    # whatever other groups a given run happens to also have.
    pressure_group = _sibling_run_group(cfg, data["run_id"], "Pressure Data", 1, "pressure", "Pressure (Torr)", title, "Pressure (Torr)")
    if pressure_group:
        groups.append(pressure_group)

    mfc_label = _heater_log_config_mfc_label(cfg) or "MFC 1"
    mfc_series = {mfc_label: (data["time_s"], data["mfc_1_series"])}
    if _has_any_value(mfc_series):
        groups.append({"key": "mfc_flow", "label": "MFC Flow (sccm)", "title": title, "x_label": "Time (s)", "y_label": "Flow (sccm)", "series": mfc_series})

    rf_group = _sibling_run_group(cfg, data["run_id"], "RF Data", 2, "rf_power", "RF Power (W)", title, "Power (W)")
    if rf_group:
        groups.append(rf_group)

    return groups


def _heater_log_chart_data(cfg, run_id=None):
    group = _heater_log_chart_groups(cfg, run_id)[0]
    return group["title"], group["x_label"], group["y_label"], group["series"]


def _heater_log_history(cfg, page, page_size, recipe=None, user_windows=None, start_date=None, end_date=None):
    # Listing every run's name+mtime is cheap (one cached remote listing, or a local stat per
    # file) so it's done over every run; only the one page actually being displayed gets fetched
    # (if remote) and parsed.
    all_entries = _filter_run_entries(cfg, _list_heater_log_entries(cfg), recipe, user_windows, start_date, end_date)
    start = (page - 1) * page_size
    page_entries = all_entries[start : start + page_size]
    tool = cfg.get("remote_tool")
    if tool is not None:
        # Pre-fetch this page's worth of runs concurrently - one at a time would serialize a full
        # SSH round trip per run (~1.5s each against Oak), badly multiplying across a page.
        remote_cache.ensure_cached_many(tool, [f"Logfile/Heater Data/{name}" for name, _mtime in page_entries])
    threshold = cfg.get("on_threshold_c", 35.0)

    parsed = []
    for name, _mtime in page_entries:
        try:
            path = _heater_log_local_path(cfg, name)
            parsed.append(_parse_heater_log(path))
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue

    if tool is not None:
        # Every distinct session Event Files entry this page's runs could need for their alarm
        # count (_heater_log_events_for_data, called per row below) - prefetched concurrently up
        # front, same reasoning as the Heater Data prewarm above. Without this, alarm counting used
        # to call ensure_cached() for each row's own event file one row at a time, in run order -
        # for a page where most rows land in a handful of session files this mostly hit an
        # already-warm cache after the first row that needed each one, but a scan_limit=300
        # fault-rate trend (see get_fault_rate_trend) can easily span dozens of distinct,
        # never-before-fetched session files, which serialized into dozens of sequential ~1.5s SSH
        # round trips - confirmed live as a real, previously-hidden chunk of "Health" page latency
        # on top of the (already-concurrent) Heater Data fetch above.
        event_names = {
            candidate
            for data in parsed
            for candidate in [_candidate_event_file_for_run_start(cfg, _heater_log_run_start(data))]
            if candidate is not None
        }
        remote_cache.ensure_cached_many(tool, [f"Logfile/Event Files/{name}" for name in event_names])

    history = []
    for data in parsed:
        latest_values = [v for v in data["latest"].values() if v is not None]
        alarm_count = sum(1 for _offset, _module, _text, is_fault in _heater_log_events_for_data(cfg, data) if is_fault)
        history.append(
            {
                "run_id": data["run_id"],
                "recipe": data["recipe"] or "(unknown)",
                "timestamp": _heater_log_run_end(data),
                "duration_s": data["time_s"][-1] if data["time_s"] else None,
                "any_on": any(v > threshold for v in latest_values),
                "status_label": "ON" if any(v > threshold for v in latest_values) else "Idle",
                "status_class": "warning" if any(v > threshold for v in latest_values) else "success",
                "alarm_count": alarm_count,
                "faulty": alarm_count > 0,
            }
        )
    return history, len(all_entries)
