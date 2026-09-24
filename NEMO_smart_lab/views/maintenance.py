"""A tool's maintenance trends page."""

from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.readers import get_fault_rate_trend, get_mvd_maintenance_trends, get_pump_down_trend
from NEMO_smart_lab.views.access import smart_lab_access_required
from NEMO_smart_lab.views.helpers import _resolve


def _maintenance_trends_context(cfg):
    """The actual trend computation for tool_maintenance_trends, factored out so it can be reused
    unchanged by both that view's fragment response (see its own docstring) and any future caller -
    returns exactly the template context this data needs, independent of the request/response
    shape around it."""
    is_mvd = cfg["kind"] == "mvd"
    try:
        fault_trend = get_fault_rate_trend(cfg)
        if is_mvd:
            # One shared scan for every mvd-specific signal (see get_mvd_maintenance_trends'
            # own docstring for why this matters: calling the four underlying trends separately
            # measured live at 58-76s for fiji5, each redundantly re-scanning/re-parsing the same
            # runs the others already had).
            mvd_trends = get_mvd_maintenance_trends(cfg)
            pump_down_trend = mvd_trends["pump_down"]
            mfc_drift_trend = mvd_trends["mfc_drift"]
            rf_health_trend = mvd_trends["rf_health"]
            turbo_speed_trend = {"reactor": mvd_trends["turbo_reactor"], "load_lock": mvd_trends["turbo_load_lock"]}
        else:
            pump_down_trend = get_pump_down_trend(cfg)
            mfc_drift_trend = rf_health_trend = []
            turbo_speed_trend = {"reactor": [], "load_lock": []}
        error = None
    except remote_sync.RemoteSyncError as e:
        fault_trend = pump_down_trend = mfc_drift_trend = rf_health_trend = []
        turbo_speed_trend = {"reactor": [], "load_lock": []}
        error = str(e)
    return {
        "is_mvd": is_mvd,
        "error": error,
        "fault_trend": fault_trend,
        "pump_down_trend": pump_down_trend,
        "mfc_drift_trend": mfc_drift_trend,
        "rf_health_trend": rf_health_trend,
        "turbo_speed_trend": turbo_speed_trend,
    }


@smart_lab_access_required
@require_GET
def tool_maintenance_trends(request, tool_id):
    """The tool-wide maintenance/health trends - now surfaced as the "Trends" tab on the tool's own
    overview page (views.tool_detail) rather than a separate page, so this endpoint has two faces:

    - `?fragment=1` (what the Trends tab itself fetches, lazily, only once actually clicked into -
      see tool_detail.html's own tab-switching script) returns just the trends markup, no
      surrounding page chrome, ready to drop straight into that tab's mount div.
    - Anything else (a direct visit - an old bookmark, a typed URL) redirects to the tool's
      overview page with the Trends tab preselected (?tab=trends), so a stale link still lands
      somewhere sensible instead of a now-orphaned standalone page.

    Trend computation itself is unchanged - see _maintenance_trends_context - and the tool-kind
    404 check runs before either path, so a kind with no maintenance concept behaves identically
    either way."""
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    if cfg["kind"] not in ("heater_log", "mvd"):
        return HttpResponseNotFound("Maintenance trends aren't available for this tool kind")

    if request.GET.get("fragment") != "1":
        return redirect(f"{reverse('smart_lab_tool_detail', args=[tool_id])}?tab=trends")

    return render(request, "NEMO_smart_lab/_tool_health_trends.html", _maintenance_trends_context(cfg))
