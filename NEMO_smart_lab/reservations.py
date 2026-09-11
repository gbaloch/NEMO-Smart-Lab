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

import logging
from datetime import timedelta

import requests
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from NEMO.models import Reservation, Tool, UsageEvent

logger = logging.getLogger(__name__)

# Reservations/usage events rarely line up to the second with a run's own log timestamps (clock
# drift between the tool PC and the reservation system, rounding in a run's own duration figure,
# etc.) - widen the run's [start, end] window by this much on each side before matching.
OVERLAP_PAD = timedelta(minutes=5)


def run_time_window(summary):
    """Best-effort [start, end] for a run, from the two fields every readers.py summary dict
    already carries: "last_update" (the run's end) and "run_duration_s" (its length). Returns
    (None, None) if last_update itself is missing (e.g. the summary is an error dict).

    readers.py builds "last_update" with datetime.fromtimestamp() (a naive datetime, in this
    server's local system timezone) since it has no timezone info of its own to work with; NEMO
    runs with USE_TZ=True, so it has to be made timezone-aware before it can be compared against
    Reservation/UsageEvent's aware start/end fields without Django silently warning and guessing."""
    end = summary.get("last_update")
    if end is None:
        return None, None
    if timezone.is_naive(end):
        end = timezone.make_aware(end)
    start = end - timedelta(seconds=summary.get("run_duration_s") or 0)
    return start - OVERLAP_PAD, end + OVERLAP_PAD


def _user_display(user):
    return user.get_name().strip() or user.username


def get_local_usage(tool_name, start, end):
    """Queries this NEMO instance's own UsageEvent, then Reservation, for `tool_name` overlapping
    [start, end]. Returns a list of {"user", "start", "end", "source"} dicts. UsageEvent (actual
    logged tool usage) is the stronger signal: if any exists, Reservation (a booking, which may or
    may not have actually been used) isn't even consulted."""
    try:
        tool = Tool.objects.get(name=tool_name)
    except Tool.DoesNotExist:
        return []

    usage_events = UsageEvent.objects.filter(tool=tool, start__lt=end).filter(
        Q(end__isnull=True) | Q(end__gt=start)
    )
    results = [
        {"user": _user_display(e.user), "start": e.start, "end": e.end, "source": "usage_event"}
        for e in usage_events.select_related("user")
    ]
    if results:
        return results

    reservations = Reservation.objects.filter(tool=tool, cancelled=False, start__lt=end, end__gt=start)
    return [
        {"user": _user_display(r.user), "start": r.start, "end": r.end, "source": "reservation"}
        for r in reservations.select_related("user")
    ]


def _remote_rows(api_source, path, real_id, end):
    """One read-only GET, tolerant of both a bare list and NEMO's default paginated
    {"results": [...]} response shape. Filters only by tool_id/start__lt server-side (the widest
    net that's unambiguous across NEMO versions/filter backends) - the exact overlap and
    cancelled-reservation checks happen client-side below on the returned rows instead, since
    getting an OR (end is null OR end > start) or a boolean filter's exact param spelling right
    for every possible NEMO deployment isn't worth relying on for what's only ever reference data."""
    response = requests.get(
        f"{api_source.api_root.rstrip('/')}/{path}/",
        params={"tool_id": real_id, "start__lt": end.isoformat()},
        headers={"Authorization": f"Token {api_source.token}"},
        timeout=10,
        verify=api_source.verify_ssl,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("results", body) if isinstance(body, dict) else body


def get_remote_usage(api_source, real_id, start, end):
    """Read-only GET against a *different* NEMO instance's REST API - see this module's and
    NemoApiSource's docstrings. Only ever called by get_run_usage() when get_local_usage() found
    nothing, purely for reference display."""
    if not api_source or not real_id:
        return []

    for path, source_label in (("usage_events", "usage_event"), ("reservations", "reservation")):
        try:
            rows = _remote_rows(api_source, path, real_id, end)
        except (requests.RequestException, ValueError) as e:
            logger.warning("Read-only %s lookup on %r failed: %s", path, api_source.name, e)
            continue

        results = []
        for row in rows:
            if row.get("cancelled"):
                continue
            row_end = parse_datetime(row["end"]) if row.get("end") else None
            row_start = parse_datetime(row["start"]) if row.get("start") else None
            if row_start is None or row_start >= end or (row_end is not None and row_end <= start):
                continue
            user = row.get("user_detail") or {}
            display = (
                f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                or user.get("username")
                or (f"user #{row['user']}" if row.get("user") else "unknown user")
            )
            results.append({"user": display, "start": row_start, "end": row_end, "source": source_label})
        if results:
            return results
    return []


def get_run_usage(tool_name, real_id, summary, api_source=None):
    """Top-level lookup used by views.tool_detail: local NEMO data first (always authoritative),
    falling back to a read-only remote lookup only when nothing local matches and an api_source is
    configured for this tool. Returns [] (not an error) if there's no run time window to look up
    at all (e.g. the summary itself errored)."""
    start, end = run_time_window(summary)
    if start is None:
        return []

    local = get_local_usage(tool_name, start, end)
    if local:
        return local

    remote = get_remote_usage(api_source, real_id, start, end)
    for row in remote:
        row["reference_from"] = api_source.name
    return remote
