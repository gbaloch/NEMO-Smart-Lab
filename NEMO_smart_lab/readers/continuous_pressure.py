"""A tool's continuous, always-on background pressure log."""

import csv
import math
import re

from datetime import datetime, timedelta

import numpy as np

from NEMO_smart_lab import remote_cache, remote_sync
from NEMO_smart_lab.readers.common import FILE_ENCODING, _cached_file_parse
from NEMO_smart_lab.readers.mvd.parsing import _UNIT_SUFFIX_RE


# fiji5's own always-on background pressure log (datalog/data/Pressure - confirmed live, a real,
# separate data source from anything per-run: the same 4 gauges as a run's own PT.txt, but logged
# continuously at ~10Hz regardless of whether a recipe is running at all) rotates into a new file
# roughly every 1-2 days (or sooner, capped near 100MB) - "<Label> - YYYY-MM-DD HH.MM.SS.txt",
# named by when THAT file started, confirmed live for fiji5's own files.
_CONTINUOUS_PRESSURE_FILENAME_RE = re.compile(r"^.+ - (\d{4})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})\.txt$")


# The file's own header claims "Time (sec)" for its first column, but every real row is actually a
# full local timestamp string, not an elapsed-seconds float - confirmed live: "2:03:38.359 PM
# 8/20/2026". A real, if confusingly-labeled, quirk of this export - not something to "fix" by
# assuming the header is right.
_CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT = "%I:%M:%S.%f %p %m/%d/%Y"


# These files are the largest thing this whole module ever reads (single files up to ~100MB,
# tens of thousands of rows) - bounding how many get fetched/parsed in one request (rather than
# every file that could possibly overlap the requested window) keeps a first, cold-cache view of
# this chart from turning into a multi-minute/multi-GB fetch; a repeat view is cheap regardless
# (see _parse_and_bucket_continuous_pressure_file's own per-file caching).
CONTINUOUS_PRESSURE_WINDOW_DAYS = 14


_CONTINUOUS_PRESSURE_BUCKET_MINUTES = 15


# A larger budget than a single "last 14 days" view needs, so "6 months"/"1 year"/"all time" can
# still cover their *entire* requested span (evenly sampled, not just the most recent slice of it -
# see get_continuous_pressure_trend's own sampling logic) rather than silently showing only a
# recent sliver of what was actually asked for. Cold-cache cost scales with this directly (~8s per
# never-before-fetched file, measured live) - 20 keeps a first "all time" view to a few minutes,
# not tens of minutes; every file is cached afterward regardless of range (see
# _parse_and_bucket_continuous_pressure_file's own docstring), so repeat views of any range are
# fast even when they share files with a range already viewed.
_MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST = 20


# Matches the toggle in tool_detail.html - missing/unrecognized falls back to "24h" (the default
# selected option), NOT "show everything" the way charts.BASE_PRESSURE_RANGE_DAYS' own missing-key
# convention does (that chart's own per-run points are cheap regardless of range; these files are
# not, so "show everything" should never be the silent default here).
CONTINUOUS_PRESSURE_RANGE_DAYS = {"24h": 1, "7d": 7, "14d": 14, "1m": 30, "6m": 182, "1y": 365}


def _continuous_pressure_filename_timestamp(name):
    m = _CONTINUOUS_PRESSURE_FILENAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _fast_continuous_pressure_timestamp(raw):
    """Hand-rolled parser for this file's own real timestamp column format ("2:03:38.359 PM
    8/20/2026" - see _CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT's own docstring for the header's
    misleading "(sec)" label) - measured live ~5x faster than
    datetime.strptime(_CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT) for this exact shape, which
    matters here: with the value-column float parsing now offloaded to numpy (see
    _parse_and_bucket_continuous_pressure_file), per-row timestamp parsing is the single largest
    remaining cost against files that run past 100,000 rows. Returns None (never raises) for
    anything that doesn't match this exact expected shape - the caller falls back to
    datetime.strptime for that rare case, so this never trades away correctness for speed."""
    try:
        time_part, ampm, date_part = raw.split(" ")
        hour_str, minute_str, second_str = time_part.split(":")
        second_str, _dot, micro_str = second_str.partition(".")
        hour = int(hour_str)
        if ampm == "PM":
            if hour != 12:
                hour += 12
        elif ampm == "AM":
            if hour == 12:
                hour = 0
        else:
            return None
        month_str, day_str, year_str = date_part.split("/")
        return datetime(
            int(year_str), int(month_str), int(day_str),
            hour, int(minute_str), int(second_str),
            int(micro_str.ljust(6, "0")[:6]) if micro_str else 0,
        )
    except (ValueError, IndexError):
        return None


