"""Public dispatch: routes a tool's configured reader kind to the right implementation."""

import os

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.readers.cobra import _cobra_history, _cobra_summary
from NEMO_smart_lab.readers.common import DEFAULT_HISTORY_LIMIT, ToolDataError
from NEMO_smart_lab.readers.eventlog import _eventlog_history, _eventlog_summary
from NEMO_smart_lab.readers.heater_log.events import get_heater_log_run_events
from NEMO_smart_lab.readers.heater_log.runs import _resolve_heater_log_file
from NEMO_smart_lab.readers.heater_log.screenshots import _heater_log_screenshot_path
from NEMO_smart_lab.readers.heater_log.summary import (
    _heater_log_chart_data,
    _heater_log_chart_groups,
    _heater_log_history,
    _heater_log_summary,
)
from NEMO_smart_lab.readers.mvd.events import get_mvd_run_events
from NEMO_smart_lab.readers.mvd.pressure import _mvd_pressure_group_list
from NEMO_smart_lab.readers.mvd.runs import _mvd_screenshot_path, _resolve_mvd_run_dir
from NEMO_smart_lab.readers.mvd.summary import _mvd_chart_data, _mvd_chart_groups, _mvd_history, _mvd_summary
from NEMO_smart_lab.readers.waferlog import _waferlog_chart_data, _waferlog_history, _waferlog_summary


_SUMMARY_FUNCS = {
    "heater_log": _heater_log_summary,
    "mvd": _mvd_summary,
    "cobra_job": _cobra_summary,
    "waferlog": _waferlog_summary,
    "eventlog": _eventlog_summary,
}


# cobra_job and eventlog use their own dedicated chart-data shapes (get_cobra_step_timeline,
# get_eventlog_timeline) instead of the generic (title, x_label, y_label, series) shape here -
# charts.py calls those directly, they're not in this dispatch table.
_CHART_FUNCS = {
    "heater_log": _heater_log_chart_data,
    "mvd": _mvd_chart_data,
    "waferlog": _waferlog_chart_data,
}


def _single_chart_group(chart_func, cfg, run_id=None):
    title, x_label, y_label, series = chart_func(cfg, run_id)
    return [{"key": "primary", "label": y_label, "title": title, "x_label": x_label, "y_label": y_label, "series": series}]


# heater_log/mvd expose every chartable signal a run's raw file actually carries (temperature,
# duty cycle, flow, power, ...) as a list of independent chart groups instead of just one. No
# currently-configured tool is waferlog kind, so there's no real data to design multi-group
# support against - it stays a single-group wrapper around its existing chart data.
_CHART_GROUP_FUNCS = {
    "heater_log": _heater_log_chart_groups,
    "mvd": _mvd_chart_groups,
    "waferlog": lambda cfg, run_id=None: _single_chart_group(_waferlog_chart_data, cfg, run_id),
}


_HISTORY_FUNCS = {
    "heater_log": _heater_log_history,
    "mvd": _mvd_history,
    "cobra_job": _cobra_history,
    "waferlog": _waferlog_history,
    "eventlog": _eventlog_history,
}


def tool_wide_role_counts(cfg):
    """How many of this tool's admin-configured channels (SmartLabToolChannel - the only source a
    "role" tag ever comes from at all, see _channel_label) share each given role - computed once,
    directly from cfg["channel_labels"], so it's available without needing a live run's data at
    all (recipes.py has no run to read "channels" off of the way get_tool_summary does). Passed
    into _mark_shared_roles as `role_counts` so a channel's role-subtitle visibility is decided by
    the SAME tool-wide picture everywhere it's shown - the live "Heater channels" table and a
    recipe's own "Heater setpoints" table alike - instead of each recalculating it from whatever
    smaller subset of channels happens to be in front of it (a recipe might only set a handful of
    the tool's channels, which used to make a role that's shared tool-wide look unique there, and
    hide the very subtitle that would tell you it isn't)."""
    counts = {}
    for _display_name, role, _hidden, _threshold in (cfg.get("channel_labels") or {}).values():
        if role and role != "other":
            counts[role] = counts.get(role, 0) + 1
    return counts


def _mark_shared_roles(channels, role_counts=None):
    """A channel's "role" (chuck/source_valve/delivery_line/...) is shown on the detail page as a
    small subtitle below its own name, e.g. "Cone" / "Chuck" - useful when it tells you Cone and
    another channel are both part of the same physical "Chuck" assembly. But when only one channel
    on the whole tool has a given role, the subtitle just repeats information the channel's own
    name/position already conveys (or duplicates the name outright, e.g. a channel literally named
    "Chuck" with role "chuck"). This doesn't touch "role" itself (still the real classification,
    used elsewhere e.g. per-channel on_threshold_c overrides) - it adds "role_shown", a display-only
    flag the template checks instead, true only when at least one other channel shares the role.

    `role_counts` - see tool_wide_role_counts - defaults to counting only within `channels` itself
    (the original behavior) when not given, for callers with no cheaper tool-wide source at hand."""
    if role_counts is None:
        role_counts = {}
        for c in channels:
            if c.get("role") and c["role"] != "other":
                role_counts[c["role"]] = role_counts.get(c["role"], 0) + 1
    for c in channels:
        c["role_shown"] = bool(c.get("role")) and c["role"] != "other" and role_counts.get(c["role"], 0) > 1
    return channels


def get_tool_summary(name, cfg, run_id=None):
    try:
        summary = _SUMMARY_FUNCS[cfg["kind"]](name, cfg, run_id)
        if summary.get("channels"):
            _mark_shared_roles(summary["channels"], tool_wide_role_counts(cfg))
        return summary
    except ToolDataError as e:
        return {"name": name, "kind": cfg["kind"], "run_id": run_id, "error": str(e)}


