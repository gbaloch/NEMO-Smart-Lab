import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_GET, require_POST

from NEMO_smart_lab.charts import (
    get_base_pressure_chart_json,
    get_chart_json,
    get_continuous_pressure_chart_json,
    get_stream_chart_json,
    render_base_pressure_chart_png,
    render_base_pressure_csv,
    render_chart_png,
    render_stream_chart_png,
)
from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.config import get_tool_sources, invalidate_tool_sources_cache
from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.readers import (
    DEFAULT_HISTORY_LIMIT,
    count_runs_by_recipe_name,
    count_runs_for_recipe,
    get_chart_group_list,
    get_fault_rate_trend,
    get_latest_run_id,
    get_mvd_maintenance_trends,
    get_pump_down_trend,
    get_recent_faulty_runs,
    get_recent_runs,
    get_run_page_number,
    get_run_screenshot,
    get_run_time_range,
    get_tool_history,
    get_tool_summary,
)
from NEMO_smart_lab.configs import find_active_config_file, get_config_file_detail, list_config_files
from NEMO_smart_lab.recipes import (
    _strip_txt_suffixes,
    base_pressure_recipe_targets,
    find_duplicate_recipes,
    find_recipe_by_name,
    get_base_pressure_recipe_links,
    get_recently_updated_recipes,
    get_recipe_detail,
    list_recipes,
    total_cycles_run,
)
from NEMO_smart_lab.reservations import annotate_run_usage, find_user_run_windows, get_run_usage, list_tool_usernames
from NEMO_smart_lab.status import get_tool_status
from NEMO_smart_lab.templatetags.smart_lab_filters import range_start

UNCATEGORIZED = "Uncategorized"


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


def _tool_group_label(category):
    """NEMO's own Tool.category convention is a "/"-delimited "Building/Sub-category" string
    (e.g. "Allen/Atomic Layer Deposition") - group the Smart Lab dashboard by just the last
    segment, since that's the meaningful grouping for staff (which process family a tool belongs
    to), not which building it's physically in."""
    if not category:
        return UNCATEGORIZED
    return category.rsplit("/", 1)[-1].strip() or UNCATEGORIZED


def _resolve(tool_id):
    """Looks up a tool by its real NEMO Tool.id (see config.get_tool_sources' "id") against the
    *current* set of configured tools, so an admin edit (rename, enable/disable, new tool) takes
    effect on the next request rather than needing a server restart. Matching on the tool's real
    id (the same one prod NEMO already uses) rather than a name-derived slug means a Smart Lab URL
    is exactly as stable/shareable as any other Tool-id-keyed URL, and a tool rename never
    changes/breaks a bookmarked or shared one. Returns (name, cfg) or (None, None)."""
    for name, cfg in get_tool_sources().items():
        if cfg.get("id") == tool_id:
            return name, cfg
    return None, None


