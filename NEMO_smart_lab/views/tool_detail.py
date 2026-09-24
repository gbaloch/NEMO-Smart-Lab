"""A single tool's overview / full run detail page."""

from django.http import HttpResponseNotFound
from django.shortcuts import render
from django.views.decorators.http import require_GET

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.readers import (
    DEFAULT_HISTORY_LIMIT,
    get_chart_group_list,
    get_latest_run_id,
    get_recent_faulty_runs,
    get_recent_runs,
    get_run_page_number,
    get_run_screenshot,
    get_tool_summary,
)
from NEMO_smart_lab.recipes import find_recipe_by_name, get_recently_updated_recipes, total_cycles_run
from NEMO_smart_lab.reservations import annotate_run_usage, get_run_usage
from NEMO_smart_lab.status import get_tool_status
from NEMO_smart_lab.templatetags.smart_lab_filters import range_start
from NEMO_smart_lab.views.access import smart_lab_access_required
from NEMO_smart_lab.views.helpers import _base_pressure_recipe_names_html, _primary_run_user, _resolve


@smart_lab_access_required
@require_GET
def tool_detail(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None

    # Tools with a real per-run history (heater_log/mvd) get a distinct "overview" page (no
    # ?run=) - the tool-wide chamber base-pressure trend, a summary of the actual latest run with
    # a link to its own full detail, and any recent faulty/aborted runs - instead of eagerly
    # rendering one specific run's full panel (channels/screenshot/interactive charts/usage) there.
    # That full panel is only shown once a specific run is explicitly requested via ?run=<id>
    # (including the tool's own latest run, reached via the overview page's "View full details"
    # link - see is_latest below). Other kinds (cobra_job/eventlog/waferlog - no linear per-run
    # list, no base-pressure/faulty-run concept) keep the original always-full-detail behavior.
    supports_overview = cfg["kind"] in ("heater_log", "mvd")
    show_full_detail = run_id is not None or not supports_overview

    if not show_full_detail:
        summary = get_tool_summary(name, cfg)
        summary["slug"] = tool_id
        slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
        recent_faulty_runs = [] if summary.get("error") else get_recent_faulty_runs(cfg)
        recent_runs = [] if summary.get("error") else get_recent_runs(cfg)
        if recent_runs:
            # One lookup covering every one of these runs at once (see annotate_run_usage), not
            # one per row - adds "usage_period" ({"user", "username", "source", ...} or None) to
            # each, the same real usage-lookup the full-detail page's own "Tool usage" panel uses.
            annotate_run_usage(recent_runs, name, slt.real_id if slt else None, slt.usage_reference_source if slt else None)
        total_cycles, counted_runs, total_runs = (0, 0, 0)
        if not summary.get("error"):
            try:
                total_cycles, counted_runs, total_runs = total_cycles_run(cfg)
            except remote_sync.RemoteSyncError:
                # An optional stat (needs the recipe tree from Oak) - a slow/unreachable remote
                # should just hide it, not turn the whole overview page into a 500.
                pass
        return render(
            request,
            "NEMO_smart_lab/tool_detail.html",
            {
                "tool": summary,
                # Same live in-use/ready/shutdown signal as the Smart Lab landing page's own tool
                # cards (see dashboard()/_named_summary_and_status and status.get_tool_status) -
                # rendered here via the shared _tool_status_badge.html partial so the two pages can
                # never drift apart from separately-maintained copies of the same badge markup.
                "dashboard_status": get_tool_status(name, summary, slt),
                "show_full_detail": False,
                # Which of this page's two tabs (see tool_detail.html's own tab strip) should be
                # active on load - "trends" only when explicitly requested via ?tab=trends (e.g.
                # the dashboard's own "Trends" button, or tool_maintenance_trends' redirect for an
                # old bookmarked link), "overview" (the default) otherwise. The Trends tab's own
                # content is fetched lazily, client-side, only once actually shown - see
                # views.tool_maintenance_trends's fragment response - so this flag only controls
                # which tab starts visible, not what gets computed server-side here.
                "active_tab": "trends" if request.GET.get("tab") == "trends" else "overview",
                "base_pressure_recipe_names_html": _base_pressure_recipe_names_html(cfg, tool_id),
                "show_base_pressure_history": bool(cfg.get("base_pressure_recipe_names")),
                "show_continuous_pressure": bool(cfg.get("continuous_pressure_subdir")),
                "recent_faulty_runs": recent_faulty_runs,
                "recent_runs": recent_runs,
                "recent_recipes": get_recently_updated_recipes(cfg),
                "total_cycles": total_cycles,
                "total_cycles_counted_runs": counted_runs,
                "total_cycles_total_runs": total_runs,
            },
        )

    summary = get_tool_summary(name, cfg, run_id)
    summary["slug"] = tool_id
    if supports_overview:
        # Resolved once and reused for both is_latest and the "Jump to the most recent run" link
        # below - that link needs the id itself (as an explicit ?run= param), not just a bool,
        # since the overview page (what a bare, run-less URL resolves to for these tool kinds -
        # see show_full_detail above) isn't "the most recent run"'s own full detail page.
        latest_run_id = get_latest_run_id(cfg)
        is_latest = run_id is not None and run_id == latest_run_id
    else:
        latest_run_id = None
        is_latest = run_id is None

    slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
    # Same live in-use/ready/shutdown signal as the overview page/landing page (see
    # status.get_tool_status) - always reflects the tool's own CURRENT state, not necessarily the
    # run being viewed here: `summary` already IS the current state when viewing the latest run
    # (is_latest), reused as-is, but a past run's own recipe/error would give a misleading "current"
    # status otherwise, so a fresh, separate lookup (cheap - each file's own parse is cached) is
    # used instead whenever viewing anything other than the latest run.
    dashboard_status = get_tool_status(name, summary if is_latest else get_tool_summary(name, cfg), slt)
    run_usage = (
        get_run_usage(name, slt.real_id if slt else None, summary, slt.usage_reference_source if slt else None)
        if not summary.get("error")
        else []
    )
    has_screenshot = not summary.get("error") and get_run_screenshot(cfg, run_id) is not None
    chart_groups = [] if summary.get("error") else get_chart_group_list(cfg, run_id)
    # See _primary_run_user - passed through to the chart endpoints (as a query param, not a
    # fresh lookup - see tool_chart_data/tool_chart) so a chart's title can read "<recipe> -
    # <username>".
    run_user = _primary_run_user(run_usage)
    run_username = run_user["username"] if run_user else None
    # Same "resolve once here, pass through as a query param" approach as run_username above - the
    # run's own end timestamp (already computed for the "Last update"/"Run ended" row on this same
    # page), formatted the same way as everywhere else on the site, so a chart's title can read
    # "<recipe> - <username> - <timestamp>".
    run_timestamp = range_start(summary.get("last_update")) if not summary.get("error") else None
    # Which page of the (default page-size) run history table this run itself falls on - so
    # "View run history" from a past run's own detail page jumps straight to it instead of always
    # landing on page 1 and leaving the viewer to go hunt for it there.
    run_history_page = (
        get_run_page_number(cfg, run_id, DEFAULT_HISTORY_LIMIT)
        if run_id and supports_overview and not summary.get("error")
        else None
    )
    # Best-effort "the recipe that (as far as we can tell) produced this run" link - None (no
    # link shown) unless exactly one current recipe file's name matches this run's own recorded
    # recipe name, so this never guesses at an ambiguous or stale match.
    matching_recipe = None if summary.get("error") else find_recipe_by_name(cfg, summary.get("recipe"))

    return render(
        request,
        "NEMO_smart_lab/tool_detail.html",
        {
            "tool": summary,
            "dashboard_status": dashboard_status,
            "show_full_detail": True,
            "is_latest": is_latest,
            "supports_overview": supports_overview,
            "latest_run_id": latest_run_id,
            "run_history_page": run_history_page,
            "matching_recipe": matching_recipe,
            "chart_groups": chart_groups,
            "run_username": run_username,
            "run_timestamp": run_timestamp,
            # Shown as two separate lists (not merged) - a UsageEvent (actual logged usage) and a
            # Reservation (calendar intent, which may cover a wider window or may not have been
            # used at all) are different signals; a run can have either, both, or neither.
            "run_usage_events": [e for e in run_usage if e["source"] == "usage_event"],
            "run_reservations": [e for e in run_usage if e["source"] == "reservation"],
            "has_screenshot": has_screenshot,
        },
    )
