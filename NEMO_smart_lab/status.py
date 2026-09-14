"""
Computes a "meaningful" overview-page status for a tool - not the raw "any heater channel
currently reads hot" heuristic (that's readers.py's per-run "status_label"/"any_on", still shown
on the tool detail page), but the lifecycle state staff actually care about at a glance:

1. Currently in active use (someone is logged into it right now) - checked live, every time, not
   from any recipe-name heuristic. Local Reservation/UsageEvent data is authoritative when this
   NEMO instance actually owns the tool (NEMO.models.Tool.get_current_usage_event()); only falls
   back to a read-only remote check (this tool's configured usage_reference_source, if any) when
   there's no local Tool row for it at all - never both, since a local Tool row already being
   correct-but-currently-idle is a real, true answer, not a reason to also ask elsewhere.
2. Marked non-operational (NEMO.models.Tool.operational - False whenever there's an unresolved
   Task with force_shutdown=True against it, or a staff member has otherwise flagged it
   non-operational by hand) - "Shut down", regardless of whether any recipe says so. This is the
   whole point of checking it here rather than relying only on recipe-name matching: a tool can be
   shut down by staff/a task with no recipe run at all to indicate it. Checked on BOTH a local Tool
   row (if one exists) AND this tool's configured usage_reference_source (if any) - unlike the
   in-use check above, these are never mutually exclusive: a local Tool row's own operational flag
   isn't necessarily kept live-synced with a separately-configured remote source of truth (e.g. a
   local dev/staging copy of a tool imported from prod once and never updated since), so an
   explicit False from either side is trusted.
3. Otherwise, the latest completed run's own recipe name, matched case-insensitively against this
   tool's admin-configured keyword lists (SmartLabTool.{shutdown,standby,valve_clean}_recipe_keywords),
   checked in that order - first match wins. No match at all falls back to a plain "Ready".

Strictly read-only wherever it touches a remote NEMO instance - see reservations.py's module
docstring for the same standing constraint; nothing here ever issues anything but a GET.
"""

import logging

import requests
from django.core.cache import cache

from NEMO.models import Tool, UsageEvent

logger = logging.getLogger(__name__)

# Unlike REMOTE_USAGE_TTL (reservations.py, 8 hours - fine for "what happened historically"),
# "is anyone using this right now" is only useful if it's actually current - an 8-hour-stale
# answer here would be actively wrong, not just imprecise. Short enough to stay meaningfully live,
# long enough that repeatedly refreshing/switching between dashboard tabs doesn't hammer a remote
# NEMO instance's API with a live-status GET on every single page load.
LIVE_STATUS_TTL = 30


def _parse_keywords(raw):
    return [k.strip().lower() for k in (raw or "").split(",") if k.strip()]


def _matches(recipe, raw_keywords):
    if not recipe:
        return False
    keywords = _parse_keywords(raw_keywords)
    if not keywords:
        return False
    recipe_lower = recipe.lower()
    return any(kw in recipe_lower for kw in keywords)


def _display_name(user):
    return user.get_name().strip() or user.username


def _local_tool_state(tool_name):
    """None if this NEMO instance has no Tool by this name at all (a remote-only tool) - both
    "active_user" (None if nobody's on it right now) and "operational" (NEMO's own non-operational
    flag - see module docstring) are real, authoritative local signals whenever a Tool row does
    exist, taking priority over anything remote/recipe-based."""
    try:
        tool = Tool.objects.get(name=tool_name)
    except Tool.DoesNotExist:
        return None
    event = UsageEvent.objects.filter(tool_id__in=tool.get_family_tool_ids(), end__isnull=True).select_related("user").first()
    active_user = {"name": _display_name(event.user), "username": event.user.username} if event is not None else None
    return {"active_user": active_user, "operational": tool.operational}


def _remote_active_user(api_source, real_id):
    if not api_source or not real_id:
        return None
    cache_key = f"smart_lab:live_status:{api_source.pk}:{real_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached != "none" else None

    result = "none"
    try:
        response = requests.get(
            f"{api_source.api_root.rstrip('/')}/usage_events/",
            params={"tool_id": real_id, "end__isnull": "true", "expand": "user"},
            headers={"Authorization": f"Token {api_source.token}"},
            timeout=10,
            verify=api_source.verify_ssl,
        )
        response.raise_for_status()
        body = response.json()
        rows = body.get("results", body) if isinstance(body, dict) else body
        if rows:
            user = rows[0].get("user")
            if isinstance(user, dict):
                display = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get("username")
                result = {"name": display or "unknown user", "username": user.get("username")}
    except (requests.RequestException, ValueError) as e:
        logger.warning("Read-only live-status lookup on %r failed: %s", api_source.name, e)

    cache.set(cache_key, result, LIVE_STATUS_TTL)
    return None if result == "none" else result