def _parse_date_param(raw):
    """Parses a run history date filter's own "YYYY-MM-DD" query param (what an <input type="date">
    always submits) into a plain date - None (not an error) for a missing/blank/malformed value, so
    an invalid or absent date param behaves exactly like "no filter" rather than a 500."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _page_numbers(current, total):
    """Windowed page list for pagination controls: first, last, and a few around current.
    None entries mark a gap that should render as "..."."""
    if total <= 7:
        return list(range(1, total + 1))
    keep = {1, total, current, current - 1, current + 1}
    keep = sorted(p for p in keep if 1 <= p <= total)
    result = []
    previous = None
    for p in keep:
        if previous is not None and p - previous > 1:
            result.append(None)
        result.append(p)
        previous = p
    return result


def _primary_run_user(run_usage):
    """The run's actual user, if known, from a get_run_usage() list - {"user": full display name,
    "username": bare username, ...} (the whole matching entry - see reservations.get_local_usage/
    get_remote_usage for its exact shape), so a caller can show either or both. A usage_event
    (actual logged usage) wins over a reservation (calendar intent, which may not reflect who
    actually showed up) when both exist, the same priority annotate_run_usage() already uses for
    its own "primary" period. None if run_usage is empty or nothing in it carries a username."""
    return next((e for e in run_usage if e["source"] == "usage_event" and e.get("username")), None) or next(
        (e for e in run_usage if e.get("username")), None
    )


def _named_summary_and_status(item):
    """Computed together, in the same worker thread, so the dashboard's status feature (which
    needs a live "is it in use right now" check - see NEMO_smart_lab.status) doesn't add a second,
    serial round of per-tool work after the summaries are already fetched concurrently below.
    Also resolves the latest run's own user here (see _primary_run_user) - the dashboard's "Last
    run" line shows both their full name and username - and whether this tool kind has its own
    Trends tab (on its overview page - see views.tool_detail/tool_maintenance_trends) at all, both
    cheap enough to fold into this same per-tool worker rather than adding a second pass afterward."""
    name, cfg, slt = item
    summary = get_tool_summary(name, cfg)
    if not summary.get("error"):
        run_usage = get_run_usage(name, slt.real_id if slt else None, summary, slt.usage_reference_source if slt else None)
        run_user = _primary_run_user(run_usage)
        summary["run_username"] = run_user["username"] if run_user else None
        summary["run_user_display"] = run_user["user"] if run_user else None
    summary["supports_maintenance"] = cfg["kind"] in ("heater_log", "mvd")
    return name, summary, get_tool_status(name, summary, slt)


def _recipe_run_counts(cfg):
    """{normalized recipe name: run_count} - readers.count_runs_by_recipe_name keyed by each
    run's own *exact* embedded name, merged here the same case-insensitive/".txt"-stripped way
    find_recipe_by_name already matches a run's recorded name against an actual recipe file, so a
    recipe file's own listing entry can look itself up regardless of minor spelling differences
    between the two. Used for the recipe list's own "Runs" column - see _grouped_recipes."""
    merged = {}
    for name, count in count_runs_by_recipe_name(cfg).items():
        key = _strip_txt_suffixes(name).lower()
        merged[key] = merged.get(key, 0) + count
    return merged


def _grouped_recipes(cfg):
    """list_recipes() is already sorted by (category, name) - group it into
    [{"category", "recipes", "pinned"}, ...] for tool_data.html's Recipes tab, same spirit as
    dashboard()'s _tool_group_label grouping. "pinned" drives both the pin icon's current state and
    the toggle form's action (pin vs. unpin) for that folder. Each recipe entry also gets
    "run_count" (see _recipe_run_counts) for the list's sortable "Runs" column - a least/most-used
    leaderboard for free, since the column is already there to sort by - and
    "is_base_pressure_recipe" (see base_pressure_recipe_targets), the same "this is one of the
    tool's configured standby/base-pressure recipes" signal recipe_detail.html already shows on one
    recipe's own page, surfaced here too so it's visible while just browsing the list, not only
    after already clicking into a specific recipe."""
    pinned = cfg.get("pinned_recipe_categories") or []
    run_counts = _recipe_run_counts(cfg)
    base_pressure_targets = base_pressure_recipe_targets(cfg)
    groups = []
    for recipe in list_recipes(cfg):
        normalized_name = _strip_txt_suffixes(recipe["name"]).lower()
        recipe = {
            **recipe,
            "run_count": run_counts.get(normalized_name, 0),
            "is_base_pressure_recipe": normalized_name in base_pressure_targets,
        }
        if not groups or groups[-1]["category"] != recipe["category"]:
            groups.append({"category": recipe["category"], "recipes": [], "pinned": recipe["category"] in pinned})
        groups[-1]["recipes"].append(recipe)
    return groups


def _grouped_config_files(cfg):
    """Same grouping as _grouped_recipes, for config_list.html - no pinning support for config
    files (not asked for, and these folders are typically far smaller/simpler than recipes)."""
    groups = []
    for config_file in list_config_files(cfg):
        if not groups or groups[-1]["category"] != config_file["category"]:
            groups.append({"category": config_file["category"], "files": []})
        groups[-1]["files"].append(config_file)
    return groups


