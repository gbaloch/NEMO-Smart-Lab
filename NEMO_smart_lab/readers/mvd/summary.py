"""The mvd reader kind's public entry points: summary, chart groups/data and run history."""

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError, _channel_label, _has_any_value, _last_non_null
from NEMO_smart_lab.readers.mvd.config import (
    _mvd_config_heater_labels,
    _mvd_config_mfc_labels,
    _mvd_mfc_display_name,
)
from NEMO_smart_lab.readers.mvd.data import _mvd_run_data, _mvd_run_history_summary
from NEMO_smart_lab.readers.mvd.runs import _list_mvd_run_entries, _mvd_run_end, _mvd_run_local_path
from NEMO_smart_lab.readers.run_names import _filter_run_entries


def _mvd_summary(name, cfg, run_id=None):
    data = _mvd_run_data(cfg, run_id)
    threshold = cfg.get("on_threshold_pct", 0.5)
    labels = {**data["summary"]["heater_labels"], **_mvd_config_heater_labels(cfg)}
    channels = []
    for num in sorted(data["temp_series"], key=int):
        auto_label = labels.get(num, "").strip() or f"Heater {num}"
        # An admin-configured override (keyed by the bare HTR number, e.g. "6") wins over the
        # auto-parsed _SUM.txt label; _channel_label falls back to returning `num` itself
        # unchanged when there's no override, in which case fall back further to auto_label.
        display_name, role, hidden, _threshold = _channel_label(cfg, num)
        if hidden:
            continue
        if display_name == num:
            display_name = auto_label
        latest_temp = _last_non_null(data["temp_series"][num])
        latest_duty = _last_non_null(data["duty_series"].get(num, []))
        on = latest_duty is not None and latest_duty > threshold
        channels.append(
            {
                "name": display_name,
                # Shown as its own "#" column on the detail page (not folded into the name/role
                # subtitle text) - e.g. "Chamber" / "HTR14", not "Chamber (HTR14)". mvd's own HTR
                # numbering already matches the physical/recipe channel number directly (unlike
                # heater_log-kind tools - see _heater_log_channel_num), so channel_num needs no
                # offset here.
                "raw_name": f"HTR{num}",
                "channel_num": int(num),
                "role": role,
                "latest_value": latest_temp,
                "unit": "°C",
                "duty_pct": latest_duty,
                "on": on,
            }
        )
    return {
        "name": name,
        "kind": "mvd",
        "run_id": data["run_id"],
        "source_file": data["run_id"],
        "recipe": data["summary"]["recipe"] or "(unknown)",
        "last_update": _mvd_run_end(data),
        "channels": channels,
        "any_on": any(c["on"] for c in channels),
        "status_label": "ON" if any(c["on"] for c in channels) else "Idle",
        "status_class": "warning" if any(c["on"] for c in channels) else "success",
        "run_duration_s": data["time_s"][-1] if data["time_s"] else None,
        "extra": {
            "status": data["summary"]["status"],
            "completion_status": data["summary"]["completion_status"],
            "run_time": data["summary"]["run_time"],
        },
    }


_MVD_UNIT_GROUP_LABELS = {
    "sccm": "Flow (sccm)",
    "W": "Power (W)",
    "rpm": "Speed (rpm)",
    "V": "Voltage (V)",
    "%": "Percent (other)",
}