def _remote_tool_operational(api_source, real_id):
    """Read-only GET of a single tool's own operational flag from a *different* NEMO instance's
    REST API (<api_root>/tools/<real_id>/) - the remote counterpart to _local_tool_state's own
    tool.operational check, consulted by get_tool_status whenever a usage_reference_source is
    configured for this tool - REGARDLESS of whether a local Tool row also exists (unlike the "is
    anyone using it right now" check, a local Tool row's own operational flag isn't necessarily
    kept live-synced with a separately-configured remote source of truth - see get_tool_status's
    own comment for why).

    A real, previously-missing check: without this, a tool with a configured remote reference
    source could be flagged non-operational/shut down on the remote NEMO instance itself - e.g. by
    a force_shutdown Task, or a staff member marking it down by hand - and this dashboard would
    never know, since get_tool_status only ever asked the remote side "is anyone using it right
    now" (_remote_active_user), never "is it actually operational" at all - confirmed live:
    savannah's own remote shutdown was invisible here (a stale, locally-present Tool row's own
    operational=True short-circuited everything else), still showing "Ready" from recipe-keyword
    fallback alone.

    Prefers the response's "operational" key (NEMO.serializers.ToolSerializer's computed property -
    _operational AND no unresolved force_shutdown Task) when present, but falls back to "_operational"
    (the raw underlying field) otherwise - confirmed live against the real production API this is
    actually pointed at: it serializes Tool with "_operational" only, not the computed "operational"
    property, so requiring the latter would make this check silently do nothing against real data.
    "_operational" alone can't reflect a force_shutdown Task with no corresponding raw-field change,
    but it's the best signal this particular deployment's API actually exposes.

    Returns True/False, or None (not a guess) if this can't be determined at all (no api_source/
    real_id, the request itself fails, or the response has neither field) - a caller should treat
    None the same as "unknown", not as "operational", but also not synthesize "shut down" from it;
    recipe-keyword matching still applies as this tool's own fallback either way. Cached the same
    short LIVE_STATUS_TTL as _remote_active_user - this needs to stay meaningfully current, not
    just fast."""
    if not api_source or not real_id:
        return None
    cache_key = f"smart_lab:live_operational:{api_source.pk}:{real_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return None if cached == "unknown" else cached

    result = "unknown"
    try:
        response = requests.get(
            f"{api_source.api_root.rstrip('/')}/tools/{real_id}/",
            headers={"Authorization": f"Token {api_source.token}"},
            timeout=10,
            verify=api_source.verify_ssl,
        )
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict):
            if "operational" in body:
                result = bool(body["operational"])
            elif "_operational" in body:
                result = bool(body["_operational"])
    except (requests.RequestException, ValueError) as e:
        logger.warning("Read-only operational-status lookup on %r failed: %s", api_source.name, e)

    cache.set(cache_key, result, LIVE_STATUS_TTL)
    return None if result == "unknown" else result


def get_tool_status(name, summary, slt):
    """`summary` is a NEMO_smart_lab.readers.get_tool_summary() dict; `slt` is the tool's
    SmartLabTool row (or None, e.g. a tool not yet migrated to the DB-backed config). Returns
    {"code", "label", "css_class", "user", "username"} - "user"/"username" are only set for
    "in_use"."""
    local = _local_tool_state(name)
    active = None
    non_operational = False
    if local is not None:
        active = local["active_user"]
        non_operational = not local["operational"]
    elif slt is not None and slt.usage_reference_source_id:
        active = _remote_active_user(slt.usage_reference_source, slt.real_id)

    # A tool's own "operational" flag is a manually-set admin signal (a force_shutdown Task, or a
    # staff member flagging it down by hand) that can be toggled directly on whichever NEMO
    # instance is actually this tool's source of truth - unlike "is anyone using it right now"
    # (`active` above), where a local instance that genuinely owns usage tracking would already see
    # a real UsageEvent for itself, a LOCAL Tool row's own `operational` flag is NOT necessarily
    # kept live-synced with a separately-configured remote reference source at all (e.g. a local
    # dev/staging copy of a tool imported from prod once and never updated since). So this ALWAYS
    # also checks the remote side when a usage_reference_source is configured - regardless of
    # whether a local Tool row already exists and answered the (separate) in-use question above -
    # and trusts an explicit remote False either way. Only an explicit False (confirmed
    # non-operational) can flip this on - None (lookup failed/not configured) must never be treated
    # as non-operational, or a transient remote-API hiccup would misreport a normal tool as "Shut
    # down" instead of falling through to its recipe-keyword status. Confirmed live: a real,
    # previously-missing check - a tool's own remote shutdown was invisible here before this
    # existed, since a stale-but-present local Tool row's own operational=True short-circuited
    # everything else, even with a remote reference source explicitly configured for this exact
    # tool.
    if not non_operational and slt is not None and slt.usage_reference_source_id:
        if _remote_tool_operational(slt.usage_reference_source, slt.real_id) is False:
            non_operational = True

    if active is not None:
        return {
            "code": "in_use",
            "label": "In use",
            "css_class": "primary",
            "user": active["name"],
            "username": active["username"],
        }

    recipe = summary.get("recipe") if not summary.get("error") else None
    if non_operational or (slt is not None and _matches(recipe, slt.shutdown_recipe_keywords)):
        return {"code": "shutdown", "label": "Shut down", "css_class": "danger", "user": None, "username": None}
    if slt is not None:
        if _matches(recipe, slt.standby_recipe_keywords):
            return {"code": "ready_standby", "label": "Ready - standby", "css_class": "success", "user": None, "username": None}
        if _matches(recipe, slt.valve_clean_recipe_keywords):
            return {"code": "ready_valve_clean", "label": "Ready - clean", "css_class": "success", "user": None, "username": None}
    return {"code": "ready", "label": "Idle", "css_class": "default", "user": None, "username": None}