def _recipe_name_choices(cfg):
    """Distinct, ".txt"-stripped recipe names (sorted, case-insensitive) - autocomplete
    suggestions for the run history's recipe filter (see tool_history.html's tag input), not a
    validation list: a tagged value that isn't in this list is still accepted as a filter (see
    readers._filter_run_entries), just without a suggestion to click for it. The same recipe name
    routinely exists as several files (one per folder - see recipes.list_recipes' own docstring),
    which would otherwise show up as several identical-looking suggestions."""
    try:
        recipes = list_recipes(cfg)
    except remote_sync.RemoteSyncError:
        return []
    seen = {}
    for r in recipes:
        name = _strip_txt_suffixes(r["name"])
        seen.setdefault(name.lower(), name)
    return sorted(seen.values(), key=str.lower)


def _base_pressure_recipe_names_html(cfg, tool_slug):
    """Pre-rendered, comma-joined HTML for the base-pressure chart's own description (each
    configured recipe name linked to its detail page when find_recipe_by_name resolves it - see
    recipes.get_base_pressure_recipe_links) - built here with format_html_join rather than a
    {% for %} loop in the template itself, since a template loop's own whitespace/indentation
    between tags ends up as stray spaces in the rendered text (confirmed live: "10 - STANDBY 100C
    , 15 - STANDBY 150C" - a space before every comma) that plain HTML whitespace collapsing
    doesn't fully hide. The returned string is already escaped/safe - render it directly, no
    further templating needed."""
    links = get_base_pressure_recipe_links(cfg)
    return format_html_join(
        ", ",
        "{}",
        (
            (
                format_html(
                    '<a href="{}">{}</a>', reverse("smart_lab_tool_recipe_detail", args=[tool_slug, link["recipe"]["id"]]), link["name"]
                )
                if link["recipe"]
                else link["name"],
            )
            for link in links
        ),
    )


@staging_api_key_required
@require_GET
def tool_sync_map(request):
    """{"<tool name>": "<remote directory>"} for every enabled, remote-sync-configured tool - a
    read-only, machine-to-machine endpoint (see staging_api_key_required) for the staging
    machine's shell scripts to fetch this mapping at request time instead of each one hardcoding
    its own copy of it (see staging/scripts/lib/nemo-tool-map.sh and staging/README.md's former
    "Known limitations"). Deliberately the *only* thing this exposes - a tool's local_root,
    thresholds, channel labels, etc. are never relevant to what the staging machine does (push raw
    files from a mount to Oak) and aren't included.

    Only tools with a sync_endpoint configured are included - one with no remote sync has no Oak
    directory for the staging pipeline to push into in the first place."""
    mapping = {
        tool.name: tool.remote_subdir_or_default
        for tool in SmartLabTool.objects.filter(enabled=True, sync_endpoint__isnull=False)
    }
    return JsonResponse(mapping)


