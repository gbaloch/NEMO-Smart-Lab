"""Access control for the Smart Lab views: staff/permission gate and the staging machine's API-key gate."""

from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.utils.crypto import constant_time_compare


def _can_access_smart_lab(user):
    # Staff/superusers always get in - a superuser's has_perm() is already unconditionally True
    # for every permission, so the explicit is_staff check is really only what lets a *non*-staff
    # user in when they haven't been granted the permission below. Anyone else - a specific user or
    # a whole group - can be let in without making them staff by granting them the
    # "smart_lab.access_smart_lab" permission from the ordinary Django admin Users/Groups screens
    # (SmartLabTool's Meta.permissions - no separate settings toggle to maintain).
    return user.is_staff or user.has_perm("smart_lab.access_smart_lab")


def smart_lab_access_required(view_func):
    """Restricts a view to staff/superusers, or anyone else explicitly granted the
    "smart_lab.access_smart_lab" permission - see _can_access_smart_lab. An unauthenticated
    request redirects to login (matching plain @login_required); an authenticated-but-unauthorized
    one gets a 403, not a login redirect loop."""

    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not _can_access_smart_lab(request.user):
            raise PermissionDenied("Smart Lab access is restricted to staff.")
        return view_func(request, *args, **kwargs)

    return wrapped


def staging_api_key_required(view_func):
    """Restricts a view to requests carrying the shared secret configured as
    settings.SMART_LAB_STAGING_API_KEY, via an "Authorization: Token <key>" header - the
    machine-to-machine equivalent of smart_lab_access_required, for the one endpoint
    (tool_sync_map) an unattended staging-machine script needs to call with no Django session at
    all (see staging/scripts/lib/nemo-tool-map.sh).

    Fails closed: no key configured on this NEMO instance means every request is refused, never
    "anything goes" - a deployment that hasn't set this up yet just doesn't expose the endpoint,
    rather than accidentally exposing it unauthenticated. Uses constant_time_compare (not a plain
    `==`) so responding slightly slower for a right-prefix-wrong-suffix guess can't leak how much
    of the key an attacker has gotten right so far."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        configured_key = getattr(settings, "SMART_LAB_STAGING_API_KEY", "") or ""
        if not configured_key:
            raise PermissionDenied("Smart Lab staging API is not configured on this instance.")
        expected = f"Token {configured_key}"
        if not constant_time_compare(request.headers.get("Authorization", ""), expected):
            raise PermissionDenied("Invalid or missing API key.")
        return view_func(request, *args, **kwargs)

    return wrapped
