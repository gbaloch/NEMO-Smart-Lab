"""Chamber base pressure over time, built from each standby run's own pressure log."""

import os

from datetime import datetime, timedelta

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import ToolDataError, _average_tail, _cached_file_parse
from NEMO_smart_lab.readers.heater_log.parsing import _parse_simple_run_log
from NEMO_smart_lab.readers.heater_log.runs import _list_heater_log_entries
from NEMO_smart_lab.readers.mvd.parsing import _parse_mvd_pt
from NEMO_smart_lab.readers.mvd.pressure import _mvd_default_visible_pressure_channel, _mvd_pt_path
from NEMO_smart_lab.readers.mvd.runs import _list_mvd_run_entries, _mvd_run_local_path
from NEMO_smart_lab.readers.run_names import (
    _heater_log_filename_timestamp,
    _mvd_folder_timestamp,
    _recipe_from_run_id,
)


# get_base_pressure_history has no cap on how many *runs* it covers (see its own docstring) - but
# fetching a tool's entire multi-year backlog of never-before-seen files over one SSH connection,
# synchronously, inside a single HTTP request, is a different problem: confirmed live against
# fiji2/fiji3/mvd, each with well over a thousand matching historical runs and a real ~1.5s round
# trip per file not yet fetched, that this turns "open the chart for the first time" into a
# multi-minute hang. So the fetch itself - not the result - is what's bounded per request: only
# this many genuinely-missing files are fetched inline; the rest are handed to a background warm
# (remote_cache.warm_in_background) that keeps making progress across the next several page loads
# without ever blocking one. Already-local files (the overwhelming majority after the first
# request or two) are never subject to this cap - see remote_cache.ensure_cached_many's own local-
# existence fast path.
_MAX_SYNCHRONOUS_FETCHES_PER_REQUEST = 30


def _remote_dir_names(tool, subdir):
    """Set of file names in `<tool's remote root>/<subdir>` from the cached remote listing, or None
    (meaning "unknown - don't filter") if the listing itself can't be fetched."""
    try:
        entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/{subdir}")
    except remote_sync.RemoteSyncError:
        return None
    return {name for name, _mtime, _size, is_dir in entries if not is_dir}


def _bounded_prewarm(cfg, tool, remote_relpaths, key, is_dir=False):
    missing = [p for p in remote_relpaths if not os.path.exists(os.path.join(tool.local_root, p))]
    remote_cache.ensure_cached_many(tool, missing[:_MAX_SYNCHRONOUS_FETCHES_PER_REQUEST], is_dir=is_dir)
    rest = missing[_MAX_SYNCHRONOUS_FETCHES_PER_REQUEST :]
    if rest:
        remote_cache.warm_in_background(tool, f"{key}:{cfg['kind']}", rest, is_dir=is_dir)


