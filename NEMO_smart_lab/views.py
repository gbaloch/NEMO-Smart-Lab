import math

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.utils.text import slugify
from django.views.decorators.http import require_GET

from NEMO_smart_lab.charts import render_chart_png, render_stream_chart_png
from NEMO_smart_lab.config import get_tool_sources
from NEMO_smart_lab.readers import DEFAULT_HISTORY_LIMIT, get_tool_history, get_tool_summary


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
    return render(request, "NEMO_smart_lab/tool_detail.html", {"tool": summary, "is_latest": is_latest})


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