@smart_lab_access_required
@require_GET
def dashboard(request):
    from NEMO.models import Tool

    sources = get_tool_sources()
    # One query for every tool's SmartLabTool row (keyword lists + usage_reference_source/real_id
    # for NEMO_smart_lab.status) instead of one per tool inside the loop below - the dashboard
    # renders every configured tool at once, so this is the difference between one query and N.
    slt_by_name = {slt.name: slt for slt in SmartLabTool.objects.filter(enabled=True).select_related("usage_reference_source")}
    # Each tool with a sync_endpoint may need its own on-demand fetch from a remote host (see
    # NEMO_smart_lab.remote_cache) - on a cold cache that's one SSH round trip per tool
    # (~1.5s measured against Oak), which serializing across every configured tool would badly
    # multiply into several seconds just to load the landing page. Fetching them concurrently
    # instead means the whole page only ever waits on the single slowest tool - the overview
    # status's own live "in use right now" check (NEMO_smart_lab.status) rides along in the same
    # worker per tool rather than adding a second serial pass afterward.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_named_summary_and_status, ((name, cfg, slt_by_name.get(name)) for name, cfg in sources.items())))

    categories = dict(Tool.objects.filter(name__in=sources).values_list("name", "_category"))

    groups = {}
    for name, summary, status in results:
        summary["slug"] = sources[name]["id"]
        summary["dashboard_status"] = status
        group = _tool_group_label(categories.get(name))
        groups.setdefault(group, []).append(summary)

    tool_groups = [
        {"category": group, "tools": sorted(tools, key=lambda t: t["name"])}
        for group, tools in sorted(groups.items(), key=lambda item: (item[0] == UNCATEGORIZED, item[0]))
    ]
    return render(request, "NEMO_smart_lab/dashboard.html", {"tool_groups": tool_groups})


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
        total_cycles, counted_runs, total_runs = (0, 0, 0) if summary.get("error") else total_cycles_run(cfg)
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


HISTORY_PAGE_SIZE_CHOICES = [25, 50, 100, 250]


