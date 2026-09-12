"""
Links a tool run's time window to whoever actually reserved/used the tool at that time.

Local-first, read-only-remote-as-fallback: NEMO_smart_lab.readers already tells us when a run
finished (a summary dict's "last_update") and how long it took ("run_duration_s"); this module
takes that window and asks first *this* NEMO instance's own Reservation/UsageEvent tables (the
real source of truth - plain ORM queries, no HTTP involved) and only if nothing local overlaps,
optionally a *different* NEMO instance's REST API - strictly read-only, GET requests only. See
NEMO_smart_lab.models.NemoApiSource's docstring. There is no POST/PUT/PATCH/DELETE call anywhere
in this file, to any host - it is structurally incapable of writing to whatever it points at.
"""

import hashlib
import logging
from datetime import timedelta

import requests
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from NEMO.models import Reservation, Tool, UsageEvent

logger = logging.getLogger(__name__)

# Reservations/usage events rarely line up to the second with a run's own log timestamps (clock
# drift between the tool PC and the reservation system, rounding in a run's own duration figure,
# etc.) - widen a run's [start, end] window by this much on each side before matching.
OVERLAP_PAD = timedelta(minutes=5)

# A remote lookup is a live HTTP round trip to a different NEMO instance - cache results the same
# aggressive way NEMO_smart_lab.remote_cache caches tool data, so browsing several runs/history
# pages doesn't repeat the same round trip every time. Both a hit and a genuine "nothing found"
# are cached (there is no cheaper way to tell "nothing overlaps" apart from "haven't checked yet"
# without caching the negative result too).
REMOTE_USAGE_TTL = 60 * 60 * 8


def _cache_key(api_source_id, real_id, start, end):
    digest = hashlib.sha1(f"{real_id}:{start.isoformat()}:{end.isoformat()}".encode()).hexdigest()[:24]
    return f"smart_lab:remote_usage:{api_source_id}:{digest}"


def run_time_window(summary):
    """Best-effort [start, end] for a run, from the two fields every readers.py summary dict
    already carries: "last_update" (the run's end) and "run_duration_s" (its length). Returns
    (None, None) if last_update itself is missing (e.g. the summary is an error dict).

    readers.py builds "last_update" with datetime.fromtimestamp() (a naive datetime, in this
    server's local system timezone) since it has no timezone info of its own to work with; NEMO
    runs with USE_TZ=True, so it has to be made timezone-aware before it can be compared against
    Reservation/UsageEvent's aware start/end fields without Django silently warning and guessing."""
    return _run_window(summary.get("last_update"), summary.get("run_duration_s"))


def _run_window(end, duration_s):
    if end is None:
        return None, None
    if timezone.is_naive(end):
        end = timezone.make_aware(end)
    start = end - timedelta(seconds=duration_s or 0)
    return start - OVERLAP_PAD, end + OVERLAP_PAD


def _user_display(user):
    return user.get_name().strip() or user.username


def get_local_usage(tool_name, start, end):
    """Queries this NEMO instance's own UsageEvent *and* Reservation tables for `tool_name`
    overlapping [start, end]. Returns a combined list of {"user", "username", "start", "end",
    "source"} dicts (usage_events first, then reservations) - these are two genuinely different
    signals worth showing separately rather than one suppressing the other: a UsageEvent is
    actual logged tool usage (someone was logged in), a Reservation is calendar intent (a booked
    slot that may cover a wider window than what was actually used, or may not have been used at
    all) - a run can reasonably have one, the other, both, or neither."""
    try:
        tool = Tool.objects.get(name=tool_name)
    except Tool.DoesNotExist:
        return []

    usage_events = UsageEvent.objects.filter(tool=tool, start__lt=end).filter(
        Q(end__isnull=True) | Q(end__gt=start)
    )
    reservations = Reservation.objects.filter(tool=tool, cancelled=False, start__lt=end, end__gt=start)
    return [
        {"user": _user_display(e.user), "username": e.user.username, "start": e.start, "end": e.end, "source": "usage_event"}
        for e in usage_events.select_related("user")
    ] + [
        {"user": _user_display(r.user), "username": r.user.username, "start": r.start, "end": r.end, "source": "reservation"}
        for r in reservations.select_related("user")
    ]