def _continuous_pressure_row_timestamp(raw):
    """_fast_continuous_pressure_timestamp, falling back to the guaranteed-correct
    datetime.strptime for anything the fast parser doesn't recognize - never disagrees with it,
    just slower for that one row."""
    parsed = _fast_continuous_pressure_timestamp(raw)
    if parsed is not None:
        return parsed
    try:
        return datetime.strptime(raw, _CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _parse_and_bucket_continuous_pressure_file(path, bucket_seconds):
    """Parses one continuous pressure datalog file directly into per-gauge, per-time-bucket (sum,
    count) accumulators - deliberately never holds the full row-level data at once (these files
    run past 100,000 rows), and just as importantly, the CACHED result (see _cached_file_parse,
    used by this function's only caller) is this same small bucketed dict, not the raw rows - a
    repeat view of the continuous pressure chart never needs to copy a huge structure back out of
    cache, only a few dozen small buckets per gauge (the same lesson get_mvd_maintenance_trends'
    own docstring already documents for fiji5's per-run DAT files, just as true here).

    Fast path: numpy's own C-level numeric-text reader (np.loadtxt) parses every VALUE cell (every
    column except the timestamp one) in a single pass, instead of one Python-level float()-per-cell
    loop per row - the same technique _parse_mvd_dat already uses for mvd/fiji5's own DAT files (see
    its own docstring), applied here for the same reason: measured live on a realistic ~200,000-row,
    4-gauge file, ~2.8x faster overall than the original per-cell loop (the remaining cost is mostly
    each row's own timestamp string, which still needs per-row parsing - see
    _fast_continuous_pressure_timestamp for that half of the speedup). Only used when the whole
    file is a clean, uniform numeric grid - np.loadtxt raises on any row of the wrong width or any
    cell it can't parse as a number, at which point this falls back to the original per-cell loop,
    which can never disagree with the fast path since it's the exact same file parsed the exact
    same way, just one cell at a time. A NaN/Inf cell (numpy parses the literal text "nan"/"inf"
    into a real float, same as Python's own float() does) is excluded from both paths identically.

    Returns {gauge_name: {bucket_start_epoch_seconds: [sum, count]}}. A row whose own timestamp or
    any given cell fails to parse is skipped, not fatal to the rest of the file - confirmed live
    that a gauge glitch can log a literal "NaN" cell, which Python's own float() happily accepts
    (unlike a ValueError for genuine garbage) but which would otherwise propagate into the bucket
    average and break the JSON response entirely (JavaScript's JSON.parse rejects a literal NaN
    token - not valid per the JSON spec, even though Python's json.dumps writes one by default)."""
    with open(path, encoding=FILE_ENCODING, newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return {}
        gauge_names = []
        for col in header[1:]:
            m = _UNIT_SUFFIX_RE.match(col)
            gauge_names.append((m.group(1) if m else col).strip('"'))
        n_gauges = len(gauge_names)
        raw_rows = list(reader)

    if not raw_rows:
        return {}

    timestamps = np.full(len(raw_rows), np.nan)
    for i, row in enumerate(raw_rows):
        if not row:
            continue
        ts = _continuous_pressure_row_timestamp(row[0].strip())
        if ts is not None:
            timestamps[i] = ts.timestamp()

    value_grid = None
    if n_gauges:
        try:
            # Deliberately re-reads `path` from scratch here (np.loadtxt wants the raw file) rather
            # than reusing `raw_rows` (already parsed by csv.reader above) - the extra read is a
            # rounding error next to the per-cell parse time it avoids, same tradeoff _parse_mvd_dat
            # already makes.
            value_grid = np.loadtxt(
                path, delimiter=",", skiprows=1, usecols=tuple(range(1, n_gauges + 1)), dtype=np.float64, ndmin=2
            )
        except (ValueError, IndexError):
            value_grid = None

    if value_grid is not None and value_grid.shape[0] == len(raw_rows):
        buckets = {}
        valid = ~np.isnan(timestamps)
        if not valid.any():
            return buckets
        bucket_starts = (np.floor(timestamps[valid] / bucket_seconds) * bucket_seconds).astype(np.int64)
        values = value_grid[valid]
        unique_buckets, inverse = np.unique(bucket_starts, return_inverse=True)
        for gauge_idx, gauge in enumerate(gauge_names):
            col = values[:, gauge_idx]
            finite = np.isfinite(col)
            if not finite.any():
                continue
            sums = np.zeros(len(unique_buckets))
            counts = np.zeros(len(unique_buckets))
            np.add.at(sums, inverse[finite], col[finite])
            np.add.at(counts, inverse[finite], 1)
            nonzero = counts > 0
            if nonzero.any():
                buckets[gauge] = {
                    int(bucket_start): [float(total), int(count)]
                    for bucket_start, total, count in zip(unique_buckets[nonzero], sums[nonzero], counts[nonzero])
                }
        return buckets

    # Fallback: the original per-cell loop - a malformed/ragged grid (or a header with no gauge
    # columns at all), not fatal, just slower for this one file.
    buckets = {}
    for row in raw_rows:
        if not row:
            continue
        timestamp = _continuous_pressure_row_timestamp(row[0].strip())
        if timestamp is None:
            continue
        bucket_start = int(timestamp.timestamp() // bucket_seconds) * bucket_seconds
        for gauge, cell in zip(gauge_names, row[1:]):
            try:
                value = float(cell)
            except ValueError:
                continue
            if math.isnan(value) or math.isinf(value):
                continue
            accumulator = buckets.setdefault(gauge, {}).setdefault(bucket_start, [0.0, 0])
            accumulator[0] += value
            accumulator[1] += 1
    return buckets


def get_continuous_pressure_trend(cfg, range_key=None):
    """Chamber pressure over the selected range (see CONTINUOUS_PRESSURE_RANGE_DAYS - "24h" (the
    default, missing-key case)/"7d"/"14d"/"1m"/"6m"/"1y", or "all"/anything else for this tool's
    entire history) from this tool's continuous,
    always-on background pressure log (opt-in via SmartLabTool.continuous_pressure_subdir - e.g.
    fiji5's "datalog/data/Pressure") - a genuinely different, much more complete signal than
    get_base_pressure_history's own "one point per standby run": every gauge, logged continuously
    at ~10Hz regardless of whether a recipe is even running, not just sampled whenever someone
    happens to run the standby recipe.

    Only ever fetches/parses up to `_MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST` files (bounded -
    see that constant's own docstring for why) - when the requested range would otherwise need
    more than that, they're EVENLY SAMPLED across the whole requested span rather than truncated
    to the most recent slice of it, so "all time" actually shows the tool's entire history (at a
    coarser resolution) instead of silently only ever showing its last couple of weeks. Downsamples
    to `_CONTINUOUS_PRESSURE_BUCKET_MINUTES`-minute bucket averages per gauge before ever
    returning - these files are far too large to hand raw rows back to the browser.

    Returns {"gauges": [name, ...], "timestamps": [epoch_seconds, ...], "series": {name:
    [avg_or_None, ...]}} (aligned - "series" values are one per "timestamps" entry, None where
    that gauge has no reading in that bucket) - shaped for charts.py to turn straight into the
    same uPlot-ready JSON get_base_pressure_chart_json already produces.

    None (not an error) if this tool has no continuous_pressure_subdir configured, no remote_tool,
    or has no continuous-pressure files at all. A tool that IS configured and has files, just none
    of them falling within the selected range (e.g. "24h" right after a gap in syncing), instead
    returns the same shape with empty lists/dict - a real, distinguishable "nothing in this window"
    outcome, not "not set up" - so charts.py can tell the two apart and show an appropriate message
    for each."""
    subdir = cfg.get("continuous_pressure_subdir")
    tool = cfg.get("remote_tool")
    if not subdir or tool is None:
        return None
    try:
        entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/{subdir}")
    except remote_sync.RemoteSyncError:
        return None

    files = []
    for name, _mtime, _size, is_dir in entries:
        if is_dir:
            continue
        timestamp = _continuous_pressure_filename_timestamp(name)
        if timestamp is not None:
            files.append((name, timestamp))
    if not files:
        return None
    files.sort(key=lambda item: item[1])  # oldest first

    # A missing range_key (None) means "default to 24h"; an explicit but unrecognized one
    # (including "all") means "this tool's entire history" - these are deliberately different
    # outcomes, not the same fallback, so a bare/omitted ?range= (e.g. the initial page load,
    # before the toggle is touched) doesn't silently show "all time" instead of the intended
    # default.
    effective_key = range_key if range_key is not None else "24h"
    window_days = CONTINUOUS_PRESSURE_RANGE_DAYS.get(effective_key)
    cutoff = (datetime.now() - timedelta(days=window_days)) if window_days is not None else files[0][1]
    # A file whose own start timestamp already falls in the window is definitely relevant; the one
    # file immediately before it is too, since its data can extend past its own start time, into
    # the window (it stops only when the NEXT file's start time begins) - EXCEPT for the very last
    # (most recent) file, which has no "next file" yet because it's the one still actively being
    # logged into right now: its own start timestamp can be well before the cutoff while its rows
    # keep extending all the way up to the present moment, so it's always relevant regardless of
    # when it started. Confirmed live: this was a real, previously-latent bug - fiji5's newest file
    # started at 00:29 but (as of a stale local cache) its own last logged row was 20:15 that same
    # day; every range tested before "24h" was added had a cutoff early enough that this file's own
    # start timestamp already cleared it on its own, so the gap never showed up until a range this
    # narrow could fall entirely after that start timestamp yet still land within the file's data.
    relevant = []
    for i, (name, timestamp) in enumerate(files):
        is_most_recent_file = i == len(files) - 1
        if is_most_recent_file or timestamp >= cutoff or files[i + 1][1] >= cutoff:
            relevant.append((name, timestamp))
    if not relevant:
        return {"gauges": [], "timestamps": [], "series": {}}
    if len(relevant) > _MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST:
        # Evenly spaced indices across the whole relevant span (always including the very first
        # and last) rather than "the most recent N" - the whole point of a wide range like "all
        # time" is to see the long-term trend across its entire span, not just its tail.
        stride = (len(relevant) - 1) / (_MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST - 1)
        indices = sorted({round(i * stride) for i in range(_MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST)})
        relevant = [relevant[i] for i in indices]

    # Prefetch every relevant file CONCURRENTLY before parsing any of them - these are the largest
    # files this whole module ever fetches (up to ~100MB each, ~8s per never-before-cached file
    # measured live), and this loop can need up to _MAX_CONTINUOUS_PRESSURE_FILES_PER_REQUEST (20)
    # of them at once. Fetching them one at a time (the original approach) serialized that into up
    # to 20 sequential ~8s round trips - a multi-minute wait on any range wide enough to need more
    # than a couple of never-before-seen files - confirmed live as the dominant cost behind this
    # chart's own "filtering by date is slow" complaints. ensure_cached_many's own 8-way concurrency
    # (and its already-existing "skip anything already local" fast path) turns that into a handful
    # of parallel batches instead, and costs nothing extra on a warm cache either way.
    remote_cache.ensure_cached_many(tool, [f"{subdir}/{name}" for name, _timestamp in relevant])

    bucket_seconds = _CONTINUOUS_PRESSURE_BUCKET_MINUTES * 60
    combined = {}
    for name, _timestamp in relevant:
        try:
            path = remote_cache.ensure_cached(tool, f"{subdir}/{name}")
        except remote_sync.RemoteSyncError:
            continue
        file_buckets = _cached_file_parse(
            "continuous_pressure", [path], lambda p=path: _parse_and_bucket_continuous_pressure_file(p, bucket_seconds)
        )
        for gauge, gauge_buckets in file_buckets.items():
            merged = combined.setdefault(gauge, {})
            for bucket_start, (total, count) in gauge_buckets.items():
                accumulator = merged.setdefault(bucket_start, [0.0, 0])
                accumulator[0] += total
                accumulator[1] += count

    if not combined:
        return {"gauges": [], "timestamps": [], "series": {}}
    cutoff_epoch = cutoff.timestamp()
    all_bucket_starts = sorted({bs for gauge_buckets in combined.values() for bs in gauge_buckets if bs >= cutoff_epoch})
    if not all_bucket_starts:
        return {"gauges": [], "timestamps": [], "series": {}}
    gauges = sorted(combined)
    series = {
        gauge: [
            (combined[gauge][bs][0] / combined[gauge][bs][1]) if bs in combined[gauge] else None
            for bs in all_bucket_starts
        ]
        for gauge in gauges
    }
    return {"gauges": gauges, "timestamps": all_bucket_starts, "series": series}