def _mvd_chart_groups(cfg, run_id=None):
    """Every chartable signal an mvd-kind DAT file actually carries - not just heater
    temperature. mvd's own DAT format (mvd/fiji5) already encodes each column's physical unit
    in its header (e.g. "(sccm)", "(W)"), so unlike heater_log this classifies columns generically
    by that unit rather than needing per-tool column names hardcoded - see _UNIT_SUFFIX_RE."""
    data = _mvd_run_data(cfg, run_id)
    labels = {**data["summary"]["heater_labels"], **_mvd_config_heater_labels(cfg)}
    title = f"Recipe: {data['summary']['recipe'] or '(unknown)'}"
    time_s = data["time_s"]

    def htr_label(num):
        auto_label = labels.get(num, "").strip() or f"Heater {num}"
        display_name, _role, hidden, _threshold = _channel_label(cfg, num)
        if hidden:
            return None
        if display_name == num:
            display_name = auto_label
        return f"{display_name} (HTR{num})"

    def htr_group(key, label, y_label, source, name_suffix=""):
        series = {}
        for num, values in source.items():
            channel_label = htr_label(num)
            if channel_label:
                series[f"{channel_label}{name_suffix}"] = (time_s, values)
        return {"key": key, "label": label, "title": title, "x_label": "Time (s)", "y_label": y_label, "series": series}

    groups = [htr_group("temperature", "Temperature (°C)", "Temperature (°C)", data["temp_series"])]
    duty_group = htr_group("duty", "Heater duty (%)", "Duty (%)", data["duty_series"])
    if _has_any_value(duty_group["series"]):
        groups.append(duty_group)
    ramp_rate_group = htr_group("ramp_rate", "Heater ramp rate (°C)", "Ramp rate (°C)", data["ramp_rate_series"], " ramp rate")
    if _has_any_value(ramp_rate_group["series"]):
        groups.append(ramp_rate_group)

    # Non-heater columns, bucketed by their own raw unit (a bare "%" here - e.g. a match-network
    # Load/Tune reading - is kept separate from "Heater duty (%)" above, since the two percentages
    # mean different things and shouldn't share an axis). MFC columns get their real config.ini
    # name here too (see _mvd_mfc_display_name) - unlike heater channels, no per-run file has any
    # MFC label field at all, so config.ini is the only source, not just a preferred one.
    mfc_labels = _mvd_config_mfc_labels(cfg)
    by_unit = {}
    for name, (unit, values) in data["other_series"].items():
        by_unit.setdefault(unit, {})[_mvd_mfc_display_name(name, mfc_labels)] = (time_s, values)

    for unit in ("sccm", "W", "rpm", "V", "%"):
        series = by_unit.pop(unit, None)
        if series and _has_any_value(series):
            label = _MVD_UNIT_GROUP_LABELS[unit]
            key = "percent_other" if unit == "%" else unit
            groups.append({"key": key, "label": label, "title": title, "x_label": "Time (s)", "y_label": label, "series": series})

    leftover = {name: values for series in by_unit.values() for name, values in series.items()}
    if leftover and _has_any_value(leftover):
        groups.append({"key": "other", "label": "Other", "title": title, "x_label": "Time (s)", "y_label": "Value", "series": leftover})

    return groups


def _mvd_chart_data(cfg, run_id=None):
    group = _mvd_chart_groups(cfg, run_id)[0]
    return group["title"], group["x_label"], group["y_label"], group["series"]


def _mvd_history(cfg, page, page_size, recipe=None, user_windows=None, start_date=None, end_date=None):
    # Listing every run folder's name+mtime is cheap (one cached remote listing, or a local stat
    # per folder) so it's done over every run; only the one page actually being displayed gets
    # fetched (if remote) and parsed.
    all_entries = _filter_run_entries(cfg, _list_mvd_run_entries(cfg), recipe, user_windows, start_date, end_date)
    start = (page - 1) * page_size
    page_entries = all_entries[start : start + page_size]
    tool = cfg.get("remote_tool")
    if tool is not None:
        remote_cache.ensure_cached_many(tool, [f"log/data/{name}" for name, _mtime in page_entries], is_dir=True)
    threshold = cfg.get("on_threshold_pct", 0.5)
    # mvd/fiji5 have no alarm log wired into the history listing the way heater_log does - its own
    # _SUM.txt completion_status is the equivalent real signal (confirmed live across two real
    # tools: mvd itself says "Successfully completed" / "Recipe stopped - Manual stop", while
    # fiji5 - a different mvd-kind instance, evidently a different software version - says
    # "Successfully Completed" with a capital C; a real bug, caught live, was comparing this
    # case-sensitively against only the lowercase spelling, which flagged every single one of
    # fiji5's normal, successful runs as "faulty") - anything other than a clean completion
    # (case-insensitively) counts as "faulty" for the tool detail page's recent-problems panel
    # (get_recent_faulty_runs) - see _mvd_run_history_summary for exactly how each field here is
    # derived (and why it's cached separately from the run's own full parsed series).
    history = []
    for name, _mtime in page_entries:
        try:
            run_dir = _mvd_run_local_path(cfg, name)
            history.append(_mvd_run_history_summary(run_dir, threshold))
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue
    return history, len(all_entries)
