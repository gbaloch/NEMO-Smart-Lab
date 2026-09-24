"""Shared pieces every reader module builds on: the error types, the parsed-file cache, channel-label lookup, and small helpers used by more than one tool kind."""

import glob
import hashlib
import os

from NEMO_smart_lab.remote_cache import cache


FILE_ENCODING = "latin-1"


DEFAULT_HISTORY_LIMIT = 25


# A single tool_detail page load calls both get_tool_summary() and get_chart_group_list() for the
# same run; each of those parses the same underlying file/folder from scratch independently, and
# every chart tab switch/download-as-image fires yet another separate re-parse on top of that -
# measured live: fiji5's own ~1.1s _mvd_run_data_for_dir() parse, paid twice over on one page load
# alone. Unlike remote_cache's/reservations.py's time-based TTLs (those cache "is this probably
# still true"), this caches "what does parsing this exact, byte-for-byte-unchanged file produce" -
# a pure function of the file's own content - so the cache key itself (see _file_fingerprint)
# encodes each underlying file's mtime+size; a stale hit is structurally impossible; safe to keep
# far longer than any TTL tuned for freshness, only bounded here to cap unbounded cache growth
# across many different runs being browsed over time.
PARSED_FILE_CACHE_TTL = 60 * 60 * 24


def _file_fingerprint(path):
    stat = os.stat(path)
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _cached_file_parse(kind, paths, parser):
    """Memoizes `parser()` (a zero-arg callable - a closure over whatever it actually needs to
    parse) keyed by `kind` plus every path in `paths` together with its own mtime+size - see
    PARSED_FILE_CACHE_TTL. `paths` is every file the parse actually reads (e.g. mvd's _SUM.txt
    *and* _DAT.txt), so the cache is invalidated the instant any of them changes, not just the one
    that happens to be biggest.

    Shares remote_cache's own optionally-persistent "smart_lab" cache alias (see that module's
    docstring) rather than importing Django's plain default cache directly - a deployment that
    configures that alias for a persistent backend gets this cache surviving a restart too, for
    the same reason: a large run's file (some real mvd DAT files here run past 200,000 rows) can
    take real, measurable time to parse from scratch (seconds, not milliseconds - see
    _parse_mvd_dat), so losing this to an ordinary process restart is worth avoiding when a
    deployment cares to."""
    try:
        fingerprint = "|".join(f"{p}:{_file_fingerprint(p)}" for p in paths)
    except OSError:
        # Let the real parser raise its own (more specific) ToolDataError for a missing file,
        # rather than this cache-key bookkeeping doing it first.
        return parser()
    cache_key = "smart_lab:parsed:" + hashlib.sha1(f"{kind}:{fingerprint}".encode()).hexdigest()[:24]
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = parser()
    cache.set(cache_key, result, PARSED_FILE_CACHE_TTL)
    return result


class ToolDataError(Exception):
    """Raised when a tool's configured data source, or a specific run within it, can't be found or parsed."""


class EmptyRunFileError(ToolDataError):
    """A run's log file exists but holds no data rows (just a header, or nothing at all) - what a
    run that was started and immediately aborted/never got going leaves behind (confirmed live:
    savannah's 2026_09_17-14-00_.txt is a 188-byte header line and nothing else). Distinct from a
    genuinely unreadable/corrupt file so "find the latest usable run" can skip these."""


def _channel_label(cfg, raw_key):
    """Returns (display_name, role, hidden, on_threshold_c) for a raw channel key, using the
    tool's admin-configured SmartLabToolChannel overrides (cfg["channel_labels"], built by
    SmartLabTool.as_source_config()) if one exists for raw_key, else (raw_key, None, False, None)
    unchanged. hidden=True means this channel is a schema slot that isn't actually wired to
    anything on this particular tool (e.g. always reads a constant 0) - callers should drop it
    entirely rather than display it, however it's named. on_threshold_c, when not None,
    overrides the tool-wide on_threshold_c for just this one channel's "is it on" check (e.g. a
    precursor jacket run at a lower steady-state temperature than the reactor/chuck zones the
    tool-wide threshold is tuned for)."""
    override = (cfg.get("channel_labels") or {}).get(raw_key)
    return override if override else (raw_key, None, False, None)


def _has_any_value(series_dict):
    """series_dict maps name -> (x_values, y_values) - True if any series has a single non-null
    y value anywhere."""
    return any(v is not None for _x_values, y_values in series_dict.values() for v in y_values)


def _find_one(dirpath, suffix):
    matches = glob.glob(os.path.join(dirpath, f"*{suffix}"))
    if not matches:
        raise ToolDataError(f"No *{suffix} file found in: {dirpath}")
    return matches[0]


def _last_non_null(values):
    for v in reversed(values):
        if v is not None:
            return v
    return None


#
# CurrentEvents.csv is a single, continuously-growing, newest-first log of every module
# state change, command, and fault. A run's run_id is its "DO PROCESS" row's own
# "Date/Time|Module|Info" (guaranteed unique in practice - see mostRecentRun in the
# original Smart-Lab EventLog.py this is adapted from), since there's no simpler natural key.
TERMINATING_EVENTS = {"State Changed To Ready", "State Changed To Idle", "State Changed To Aborted"}


FAULT_KEYWORDS = ("Fault", "Alarm")


def _average_tail(time_s, values, window_s):
    """Mean of whichever values fall in the last `window_s` seconds of a run's own time_s array -
    None (not 0) if that window has no real (non-null) readings at all, so a run with no usable
    tail data is skipped entirely rather than plotted as a misleading zero."""
    if not time_s:
        return None
    cutoff = time_s[-1] - window_s
    tail = [v for t, v in zip(time_s, values) if t >= cutoff and v is not None]
    return sum(tail) / len(tail) if tail else None
