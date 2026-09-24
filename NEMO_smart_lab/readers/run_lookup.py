"""Cheap, listing-only lookups across a tool's runs (latest, page number, time range, per-recipe counts)."""

from NEMO_smart_lab.readers.common import ToolDataError
from NEMO_smart_lab.readers.heater_log.runs import _list_heater_log_entries
from NEMO_smart_lab.readers.mvd.runs import _list_mvd_run_entries
from NEMO_smart_lab.readers.run_names import _filter_run_entries, _recipe_from_run_id, _run_start_timestamp


def get_latest_run_id(cfg):
    """This tool's own most recent run's id (filename/foldername) - a cheap, listing-only lookup
    (no fetch/parse). Used by the tool detail page to tell whether an explicit ?run=<id> happens
    to be the tool's actual latest run (reached via the overview page's own "View full details"
    link) rather than a genuinely earlier one, so the "Viewing a past run" banner isn't shown for
    it. None if this tool kind has no linear per-run list at all (only heater_log/mvd do -
    cobra_job/eventlog are single, continuously-growing files), or it currently has zero runs (the
    underlying listing raises ToolDataError for "no runs at all", swallowed here into None since a
    tool with nothing to show yet isn't a real error for this particular check)."""
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return None
    except ToolDataError:
        return None
    return entries[0][0] if entries else None


def get_run_page_number(cfg, run_id, page_size, recipe=None, user_windows=None):
    """Which page of get_tool_history(cfg, page_size=page_size, recipe=recipe,
    user_windows=user_windows) this specific run_id falls on (1-indexed) - a cheap, listing-only
    lookup (no fetch/parse), same shape as get_latest_run_id. Used by the "View run history" link
    on a past run's own detail page, so it jumps straight to the page that run is actually on
    instead of always landing on page 1 and leaving the viewer to go hunting for it. None if this
    tool kind has no linear per-run list at all, or run_id isn't found in it (e.g. a stale/bad
    ?run= value, or one filtered out by `recipe`/`user_windows`)."""
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return None
    except ToolDataError:
        return None
    entries = _filter_run_entries(cfg, entries, recipe, user_windows)
    names = [name for name, _mtime in entries]
    try:
        index = names.index(run_id)
    except ValueError:
        return None
    return index // page_size + 1


def get_run_time_range(cfg):
    """(earliest, latest) naive-local-time run start timestamps (see _run_start_timestamp) across
    this tool's whole run history - a cheap, listing-only computation (no fetch/parse of any run's
    own content). Used to bound the *one* remote usage lookup reservations.find_user_run_windows
    makes when a user-filter search finds nothing locally, the same "one bounded query covering a
    known range" reasoning _remote_rows' own docstring already establishes is necessary to keep a
    remote lookup fast (confirmed live: an unbounded one pulled 1000+ rows, 20+ seconds, on a real
    prod tool).

    (None, None) for a kind with no linear per-run list at all, or one with no runs/no parseable
    filename timestamps."""
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return None, None
    except ToolDataError:
        return None, None
    timestamps = [ts for ts in (_run_start_timestamp(cfg, name) for name, _mtime in entries) if ts is not None]
    if not timestamps:
        return None, None
    return min(timestamps), max(timestamps)


def count_runs_for_recipe(cfg, recipe_name):
    """How many of this tool's past runs have this exact recipe name embedded in their own
    filename/foldername (see _recipe_from_run_id) - a cheap, listing-only count (no fetch/parse of
    any run's own content, same reasoning as get_run_time_range/get_base_pressure_history's own
    filename-only matching). Shown on a recipe's own detail page (recipes.get_recipe_detail) so
    "how much has this recipe actually been used" is visible without opening the (potentially
    filtered-down-to-this-recipe) run history separately. None (not 0 - "not tracked", not
    "confirmed zero") for a kind with no linear per-run list at all; 0 for a blank/unmatched
    recipe_name or a kind that simply has none yet - never an error either way."""
    if not recipe_name:
        return 0
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return None
    except ToolDataError:
        return 0
    return len(_filter_run_entries(cfg, entries, recipe=[recipe_name]))


def count_runs_by_recipe_name(cfg):
    """{recipe_name: run_count} across this tool's ENTIRE run history, keyed by each run's own
    embedded recipe name exactly as it appears in its own filename/foldername (not case-folded or
    ".txt"-stripped - two different-looking spellings of what's "really" the same recipe are
    counted separately here; a caller that wants them merged, e.g. to match against an actual
    recipe *file*'s own name, needs to normalize both sides itself the same way
    recipes.find_recipe_by_name already does). Cheap, listing-only (no per-run fetch/parse) - same
    reasoning as count_runs_for_recipe, just aggregated over every recipe name at once instead of
    testing one. Used by recipes.total_cycles_run (weight each recipe's current cycle count by how
    many times it's actually been run) and the recipe list's own "Runs" column
    (views._grouped_recipes). {} for a kind with no linear per-run list at all."""
    try:
        if cfg["kind"] == "heater_log":
            entries = _list_heater_log_entries(cfg)
        elif cfg["kind"] == "mvd":
            entries = _list_mvd_run_entries(cfg)
        else:
            return {}
    except ToolDataError:
        return {}
    counts = {}
    for name, _mtime in entries:
        recipe_name = _recipe_from_run_id(cfg, name).strip()
        if recipe_name:
            counts[recipe_name] = counts.get(recipe_name, 0) + 1
    return counts
