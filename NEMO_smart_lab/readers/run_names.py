"""Run identity: parsing a run's own start time/recipe out of its filename or folder name, and the metadata-only recipe/user/date filters applied to run lists (no file is opened)."""

import re

from datetime import datetime


# A run's filename ("YYYY_MM_DD-HH-MM-SS_<recipe>.txt") embeds its own start time, written once by
# the tool PC when the run began - confirmed live to be the only reliable "when did this run
# actually happen" signal. A file's mtime is NOT that: it reflects whenever the file was last
# written *to whatever filesystem is being read* - for a freshly-fetched local cache that's when it
# was pulled from Oak, and even Oak's own copy can get a fresh mtime if a batch of old data is ever
# re-uploaded/re-synced onto it out of chronological order (confirmed: an old run recently
# re-copied to Oak sorted as "the newest run" by mtime alone, despite having happened long before
# runs whose files hadn't been touched since). Every ordering/"most recent" decision and every
# displayed run-end time below is anchored to this filename timestamp (plus the run's own elapsed
# duration for the *end* time) instead, falling back to mtime only for the rare file whose name
# doesn't match this pattern at all. The trailing "-SS" seconds group is optional - confirmed live
# that savannah's own filenames omit it entirely ("2026_08_25-09-18_clear0.txt", minutes only),
# unlike fiji1/2/3's always-present seconds ("2026_09_10-17-59-31_..."); a real, previously
# unnoticed bug - savannah's filename timestamp never matched this pattern at all before, silently
# falling back to (unreliable, per this whole module's own docstring) mtime for every one of its
# runs.
_HEATER_LOG_FILENAME_RE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})-(\d{2})-(\d{2})(?:-(\d{2}))?_")


def _heater_log_filename_timestamp(name):
    m = _HEATER_LOG_FILENAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) if g is not None else 0 for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


# A run folder's own name ("YYYYMMDD_HHMMSS_<recipe>", confirmed live for both mvd and fiji5)
# embeds its start time, same reasoning as _HEATER_LOG_FILENAME_RE above: any mtime (the folder's,
# or the DAT file's inside it) reflects whenever it was last *written to whatever filesystem is
# being read*, not necessarily when the run happened - confirmed live elsewhere that a re-upload
# out of chronological order gives a stale run a fresh mtime, which would sort it as "newest".
_MVD_FOLDER_NAME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_")


def _mvd_folder_timestamp(name):
    m = _MVD_FOLDER_NAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _recipe_from_run_id(cfg, run_id):
    """The recipe name embedded in a run's own filename/foldername, stripped of its timestamp
    prefix (heater_log: "YYYY_MM_DD-HH-MM-SS_<recipe>.txt") or (mvd: "YYYYMMDD_HHMMSS_<recipe>") -
    cheap (no fetch/parse needed) name-only extraction, used by get_base_pressure_history to find
    matching runs before ever touching their actual data."""
    if cfg["kind"] == "heater_log":
        recipe = _HEATER_LOG_FILENAME_RE.sub("", run_id)
        while recipe.lower().endswith(".txt"):
            recipe = recipe[: -len(".txt")]
        return recipe
    if cfg["kind"] == "mvd":
        return _MVD_FOLDER_NAME_RE.sub("", run_id)
    return run_id


def _run_start_timestamp(cfg, run_id):
    """The run's own start time embedded in its filename/foldername (see
    _heater_log_filename_timestamp/_mvd_folder_timestamp) - cheap (no fetch/parse needed), a naive
    datetime in this server's local system timezone (same convention every other readers.py
    timestamp already uses - see reservations.run_time_window's docstring). None for a kind with
    no such embedded timestamp, or a name that doesn't match the expected pattern."""
    if cfg["kind"] == "heater_log":
        return _heater_log_filename_timestamp(run_id)
    if cfg["kind"] == "mvd":
        return _mvd_folder_timestamp(run_id)
    return None


def _filter_run_entries(cfg, entries, recipe=None, user_windows=None, start_date=None, end_date=None):
    """Narrows a history kind's own [(name, mtime), ...] entry list down to what
    tool_history/get_recipe_run_history actually asked for - applied *before* pagination slicing,
    so page counts/totals reflect the filtered set, not the tool's whole history. Every filter here
    is metadata-only (filename/foldername, never the run's own file content), so this stays cheap
    regardless of how many runs a tool has on disk - the same reasoning get_base_pressure_history
    already relies on for matching by recipe name.

    `recipe` - a list of recipe names (the run history's recipe filter is a multi-select "tag"
    input - see tool_history.html) - a run matches if its own embedded recipe name exactly matches
    (case-insensitive) ANY one of them (OR, not AND - a run only ever has one recipe, so requiring
    every tagged recipe to match would always return nothing once more than one tag is added).
    `user_windows` - [(start, end), ...] naive-local-time windows (see
    reservations.find_user_run_windows, itself already an OR across every tagged username) - a run
    matches if its own embedded start timestamp falls within any one of them. This is a
    start-time-only test (a run's real duration isn't known without parsing its content - see
    _run_start_timestamp's docstring), so it's a reasonable filter, not a byte-for-byte-exact
    reproduction of annotate_run_usage's own full interval-overlap test.
    `start_date`/`end_date` - plain date objects (inclusive on both ends, either or both may be
    given independently) - a run matches if its own embedded start date falls within them; a run
    with no parseable start timestamp at all never matches once either bound is given, the same
    "no signal, no match" reasoning user_windows already uses."""
    if recipe:
        targets = {r.strip().lower() for r in recipe if r and r.strip()}
        if targets:
            entries = [(name, mtime) for name, mtime in entries if _recipe_from_run_id(cfg, name).strip().lower() in targets]
    if user_windows is not None:
        matched = []
        for name, mtime in entries:
            ts = _run_start_timestamp(cfg, name)
            if ts is not None and any(w[0] <= ts <= w[1] for w in user_windows):
                matched.append((name, mtime))
        entries = matched
    if start_date is not None or end_date is not None:
        matched = []
        for name, mtime in entries:
            ts = _run_start_timestamp(cfg, name)
            if ts is None:
                continue
            run_date = ts.date()
            if start_date is not None and run_date < start_date:
                continue
            if end_date is not None and run_date > end_date:
                continue
            matched.append((name, mtime))
        entries = matched
    return entries