def get_run_screenshot(cfg, run_id=None):
    """Local filesystem path to a run's post-run report screenshot (heater_log's Reports/*.jpg,
    mvd's per-run-folder *.jpg), or None if this tool kind doesn't have one, the run itself can't
    be resolved, or no screenshot exists for that particular run."""
    try:
        if cfg["kind"] == "heater_log":
            path = _resolve_heater_log_file(cfg, run_id)
            return _heater_log_screenshot_path(cfg, os.path.basename(path))
        if cfg["kind"] == "mvd":
            # A run's whole folder (including its .jpg, if any) is fetched as one unit by
            # _resolve_mvd_run_dir in remote mode, so this works automatically either way.
            return _mvd_screenshot_path(_resolve_mvd_run_dir(cfg, run_id))
    except (ToolDataError, remote_sync.RemoteSyncError):
        return None
    return None


def get_chart_data(cfg, run_id=None):
    """Returns (title, x_label, y_label, {series_name: (x_values, y_values)}) - always exactly
    get_chart_groups()[0] (the Temperature group), kept as its own function for every existing
    caller that only ever wanted the one chart this used to be the only option."""
    return _CHART_FUNCS[cfg["kind"]](cfg, run_id)


def get_chart_groups(cfg, run_id=None):
    """Every independent chart this tool's raw data supports for this run - group 0 is always
    exactly what get_chart_data() returns. cobra_job/eventlog aren't in here at all (gantt/scatter
    aren't part of this "line chart groups" concept - charts.py calls their dedicated functions
    directly, same as get_chart_data never covered them either)."""
    return _CHART_GROUP_FUNCS[cfg["kind"]](cfg, run_id)


def get_chart_group_list(cfg, run_id=None):
    """[{"key", "label"}, ...] - the cheap part of get_chart_groups(), for a caller (views.py's
    tool_detail) that only needs to know how many chart panels to render and their keys/labels,
    not ship every group's full series data up front.

    cobra_job/eventlog (gantt/scatter, not part of the "line chart groups" concept - see
    get_chart_groups) get a single placeholder entry instead, so a template that renders one
    <canvas> per entry still renders exactly the one canvas those kinds have always had, with an
    empty group key that get_chart_json/render_chart_png's group_key param already treats as
    "use the default" (both kinds ignore group_key entirely, same as before groups existed).

    Swallows ToolDataError the same way get_tool_history() does - a caller building a page around
    an already-successfully-fetched summary shouldn't have this one extra, purely-cosmetic parse
    take the whole page down if it happens to fail; it just renders zero chart panels instead."""
    if cfg["kind"] not in _CHART_GROUP_FUNCS:
        return [{"key": "", "label": "Chart"}]
    try:
        groups = [{"key": g["key"], "label": g["label"]} for g in get_chart_groups(cfg, run_id)]
    except ToolDataError:
        return []

    # "Events" (see get_heater_log_run_events/get_mvd_run_events) is a scatter timeline/plain
    # list, not a line-series group, so it deliberately isn't part of
    # get_chart_groups()/_CHART_GROUP_FUNCS above - charts.py special cases this one key the same
    # way it already special cases cobra_job/eventlog's chart types.
    if cfg["kind"] == "heater_log":
        try:
            _title, points = get_heater_log_run_events(cfg, run_id)
        except ToolDataError:
            points = []
        if points:
            groups.append({"key": "events", "label": "Events"})
    elif cfg["kind"] == "mvd":
        # Pressure (_PT.txt) - listed cheaply (header-only, see _mvd_pressure_group_list) so this
        # never pays for a full parse of what can be a 100,000+ row file just to render the tab
        # strip; the real parse only happens if/when that tab is actually opened
        # (get_mvd_pressure_group). Inserted right after "Temperature" (group 0 is always that -
        # see _mvd_chart_groups) rather than appended at the end, same "pressure belongs right
        # next to temperature" placement as heater_log's own _heater_log_chart_groups.
        groups[1:1] = _mvd_pressure_group_list(cfg, run_id)
        # Events (_EVT.txt) - already fetched locally as part of this run's folder sync either
        # way (see _mvd_run_local_path), and typically small enough that checking for content here
        # isn't worth special-casing further - the parse is cached, so this doesn't duplicate work
        # once the tab is actually opened.
        try:
            _title, points = get_mvd_run_events(cfg, run_id)
        except ToolDataError:
            points = []
        if points:
            groups.append({"key": "events", "label": "Events"})

    return groups


def get_tool_history(cfg, page=1, page_size=DEFAULT_HISTORY_LIMIT, recipe=None, user_windows=None, start_date=None, end_date=None):
    """
    Returns (runs, total_count) for one page of runs, most recent first, as summary dicts
    (no channel series). Only the runs on the requested page are actually parsed - the full
    list of runs is only stat'd (cheap), not read, so this stays fast regardless of how many
    runs a tool has on disk.

    `recipe` (a list of recipe names - a run matches any one of them, exact/case-insensitive),
    `user_windows` ([(start, end), ...] naive-local-time windows - see
    reservations.find_user_run_windows, itself already an OR across every tagged username), and
    `start_date`/`end_date` (plain date objects, inclusive, either or both may be given
    independently) narrow the list before pagination, so `total_count` reflects the filtered set -
    see _filter_run_entries for how each is matched. All are metadata-only filters (no extra
    fetch/parse cost) for heater_log/mvd; every other kind ignores them (no linear per-run list
    with an embedded recipe name/start timestamp to filter by).
    """
    try:
        return _HISTORY_FUNCS[cfg["kind"]](cfg, page, page_size, recipe, user_windows, start_date, end_date)
    except ToolDataError:
        return [], 0
