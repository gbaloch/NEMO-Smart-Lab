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

import functools
import hashlib
import logging
import operator
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

# _remote_rows' server-side `start__gte` lower bound (see its docstring) is necessary to keep the
# query fast, but a flat `start__gte=<run's own window start>` is wrong on its own: a reservation
# that began well before this specific run - and simply covers it, along with several others back
# to back - has an *own* `start` earlier than the run's window, so `start__gte` would exclude it
# even though it genuinely overlaps (confirmed live: a run at 12:29-13:09 sits entirely inside a
# 10:00-14:00 reservation, which get_run_usage's single-run lookup was missing entirely while
# annotate_run_usage's whole-page lookup - whose wider query range happened to reach back past
# 10:00 already - found it, for the exact same run). Padding the lower bound back this far keeps
# the "don't pull years of history" protection while covering any realistically-long single
# reservation/usage session.
REMOTE_LOOKBACK_PAD = timedelta(hours=24)

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
    # shortened=True (NEMO.models.Reservation) marks the *original* reservation once a user ends
    # their tool usage early - NEMO leaves it cancelled=False (it's still real history) but points
    # its `descendant` at a brand new reservation reflecting the actual, shortened time, and its
    # own docstring says the original "will no longer be visible on the calendar". Confirmed live
    # against real prod data: without this exclusion, both the original (e.g. 10:00-14:00, the
    # calendar booking) and its descendant (10:00-13:47, matching when the run actually ended) show
    # up side by side as if they were two unrelated reservations for the same user/day.
    #
    # missed=True marks a reservation nobody ever showed up for before the tool's "missed
    # reservation threshold" passed - confirmed live: a user no-showed an 8:00-11:00pm booking,
    # then separately booked (and used) 9:30-10:55pm the same evening. Both rows are real,
    # independent Reservation records (not an ancestor/descendant pair), but the missed one was
    # never actually honored - showing it alongside the real one as if it were a second valid
    # reservation is misleading, not merely redundant.
    reservations = Reservation.objects.filter(
        tool=tool, cancelled=False, shortened=False, missed=False, start__lt=end, end__gt=start
    )
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

    Filters both `start__lt` (end of our window) and `start__gte` (start of our window, minus
    REMOTE_LOOKBACK_PAD - see its docstring for why a flat, unpadded lower bound is wrong) server
    side - confirmed necessary, not just an optimization: without a lower bound at all this pulled
    every reservation/usage event ever recorded for the tool (1000+ rows, 20+ seconds on a real
    prod tool with years of history) instead of just the handful actually overlapping the window.
    `end__gt`/cancelled are still only checked client-side below, since getting an OR (end is null
    OR end > start) or a boolean filter's exact param spelling right for every possible NEMO
    deployment isn't worth relying on for what's only ever reference data.

    `expand=user` (NEMO's REST API's django-rest-framework-flex-fields support, confirmed against
    the real prod API) replaces the bare `"user": <id>` field with a full nested user object
    in-place under that same "user" key - there is no separate "user_detail" field."""
    response = requests.get(
        f"{api_source.api_root.rstrip('/')}/{path}/",
        params={
            "tool_id": real_id,
            "start__gte": (start - REMOTE_LOOKBACK_PAD).isoformat(),
            "start__lt": end.isoformat(),
            "expand": "user",
        },
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
            # shortened=True / missed=True: see get_local_usage's comments on the same fields.
            # UsageEvent rows never have either key, so this is a no-op for that endpoint's rows.
            if row.get("shortened") or row.get("missed"):
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
    return _reference_rows(api_source, real_id, start, end)


def _reference_rows(api_source, real_id, start, end):
    remote = get_remote_usage(api_source, real_id, start, end)
    for row in remote:
        row["reference"] = True
        row["reference_from"] = api_source.name if api_source else None
    return remote


def _aware(dt):
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def find_user_run_windows(tool_name, queries, real_id=None, api_source=None, remote_range=None):
    """[(start, end), ...] padded windows (naive, in this server's local system timezone - see
    readers._run_start_timestamp's docstring for why) during which a username matching ANY of
    `queries` (case-insensitive substring each, OR'd together - e.g. several usernames tagged onto
    the run history's user filter at once) used this tool.

    THIS NEMO instance's own local Reservation/UsageEvent tables are always tried first. If that
    turns up nothing at all AND `real_id`/`api_source`/`remote_range` are all given, this also
    tries exactly ONE read-only GET against that remote NEMO instance (see get_remote_usage),
    bounded to `remote_range` (typically this tool's own [earliest, latest] run timestamp - see
    readers.get_run_time_range) - the same "local first, remote only as a fallback, purely for
    reference" pattern get_run_usage/get_usage_periods_for_range already use for a single run, just
    widened to cover the tool's whole history since a user search has no one run's own window to
    anchor to. Deliberately never more than this one bounded request per search (see
    _remote_rows' own docstring: an *unbounded* remote query - no lower time bound at all - pulled
    1000+ rows and took 20+ seconds on a real prod tool; still cached afterwards, same
    REMOTE_USAGE_TTL as every other remote lookup, so a repeated search doesn't pay this twice).
    The remote API has no confirmed server-side username filter (only tool_id/start - see
    _remote_rows), so the substring match against `queries` is applied client-side to whatever it
    returns. `remote_range` of (None, None) (the default) skips the remote fallback entirely -
    there would be no time window to safely bound it to.

    Used to filter run history by user without ever parsing a single run's own file content: a
    run's cheap, free-to-read filename/foldername start timestamp is compared against these
    windows instead of computing each run's real usage the expensive way - see
    readers.get_tool_history's `user_windows` param and readers._filter_run_entries.

    Same exclusions as get_local_usage (a shortened/missed reservation isn't real, honored usage)
    and the same OVERLAP_PAD widening, for consistency with how a single run's own usage lookup
    already works. Returns [] (not an error) for no queries or an unrecognized tool name."""
    queries = [q for q in (queries or []) if q]
    if not queries:
        return []
    try:
        tool = Tool.objects.get(name=tool_name)
    except Tool.DoesNotExist:
        return []
    username_match = functools.reduce(operator.or_, (Q(user__username__icontains=q) for q in queries))
    usage_events = list(UsageEvent.objects.filter(username_match, tool=tool).values_list("start", "end"))
    reservations = list(
        Reservation.objects.filter(
            username_match, tool=tool, cancelled=False, shortened=False, missed=False
        ).values_list("start", "end")
    )
    raw_windows = usage_events + reservations

    remote_start, remote_end = remote_range or (None, None)
    if not raw_windows and api_source and real_id and remote_start and remote_end:
        remote_rows = get_remote_usage(api_source, real_id, _aware(remote_start) - OVERLAP_PAD, _aware(remote_end) + OVERLAP_PAD)
        for row in remote_rows:
            username = row.get("username")
            if username and any(q.lower() in username.lower() for q in queries):
                raw_windows.append((row["start"], row["end"]))

    windows = []
    for start, end in raw_windows:
        if start is None:
            continue
        padded_start = _aware(start) - OVERLAP_PAD
        padded_end = _aware(end or start) + OVERLAP_PAD
        # readers.py's own timestamps (a run's filename-embedded start time) are naive, in this
        # server's local system timezone - converting to that same shape here (rather than making
        # readers.py deal with tz-aware datetimes at all) keeps every reader kind-agnostic of
        # Django/timezone concerns entirely, matching its existing "framework-free" design.
        windows.append(
            (timezone.localtime(padded_start).replace(tzinfo=None), timezone.localtime(padded_end).replace(tzinfo=None))
        )
    return windows


def list_tool_usernames(tool_name):
    """Every distinct username with at least one local Reservation or UsageEvent on this tool,
    sorted - autocomplete suggestions for the run history's user filter (see views.tool_history),
    not a validation list: a typed/tagged value that isn't in this list is still accepted as a
    filter by find_user_run_windows, just without a suggestion to click for it. Local-only, same
    reasoning as find_user_run_windows itself - and the same live-usage-only source, so someone who
    has only ever used this tool via a *remote* NemoApiSource's history won't be suggested here."""
    try:
        tool = Tool.objects.get(name=tool_name)
    except Tool.DoesNotExist:
        return []
    usernames = set(UsageEvent.objects.filter(tool=tool).values_list("user__username", flat=True))
    usernames |= set(
        Reservation.objects.filter(tool=tool, cancelled=False, shortened=False, missed=False).values_list(
            "user__username", flat=True
        )
    )
    return sorted(usernames)


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
    # Each run's own padded [start, end] window (the same shape run_time_window()/get_run_usage()
    # build for the single-run detail page) - reused below for the actual per-run overlap test
    # too, not just to compute the whole page's min/max fetch range, so a run's detail page and
    # its row here are guaranteed to agree on which periods overlap it (they used to disagree: an
    # earlier version of this loop only point-tested a period against the run's raw *end*
    # timestamp, which misses a period that overlaps the run's start but ends before the run
    # does - a real case get_run_usage's proper interval-overlap query already handled correctly).
    padded_windows = [_run_window(run.get("timestamp"), run.get("duration_s")) for run in runs]
    starts = [w[0] for w in padded_windows if w[0] is not None]
    ends = [w[1] for w in padded_windows if w[1] is not None]

    # "Local first, remote only as a fallback" is decided PER RUN, not once for the whole page's
    # range: a single local record anywhere in a multi-day range (e.g. one dev-seeded/test usage
    # row) used to make get_usage_periods_for_range() return only local rows and never consult the
    # remote at all, blanking the user for every *other* run on the page (confirmed live: fiji1's
    # runs after one stray local row simply showed no user).
    local_periods = get_local_usage(tool_name, min(starts), max(ends)) if starts else []
    remote_periods = None  # fetched lazily, once, only if some run has no local match

    def _overlaps(p, run_start, run_end):
        # Same interval-overlap semantics as get_local_usage's own DB filter (Q(end__isnull=True)
        # | Q(end__gt=start)) - no extra padding on the period's own boundaries, since
        # run_start/run_end here are already the padded ones get_run_usage would use.
        return p["start"] < run_end and (p["end"] is None or p["end"] > run_start)

    for run, (run_start, run_end) in zip(runs, padded_windows):
        overlapping = []
        if run_start is not None:
            overlapping = [p for p in local_periods if _overlaps(p, run_start, run_end)]
            if not overlapping:
                if remote_periods is None:
                    remote_periods = _reference_rows(api_source, real_id, min(starts), max(ends))
                overlapping = [p for p in remote_periods if _overlaps(p, run_start, run_end)]
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