def _remote_rows(api_source, path, real_id, start, end):
    """One read-only GET, tolerant of both a bare list (confirmed against the real prod API - it
    returns a plain JSON array, no pagination envelope, for these endpoints) and a
    {"results": [...]} paginated shape some NEMO deployments/versions may use instead.

    Filters both `start__lt` (end of our window) and `start__gte` (start of our window) server
    side - confirmed necessary, not just an optimization: without a lower bound this pulled every
    reservation/usage event ever recorded for the tool (1000+ rows, 20+ seconds on a real prod
    tool with years of history) instead of just the handful actually overlapping the window.
    `end__gt`/cancelled are still only checked client-side below, since getting an OR (end is null
    OR end > start) or a boolean filter's exact param spelling right for every possible NEMO
    deployment isn't worth relying on for what's only ever reference data.

    `expand=user` (NEMO's REST API's django-rest-framework-flex-fields support, confirmed against
    the real prod API) replaces the bare `"user": <id>` field with a full nested user object
    in-place under that same "user" key - there is no separate "user_detail" field."""
    response = requests.get(
        f"{api_source.api_root.rstrip('/')}/{path}/",
        params={"tool_id": real_id, "start__gte": start.isoformat(), "start__lt": end.isoformat(), "expand": "user"},
        headers={"Authorization": f"Token {api_source.token}"},
        timeout=30,
        verify=api_source.verify_ssl,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("results", body) if isinstance(body, dict) else body


def get_remote_usage(api_source, real_id, start, end):
    """Read-only GET against a *different* NEMO instance's REST API - see this module's and
    NemoApiSource's docstrings. Only ever called by get_usage_periods_for_range() when nothing
    local overlaps, purely for reference display. Cached (see REMOTE_USAGE_TTL) - never fetched
    live more than once per window per source.

    Fetches *both* usage_events and reservations and combines them (same reasoning as
    get_local_usage: two different signals, shown separately, neither suppresses the other)."""
    if not api_source or not real_id:
        return []

    cache_key = _cache_key(api_source.pk, real_id, start, end)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    combined = []
    for path, source_label in (("usage_events", "usage_event"), ("reservations", "reservation")):
        try:
            rows = _remote_rows(api_source, path, real_id, start, end)
        except (requests.RequestException, ValueError) as e:
            logger.warning("Read-only %s lookup on %r failed: %s", path, api_source.name, e)
            continue

        for row in rows:
            if row.get("cancelled"):
                continue
            row_end = parse_datetime(row["end"]) if row.get("end") else None
            row_start = parse_datetime(row["start"]) if row.get("start") else None
            if row_start is None or row_start >= end or (row_end is not None and row_end <= start):
                continue
            user = row.get("user")
            if isinstance(user, dict):
                display = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get("username")
                username = user.get("username")
            else:
                display = f"user #{user}" if user else None
                username = None
            combined.append(
                {"user": display or "unknown user", "username": username, "start": row_start, "end": row_end, "source": source_label}
            )
    cache.set(cache_key, combined, REMOTE_USAGE_TTL)
    return combined


def get_usage_periods_for_range(tool_name, real_id, start, end, api_source=None):
    """Local-first, read-only-remote-as-fallback lookup for a *whole* time range (e.g. a full
    history page's worth of runs) rather than one run - returns every reservation/usage period
    overlapping [start, end]. Used both by get_run_usage() (one run) and annotate_run_usage()
    (a whole page at once, so one reservation spanning several runs is fetched once instead of
    once per run)."""
    local = get_local_usage(tool_name, start, end)
    if local:
        return local
    remote = get_remote_usage(api_source, real_id, start, end)
    for row in remote:
        row["reference"] = True
        row["reference_from"] = api_source.name if api_source else None
    return remote


def get_run_usage(tool_name, real_id, summary, api_source=None):
    """Top-level lookup used by views.tool_detail: local NEMO data first (always authoritative),
    falling back to a read-only remote lookup only when nothing local matches and an api_source is
    configured for this tool. Returns [] (not an error) if there's no run time window to look up
    at all (e.g. the summary itself errored)."""
    start, end = run_time_window(summary)
    if start is None:
        return []
    return get_usage_periods_for_range(tool_name, real_id, start, end, api_source)


def annotate_run_usage(runs, tool_name, real_id, api_source=None):
    """Mutates `runs` (tool_history's per-page row dicts, already sorted newest-first) in place,
    adding "usage_periods" (the list of every overlapping get_usage_periods_for_range() entry -
    a run can have a usage_event, a reservation, both, or neither) to each - fetching everything
    covering the *whole page's* time range in one lookup instead of one per run, so a single
    reservation spanning several back-to-back runs (someone running several recipes in one booked
    slot) is recognized as covering all of them at once, not re-discovered redundantly per row.

    Also adds "usage_period" (singular - the *primary* one for grouping purposes: a usage_event
    if one overlaps, since actual logged usage is the stronger signal, otherwise a reservation, or
    None), "usage_show_cell" (bool) and "usage_rowspan" (int) so tool_history.html can render one
    rowspan'd cell across a consecutive run of rows sharing the same primary period - a visual
    "bracket" alongside the grouped runs - instead of repeating the same name on every row. Rows
    with no matching period each get their own show_cell=True, rowspan=1 (a plain "-" cell); a run
    with both a usage_event and a reservation shows both (see usage_periods) within that one cell.
    """
    # Two different uses of each run's end timestamp need two different amounts of padding: the
    # *query range* sent to get_usage_periods_for_range() should be generously padded (so a
    # reservation whose own boundary is a few minutes off from the run's still gets fetched at
    # all), but the *raw* run end timestamp - authoritative, straight from the log file - is what
    # actually gets tested against each period's own (separately padded) boundary below.
    padded_windows = [_run_window(run.get("timestamp"), run.get("duration_s")) for run in runs]
    starts = [w[0] for w in padded_windows if w[0] is not None]
    ends = [w[1] for w in padded_windows if w[1] is not None]

    periods = []
    if starts:
        periods = get_usage_periods_for_range(tool_name, real_id, min(starts), max(ends), api_source)

    raw_ends = []
    for run in runs:
        run_end = run.get("timestamp")
        if run_end is not None and timezone.is_naive(run_end):
            run_end = timezone.make_aware(run_end)
        raw_ends.append(run_end)

    for run, run_end in zip(runs, raw_ends):
        overlapping = []
        if run_end is not None:
            for period in periods:
                p_start = period["start"] - OVERLAP_PAD
                p_end = (period["end"] or run_end) + OVERLAP_PAD
                if p_start <= run_end <= p_end:
                    overlapping.append(period)
        run["usage_periods"] = overlapping
        run["usage_period"] = next((p for p in overlapping if p["source"] == "usage_event"), None) or (
            overlapping[0] if overlapping else None
        )

    i, n = 0, len(runs)
    while i < n:
        period = runs[i]["usage_period"]
        j = i + 1
        if period is not None:
            while j < n and runs[j]["usage_period"] is period:
                j += 1
        runs[i]["usage_show_cell"] = True
        runs[i]["usage_rowspan"] = j - i
        for k in range(i + 1, j):
            runs[k]["usage_show_cell"] = False
        i = j
