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
2. Marked non-operational on NEMO itself (NEMO.models.Tool.operational - False whenever there's an
   unresolved Task with force_shutdown=True against it, or a staff member has otherwise flagged it
   non-operational by hand) - "Shut down", regardless of whether any recipe says so. This is the
   whole point of checking it here rather than relying only on recipe-name matching: a tool can be
   shut down by staff/a task with no recipe run at all to indicate it.
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
        return {"code": "shutdown", "label": "Shut down", "css_class": "default", "user": None, "username": None}
    if slt is not None:
        if _matches(recipe, slt.standby_recipe_keywords):
            return {"code": "ready_standby", "label": "Ready - standby", "css_class": "success", "user": None, "username": None}
        if _matches(recipe, slt.valve_clean_recipe_keywords):
            return {"code": "ready_valve_clean", "label": "Ready - clean", "css_class": "success", "user": None, "username": None}
    return {"code": "ready", "label": "Ready", "css_class": "default", "user": None, "username": None}
