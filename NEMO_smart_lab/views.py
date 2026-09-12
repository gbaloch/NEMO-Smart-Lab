import math
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from NEMO_smart_lab.charts import (
    get_base_pressure_chart_json,
    get_chart_json,
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
    get_chart_group_list,
    get_latest_run_id,
    get_recent_faulty_runs,
    get_recent_runs,
    get_run_page_number,
    get_run_screenshot,
    get_tool_history,
    get_tool_summary,
)
from NEMO_smart_lab.recipes import get_recently_updated_recipes, get_recipe_detail, list_recipes
from NEMO_smart_lab.reservations import annotate_run_usage, get_run_usage
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


def _tool_group_label(category):
    """NEMO's own Tool.category convention is a "/"-delimited "Building/Sub-category" string
    (e.g. "Allen/Atomic Layer Deposition") - group the Smart Lab dashboard by just the last
    segment, since that's the meaningful grouping for staff (which process family a tool belongs
    to), not which building it's physically in."""
    if not category:
        return UNCATEGORIZED
    return category.rsplit("/", 1)[-1].strip() or UNCATEGORIZED


def _resolve(tool_slug):
    """Looks up a tool by its slug against the *current* set of configured tools, so an admin
    edit (rename, enable/disable, new tool) takes effect on the next request rather than
    needing a server restart. Returns (name, cfg) or (None, None)."""
    for name, cfg in get_tool_sources().items():
        if slugify(name) == tool_slug:
            return name, cfg
    return None, None


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


def _named_summary_and_status(item):
    """Computed together, in the same worker thread, so the dashboard's status feature (which
    needs a live "is it in use right now" check - see NEMO_smart_lab.status) doesn't add a second,
    serial round of per-tool work after the summaries are already fetched concurrently below."""
    name, cfg, slt = item
    summary = get_tool_summary(name, cfg)
    return name, summary, get_tool_status(name, summary, slt)