@smart_lab_access_required
@require_GET
def tool_history(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")

    try:
        page = int(request.GET.get("page", 1))
    except ValueError:
        page = 1
    page = max(page, 1)

    try:
        page_size = int(request.GET.get("page_size", DEFAULT_HISTORY_LIMIT))
    except ValueError:
        page_size = DEFAULT_HISTORY_LIMIT
    if page_size not in HISTORY_PAGE_SIZE_CHOICES:
        page_size = DEFAULT_HISTORY_LIMIT

    # Both are metadata-only filters (run filename/foldername, never a run's own file content -
    # see readers._filter_run_entries) - only heater_log/mvd (the only kinds with a linear per-run
    # list at all) actually support them; every other kind just ignores them. Each is a list - the
    # filter form is a multi-select "tag" input (see tool_history.html/smart_lab_tags.js), so a
    # plain GET repeats the param once per tag (?recipe=A&recipe=B), not a single comma-joined one.
    supports_run_filters = cfg["kind"] in ("heater_log", "mvd")
    recipe_filter = [v.strip() for v in request.GET.getlist("recipe") if v.strip()]
    user_filter = [v.strip() for v in request.GET.getlist("user") if v.strip()]
    # Needed up front now (not just down by annotate_run_usage below) - real_id/usage_reference_source
    # are what let find_user_run_windows fall back to a bounded remote lookup when nothing local
    # matches a searched username (see that function's own docstring).
    slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
    # Resolved once here (not per-run) - see reservations.find_user_run_windows's own docstring
    # for why this is a single cheap local (or, as a fallback, one bounded remote) query rather
    # than something readers.py itself could do.
    user_windows = (
        find_user_run_windows(
            name,
            user_filter,
            real_id=slt.real_id if slt else None,
            api_source=slt.usage_reference_source if slt else None,
            remote_range=get_run_time_range(cfg),
        )
        if user_filter and supports_run_filters
        else None
    )
    recipe_choices = _recipe_name_choices(cfg) if supports_run_filters else []
    user_choices = list_tool_usernames(name) if supports_run_filters else []
    # Same metadata-only reasoning as recipe/user above - a run's own embedded start date (see
    # readers._run_start_timestamp), no fetch/parse needed. Deliberately date-only (not date+time):
    # "generally choosing only the day" is what the run history's own users actually want most of
    # the time, and a single <input type="date"> pair is simpler than four separate fields - the
    # underlying filter (readers._filter_run_entries) is date-inclusive on both ends either way, so
    # picking the SAME day for both start and end already covers "just this one day".
    start_date = _parse_date_param(request.GET.get("start_date"))
    end_date = _parse_date_param(request.GET.get("end_date"))
    # Re-attached to every pager/page-size link below so switching pages or the page size never
    # drops the active filter tags - built once here (already urlencoded) rather than reconstructed
    # by hand in the template for every single link.
    filter_query_params = (
        [("recipe", r) for r in recipe_filter]
        + [("user", u) for u in user_filter]
        + ([("start_date", start_date.isoformat())] if start_date else [])
        + ([("end_date", end_date.isoformat())] if end_date else [])
    )
    filter_query_string = ("&" + urlencode(filter_query_params)) if filter_query_params else ""

    history_kwargs = dict(
        recipe=recipe_filter, user_windows=user_windows, start_date=start_date, end_date=end_date
    )
    runs, total = get_tool_history(cfg, page=page, page_size=page_size, **history_kwargs)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    if page > total_pages:
        page = total_pages
        runs, total = get_tool_history(cfg, page=page, page_size=page_size, **history_kwargs)

    # total/total_pages come from the raw remote file listing, but a page's entries can still end
    # up empty after get_tool_history() silently skips any file that fails to parse (partial
    # upload, corrupt file, etc. - see _heater_log_history's `except ... continue`). That mismatch
    # is invisible until you land on the last page or one right after a run of bad files: the
    # pager still claims that page exists ("last") but it renders with zero runs. Back off one
    # page at a time until a non-empty page turns up (or we hit page 1) so "last" always lands
    # somewhere with actual content, capped to avoid unbounded re-fetching if a tool's history is
    # pathologically sparse.
    backoff_budget = 10
    while not runs and page > 1 and backoff_budget > 0:
        page -= 1
        total_pages = page
        runs, total = get_tool_history(cfg, page=page, page_size=page_size, **history_kwargs)
        backoff_budget -= 1

    # One lookup for the whole page's time range (not one per run) - see annotate_run_usage()'s
    # docstring for why: a single reservation covering several back-to-back runs is recognized as
    # covering all of them, instead of being independently re-discovered once per run. Reuses the
    # same `slt` already resolved above for user_windows.
    annotate_run_usage(runs, name, slt.real_id if slt else None, slt.usage_reference_source if slt else None)

    return render(
        request,
        "NEMO_smart_lab/tool_history.html",
        {
            "tool_name": name,
            "slug": tool_id,
            "latest_run_id": get_latest_run_id(cfg),
            "runs": runs,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "page_numbers": _page_numbers(page, total_pages),
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "page_size": page_size,
            "page_size_choices": HISTORY_PAGE_SIZE_CHOICES,
            "supports_run_filters": supports_run_filters,
            "recipe_filter": recipe_filter,
            "user_filter": user_filter,
            "recipe_choices": recipe_choices,
            "user_choices": user_choices,
            "filter_query_string": filter_query_string,
            "is_filtered": bool(recipe_filter or user_filter or start_date or end_date),
            "start_date": start_date,
            "end_date": end_date,
        },
    )


@smart_lab_access_required
@require_GET
def tool_chart(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None
    group_key = request.GET.get("group") or None
    username = request.GET.get("user") or None
    timestamp = request.GET.get("ts") or None
    # A series the user has already unchecked on the interactive uPlot legend before clicking
    # "Download as image" (see smart_lab_charts.js's download link handler) - left out of the PNG
    # too, rather than the download silently including channels the user explicitly turned off.
    hide = set(request.GET.getlist("hide")) or None
    png_bytes = render_chart_png(cfg, run_id, group_key, username, timestamp, hide)
    return HttpResponse(png_bytes, content_type="image/png")


@smart_lab_access_required
@require_GET
def tool_stream_chart(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    png_bytes = render_stream_chart_png(cfg)
    return HttpResponse(png_bytes, content_type="image/png")


@smart_lab_access_required
@require_GET
def tool_screenshot(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None
    path = get_run_screenshot(cfg, run_id)
    if not path:
        return HttpResponseNotFound("No screenshot available for this run")
    return FileResponse(open(path, "rb"), content_type="image/jpeg")


def _parse_float(value):
    try:
        return float(value) if value else None
    except ValueError:
        return None


@smart_lab_access_required
@require_GET
def tool_chart_data(request, tool_id):
    """JSON counterpart to tool_chart() (chart.png) - same data, consumed by
    static/NEMO_smart_lab/js/smart_lab_charts.js to draw an interactive chart instead of a static
    image. start/end (optional) request just that x-range - a zoom-triggered re-fetch for real,
    undecimated data at whatever's currently visible, rather than only ever re-scaling a fixed
    pre-downsampled buffer. `user`/`ts` (optional) are the run's already-known username/end
    timestamp (from tool_detail's own reservation/usage lookup and summary, passed through as
    query params rather than looked up again here) - appended to the chart title as "<recipe> -
    <username> - <timestamp>"."""
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None
    group_key = request.GET.get("group") or None
    start = _parse_float(request.GET.get("start"))
    end = _parse_float(request.GET.get("end"))
    username = request.GET.get("user") or None
    timestamp = request.GET.get("ts") or None
    return JsonResponse(get_chart_json(cfg, run_id, group_key, start, end, username, timestamp))


@smart_lab_access_required
@require_GET
def tool_stream_chart_data(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return JsonResponse(get_stream_chart_json(cfg))


@smart_lab_access_required
@require_GET
def tool_base_pressure_data(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return JsonResponse(get_base_pressure_chart_json(cfg, range_key=request.GET.get("range")))


@smart_lab_access_required
@require_GET
def tool_base_pressure_chart(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return HttpResponse(render_base_pressure_chart_png(cfg, range_key=request.GET.get("range")), content_type="image/png")


@smart_lab_access_required
@require_GET
def tool_base_pressure_csv(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    response = HttpResponse(render_base_pressure_csv(cfg, range_key=request.GET.get("range")), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{name}-base-pressure.csv"'
    return response


@smart_lab_access_required
@require_GET
def tool_continuous_pressure_data(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return JsonResponse(get_continuous_pressure_chart_json(cfg, range_key=request.GET.get("range")))


@smart_lab_access_required
@require_GET
def tool_data(request, tool_id):
    """Recipes and config files, both on this one page (each its own tab - see tool_data.html)
    instead of two separate pages a viewer has to navigate between - both are cheap, listing-only
    reads (no per-run scanning the way tool_maintenance_trends' own trends are), so unlike that
    page's own lazily-loaded Trends tab, both tabs here are just computed eagerly, together, in
    this one view - switching tabs is a pure client-side visibility toggle, no fetch involved."""
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    try:
        recipe_groups = _grouped_recipes(cfg)
        recipes_error = None
    except remote_sync.RemoteSyncError as e:
        # A transient remote-host hiccup (Oak unreachable, DNS blip, etc.) shouldn't crash this
        # page with a raw 500 - same reasoning as tool_detail's own "tool.error" handling.
        recipe_groups = []
        recipes_error = str(e)
    try:
        config_groups = _grouped_config_files(cfg)
        configs_error = None
    except remote_sync.RemoteSyncError as e:
        config_groups = []
        configs_error = str(e)
    try:
        # The exact file readers.py's own config-derived channel labels (heater/MFC names shown
        # on charts) actually read - shown as a "currently in use" badge so a viewer looking at
        # several similarly-named files (a live Setup.ini.txt next to an old "- Copy" one) can
        # tell which one is live. None (no badge shown) is a normal outcome, not an error - most
        # tools have no such lookup at all, or config_subdir isn't set.
        active_entry = find_active_config_file(cfg)
    except remote_sync.RemoteSyncError:
        active_entry = None
    return render(
        request,
        "NEMO_smart_lab/tool_data.html",
        {
            "tool_name": name,
            "slug": tool_id,
            # Which of this page's two tabs (see tool_data.html's own tab strip) should be active
            # on load - "configs" only when explicitly requested via ?tab=configs (e.g. a config
            # file's own "back to config files" link, or tool_recipe_toggle_pin/tool_recipes'/
            # tool_configs' own redirects), "recipes" (the default) otherwise.
            "active_tab": "configs" if request.GET.get("tab") == "configs" else "recipes",
            "recipe_groups": recipe_groups,
            "recipes_error": recipes_error,
            "latest_run_id": None if recipes_error else get_latest_run_id(cfg),
            "config_groups": config_groups,
            "configs_error": configs_error,
            "active_config_file_id": active_entry["id"] if active_entry else None,
        },
    )


@smart_lab_access_required
@require_GET
def tool_recipes(request, tool_id):
    """The Recipes tab - now part of the combined tool_data page (see its own docstring) rather
    than a separate page. Kept as a thin redirect (?tab=recipes selects that tab) so an old
    bookmarked/shared link still lands somewhere sensible."""
    name, _cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return redirect(f"{reverse('smart_lab_tool_data', args=[tool_id])}?tab=recipes")


@smart_lab_access_required
@require_GET
def tool_recipe_duplicates(request, tool_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    try:
        duplicate_groups = find_duplicate_recipes(cfg)
        error = None
    except remote_sync.RemoteSyncError as e:
        duplicate_groups = []
        error = str(e)
    return render(
        request,
        "NEMO_smart_lab/recipe_duplicates.html",
        {"tool_name": name, "slug": tool_id, "duplicate_groups": duplicate_groups, "error": error},
    )


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


@smart_lab_access_required
@require_POST
def tool_recipe_toggle_pin(request, tool_id):
    """Toggles one recipe folder's membership in this tool's pinned_recipe_categories - the small
    pin icon next to each folder heading on the Recipes page. This writes to this plugin's own
    local SmartLabTool row only (never to Oak or to prod NEMO), so it's fine for any user who can
    already access Smart Lab to do, same as every other view here."""
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    category = request.POST.get("category")
    if category:
        slt = SmartLabTool.objects.filter(name=name).first()
        if slt is not None:
            pinned = list(slt.pinned_recipe_categories or [])
            if category in pinned:
                pinned.remove(category)
            else:
                pinned.append(category)
            slt.pinned_recipe_categories = pinned
            slt.save(update_fields=["pinned_recipe_categories"])
            invalidate_tool_sources_cache()
    return redirect(f"{reverse('smart_lab_tool_data', args=[tool_id])}?tab=recipes")


@smart_lab_access_required
@require_GET
def tool_recipe_detail(request, tool_id, recipe_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    recipe = get_recipe_detail(cfg, recipe_id)
    if recipe is None:
        return HttpResponseNotFound("Unknown recipe")
    return render(
        request,
        "NEMO_smart_lab/recipe_detail.html",
        {
            "tool_name": name,
            "slug": tool_id,
            "recipe": recipe,
            "recipe_run_count": count_runs_for_recipe(cfg, _strip_txt_suffixes(recipe["name"])),
            "is_base_pressure_recipe": _strip_txt_suffixes(recipe["name"]).lower() in base_pressure_recipe_targets(cfg),
        },
    )


@smart_lab_access_required
@require_GET
def tool_configs(request, tool_id):
    """The Config files tab - now part of the combined tool_data page (see its own docstring)
    rather than a separate page. Kept as a thin redirect (?tab=configs selects that tab) so an old
    bookmarked/shared link still lands somewhere sensible."""
    name, _cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return redirect(f"{reverse('smart_lab_tool_data', args=[tool_id])}?tab=configs")


@smart_lab_access_required
@require_GET
def tool_config_detail(request, tool_id, file_id):
    name, cfg = _resolve(tool_id)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    config_file = get_config_file_detail(cfg, file_id)
    if config_file is None:
        return HttpResponseNotFound("Unknown configuration file")
    try:
        active_entry = find_active_config_file(cfg)
    except remote_sync.RemoteSyncError:
        active_entry = None
    return render(
        request,
        "NEMO_smart_lab/config_detail.html",
        {
            "tool_name": name,
            "slug": tool_id,
            "config_file": config_file,
            "is_active_config_file": bool(active_entry) and active_entry["id"] == file_id,
        },
    )