def get_base_pressure_history(cfg, window_s=10.0):
    """Chamber base pressure over time: for every run of any of this tool's configured, *exact*
    standby recipe(s) (SmartLabTool.base_pressure_recipe_names - deliberately a set of specific
    recipes, not a keyword, since a genuinely unrelated recipe that merely contains "standby" in
    its name could have a very different baseline pressure - see that field's docstring; several
    *real* standby variants can legitimately share this list, confirmed live that more than one
    can end in the same long settle-then-measure wait step this feature depends on - see
    recipes.suggest_base_pressure_recipes for auto-discovering which ones), averages the last
    `window_s` seconds of its pressure data (the tail of that wait step, once it's had time to
    actually settle) and returns that one number per run - a simple, real proxy for "is this
    chamber's base vacuum drifting over time" (a slow leak, a dirtying O-ring, etc. shows up as a
    rising trend here long before it's obvious from any single run).

    "recipe" (each run's own embedded recipe name, see _recipe_from_run_id - free, already computed
    to match against `targets` above) is included per point so a tool with more than one configured
    standby recipe can show which one actually produced a given point, rather than lumping them all
    together as an unlabeled single average.

    Returns [{"timestamp": datetime, "value": float, "unit": str, "run_id": str, "recipe": str}, ...] oldest
    first (ready to plot left-to-right), covering every matching run in this tool's whole history
    (no cap - each file's own parse is cached by content fingerprint, so repeat page loads are
    cheap regardless of how many runs match; only the very first computation for a tool with a
    long history pays the full one-time cost of visiting each of them). Returns [] (not an error)
    if this tool has no base_pressure_recipe_names configured, or no run at all matches - this is
    an opt-in, deliberately-configured feature, not something every tool is expected to have wired
    up.

    Matches runs by their filename/foldername's own embedded recipe name (cheap - no fetch/parse
    at all for the runs that don't match) rather than each run's parsed-from-content recipe field,
    so this never pays to open a run's data just to find out it isn't a relevant one."""
    raw_targets = cfg.get("base_pressure_recipe_names")
    if not raw_targets or cfg["kind"] not in ("heater_log", "mvd"):
        return []
    targets = {t.strip().lower() for t in raw_targets.split(",") if t.strip()}
    if not targets:
        return []

    if cfg["kind"] == "heater_log":
        all_entries = _list_heater_log_entries(cfg)
        matching = [name for name, _mtime in all_entries if _recipe_from_run_id(cfg, name).strip().lower() in targets]
        tool = cfg.get("remote_tool")
        if tool is not None:
            # Not every Heater Data run has a matching Pressure Data file (confirmed live on fiji1/
            # fiji3: several standby runs simply have none on Oak) - checked against the (cached)
            # directory listing first so those are skipped outright instead of each costing a
            # failed rsync round trip plus a warning on every cold page load.
            available = _remote_dir_names(tool, "Logfile/Pressure Data")
            if available is not None:
                matching = [name for name in matching if name in available]
            _bounded_prewarm(cfg, tool, [f"Logfile/Pressure Data/{name}" for name in matching], "base_pressure")
        results = []
        for name in matching:
            try:
                path = (
                    remote_cache.ensure_cached(tool, f"Logfile/Pressure Data/{name}")
                    if tool is not None
                    else os.path.join(cfg["root"], "Logfile", "Pressure Data", name)
                )
                time_s, value_series = _parse_simple_run_log(path, value_column_count=1)
            except (ToolDataError, remote_sync.RemoteSyncError):
                continue
            if not time_s:
                continue
            pressure_name = next(iter(value_series))
            avg = _average_tail(time_s, value_series[pressure_name], window_s)
            if avg is None:
                continue
            run_end = (_heater_log_filename_timestamp(name) or datetime.fromtimestamp(0)) + timedelta(seconds=time_s[-1])
            results.append(
                {"timestamp": run_end, "value": avg, "unit": "Torr", "run_id": name, "recipe": _recipe_from_run_id(cfg, name)}
            )
        results.sort(key=lambda r: r["timestamp"])
        return results

    # mvd/fiji5
    all_entries = _list_mvd_run_entries(cfg)
    matching = [name for name, _mtime in all_entries if _recipe_from_run_id(cfg, name).strip().lower() in targets]
    tool = cfg.get("remote_tool")
    if tool is not None:
        _bounded_prewarm(cfg, tool, [f"log/data/{name}" for name in matching], "base_pressure", is_dir=True)
    results = []
    for name in matching:
        try:
            run_dir = _mvd_run_local_path(cfg, name)
            pt_path = _mvd_pt_path(run_dir)
            if pt_path is None:
                continue
            time_s, series = _cached_file_parse("mvd_pt", [pt_path], lambda p=pt_path: _parse_mvd_pt(p))
        except (ToolDataError, remote_sync.RemoteSyncError):
            continue
        if not time_s or not series:
            continue
        channel_names = {n: u for n, (u, _v) in series.items()}
        primary = _mvd_default_visible_pressure_channel(channel_names) or next(iter(series), None)
        if primary is None:
            continue
        unit, values = series[primary]
        avg = _average_tail(time_s, values, window_s)
        if avg is None:
            continue
        run_end = (_mvd_folder_timestamp(name) or datetime.fromtimestamp(0)) + timedelta(seconds=time_s[-1])
        results.append(
            {"timestamp": run_end, "value": avg, "unit": unit or "", "run_id": name, "recipe": _recipe_from_run_id(cfg, name)}
        )
    results.sort(key=lambda r: r["timestamp"])
    return results
