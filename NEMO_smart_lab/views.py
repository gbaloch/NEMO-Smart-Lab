import math

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.utils.text import slugify
from django.views.decorators.http import require_GET

from NEMO_smart_lab.charts import get_chart_json, get_stream_chart_json, render_chart_png, render_stream_chart_png
from NEMO_smart_lab.config import get_tool_sources
from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.readers import DEFAULT_HISTORY_LIMIT, get_run_screenshot, get_tool_history, get_tool_summary
from NEMO_smart_lab.reservations import get_run_usage


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


@login_required
@require_GET
def dashboard(request):
    tools = []
    for name, cfg in get_tool_sources().items():
        summary = get_tool_summary(name, cfg)
        summary["slug"] = slugify(name)
        tools.append(summary)
    tools.sort(key=lambda t: t["name"])
    return render(request, "NEMO_smart_lab/dashboard.html", {"tools": tools})


@login_required
@require_GET
def tool_detail(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None
    summary = get_tool_summary(name, cfg, run_id)
    summary["slug"] = tool_slug
    is_latest = run_id is None

    slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
    run_usage = (
        get_run_usage(name, slt.real_id if slt else None, summary, slt.usage_reference_source if slt else None)
        if not summary.get("error")
        else []
    )
    has_screenshot = not summary.get("error") and get_run_screenshot(cfg, run_id) is not None

    return render(
        request,
        "NEMO_smart_lab/tool_detail.html",
        {"tool": summary, "is_latest": is_latest, "run_usage": run_usage, "has_screenshot": has_screenshot},
    )


@login_required
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
    page_size = DEFAULT_HISTORY_LIMIT

    runs, total = get_tool_history(cfg, page=page, page_size=page_size)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    if page > total_pages:
        page = total_pages
        runs, total = get_tool_history(cfg, page=page, page_size=page_size)

    slt = SmartLabTool.objects.filter(name=name).select_related("usage_reference_source").first()
    for run in runs:
        # Only this one page's worth of rows (page_size, default 25) ever gets a usage lookup -
        # get_tool_history() already only parses that page's run files, so this stays cheap.
        usage = get_run_usage(
            name,
            slt.real_id if slt else None,
            {"last_update": run["timestamp"], "run_duration_s": run["duration_s"]},
            slt.usage_reference_source if slt else None,
        )
        run["usage_users"] = [entry["user"] for entry in usage]

    return render(
        request,
        "NEMO_smart_lab/tool_history.html",
        {
            "tool_name": name,
            "slug": tool_slug,
            "runs": runs,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "page_numbers": _page_numbers(page, total_pages),
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    )


@login_required
@require_GET
def tool_chart(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None
    png_bytes = render_chart_png(cfg, run_id)
    return HttpResponse(png_bytes, content_type="image/png")


@login_required
@require_GET
def tool_stream_chart(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    png_bytes = render_stream_chart_png(cfg)
    return HttpResponse(png_bytes, content_type="image/png")


@login_required
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


@login_required
@require_GET
def tool_chart_data(request, tool_slug):
    """JSON counterpart to tool_chart() (chart.png) - same data, consumed by
    static/NEMO_smart_lab/js/smart_lab_charts.js to draw an interactive Chart.js canvas instead of
    a static image."""
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    run_id = request.GET.get("run") or None
    return JsonResponse(get_chart_json(cfg, run_id))


@login_required
@require_GET
def tool_stream_chart_data(request, tool_slug):
    name, cfg = _resolve(tool_slug)
    if not name:
        return HttpResponseNotFound("Unknown Smart Lab tool")
    return JsonResponse(get_stream_chart_json(cfg))