def _grouped_recipes(cfg):
    """list_recipes() is already sorted by (category, name) - group it into
    [{"category", "recipes", "pinned"}, ...] for recipe_list.html, same spirit as dashboard()'s
    _tool_group_label grouping. "pinned" drives both the pin icon's current state and the toggle
    form's action (pin vs. unpin) for that folder."""
    pinned = cfg.get("pinned_recipe_categories") or []
    groups = []
    for recipe in list_recipes(cfg):
        if not groups or groups[-1]["category"] != recipe["category"]:
            groups.append({"category": recipe["category"], "recipes": [], "pinned": recipe["category"] in pinned})
        groups[-1]["recipes"].append(recipe)
    return groups


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
        summary["slug"] = slugify(name)
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
def tool_detail(request, tool_slug):
    name, cfg = _resolve(tool_slug)
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
        summary["slug"] = tool_slug
        recent_faulty_runs = [] if summary.get("error") else get_recent_faulty_runs(cfg)
        recent_runs = [] if summary.get("error") else get_recent_runs(cfg)
        if recent_runs:
            slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
            # One lookup covering every one of these runs at once (see annotate_run_usage), not
            # one per row - adds "usage_period" ({"user", "username", "source", ...} or None) to
            # each, the same real usage-lookup the full-detail page's own "Tool usage" panel uses.
            annotate_run_usage(recent_runs, name, slt.real_id if slt else None, slt.usage_reference_source if slt else None)
        return render(
            request,
            "NEMO_smart_lab/tool_detail.html",
            {
                "tool": summary,
                "show_full_detail": False,
                "base_pressure_recipe_names": cfg.get("base_pressure_recipe_names"),
                "show_base_pressure_history": bool(cfg.get("base_pressure_recipe_names")),
                "recent_faulty_runs": recent_faulty_runs,
                "recent_runs": recent_runs,
                "recent_recipes": get_recently_updated_recipes(cfg),
            },
        )

    summary = get_tool_summary(name, cfg, run_id)
    summary["slug"] = tool_slug
    if supports_overview:
        is_latest = run_id is not None and run_id == get_latest_run_id(cfg)
    else:
        is_latest = run_id is None

    slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
    run_usage = (
        get_run_usage(name, slt.real_id if slt else None, summary, slt.usage_reference_source if slt else None)
        if not summary.get("error")
        else []
    )
    has_screenshot = not summary.get("error") and get_run_screenshot(cfg, run_id) is not None
    chart_groups = [] if summary.get("error") else get_chart_group_list(cfg, run_id)
    # The run's actual user, if known - passed through to the chart endpoints (as a query param,
    # not a fresh lookup - see tool_chart_data/tool_chart) so a chart's title can read "<recipe> -
    # <username>", the same usage_event-preferred priority annotate_run_usage() already uses for
    # its "primary" period.
    run_username = next((e["username"] for e in run_usage if e["source"] == "usage_event" and e.get("username")), None) or next(
        (e["username"] for e in run_usage if e.get("username")), None
    )
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

    return render(
        request,
        "NEMO_smart_lab/tool_detail.html",
        {
            "tool": summary,
            "show_full_detail": True,
            "is_latest": is_latest,
            "supports_overview": supports_overview,
            "run_history_page": run_history_page,
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
def tool_history(request, tool_slug):
    name, cfg = _resolve(tool_slug)
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

    runs, total = get_tool_history(cfg, page=page, page_size=page_size)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    if page > total_pages:
        page = total_pages
        runs, total = get_tool_history(cfg, page=page, page_size=page_size)

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
        runs, total = get_tool_history(cfg, page=page, page_size=page_size)
        backoff_budget -= 1

    slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
    # One lookup for the whole page's time range (not one per row) - see annotate_run_usage()'s
    # docstring for why: a single reservation covering several back-to-back runs is recognized as
    # covering all of them, instead of being independently re-discovered once per run.
    annotate_run_usage(runs, name, slt.real_id if slt else None, slt.usage_reference_source if slt else None)

    return render(
        request,
        "NEMO_smart_lab/tool_history.html",
        {
            "tool_name": name,
            "slug": tool_slug,
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
        },
    )


@smart_lab_access_required
@require_GET
def tool_chart(request, tool_slug):
    name, cfg = _resolve(tool_slug)
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
def tool_stream_chart(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    png_bytes = render_stream_chart_png(cfg)
    return HttpResponse(png_bytes, content_type="image/png")


@smart_lab_access_required
@require_GET
def tool_screenshot(request, tool_slug):
    name, cfg = _resolve(tool_slug)
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
def tool_chart_data(request, tool_slug):
    """JSON counterpart to tool_chart() (chart.png) - same data, consumed by
    static/NEMO_smart_lab/js/smart_lab_charts.js to draw an interactive chart instead of a static
    image. start/end (optional) request just that x-range - a zoom-triggered re-fetch for real,
    undecimated data at whatever's currently visible, rather than only ever re-scaling a fixed
    pre-downsampled buffer. `user`/`ts` (optional) are the run's already-known username/end
    timestamp (from tool_detail's own reservation/usage lookup and summary, passed through as
    query params rather than looked up again here) - appended to the chart title as "<recipe> -
    <username> - <timestamp>"."""
    name, cfg = _resolve(tool_slug)
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
def tool_stream_chart_data(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return JsonResponse(get_stream_chart_json(cfg))


@smart_lab_access_required
@require_GET
def tool_base_pressure_data(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return JsonResponse(get_base_pressure_chart_json(cfg, range_key=request.GET.get("range")))


@smart_lab_access_required
@require_GET
def tool_base_pressure_chart(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return HttpResponse(render_base_pressure_chart_png(cfg, range_key=request.GET.get("range")), content_type="image/png")


@smart_lab_access_required
@require_GET
def tool_base_pressure_csv(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    response = HttpResponse(render_base_pressure_csv(cfg, range_key=request.GET.get("range")), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{tool_slug}-base-pressure.csv"'
    return response


@smart_lab_access_required
@require_GET
def tool_recipes(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    try:
        recipe_groups = _grouped_recipes(cfg)
        error = None
    except remote_sync.RemoteSyncError as e:
        # A transient remote-host hiccup (Oak unreachable, DNS blip, etc.) shouldn't crash this
        # page with a raw 500 - same reasoning as tool_detail's own "tool.error" handling.
        recipe_groups = []
        error = str(e)
    return render(
        request,
        "NEMO_smart_lab/recipe_list.html",
        {
            "tool_name": name,
            "slug": tool_slug,
            "recipe_groups": recipe_groups,
            "error": error,
            "latest_run_id": None if error else get_latest_run_id(cfg),
        },
    )


@smart_lab_access_required
@require_POST
def tool_recipe_toggle_pin(request, tool_slug):
    """Toggles one recipe folder's membership in this tool's pinned_recipe_categories - the small
    pin icon next to each folder heading on the Recipes page. This writes to this plugin's own
    local SmartLabTool row only (never to Oak or to prod NEMO), so it's fine for any user who can
    already access Smart Lab to do, same as every other view here."""
    name, cfg = _resolve(tool_slug)
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
    return redirect("smart_lab_tool_recipes", tool_slug)


@smart_lab_access_required
@require_GET
def tool_recipe_detail(request, tool_slug, recipe_id):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    recipe = get_recipe_detail(cfg, recipe_id)
    if recipe is None:
        return HttpResponseNotFound("Unknown recipe")
    return render(
        request,
        "NEMO_smart_lab/recipe_detail.html",
        {"tool_name": name, "slug": tool_slug, "recipe": recipe},
    )
