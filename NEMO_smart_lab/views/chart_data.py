"""Chart, image, CSV and screenshot endpoints for a tool."""

from django.http import FileResponse, HttpResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.http import require_GET

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
from NEMO_smart_lab.readers import get_run_screenshot
from NEMO_smart_lab.views.access import smart_lab_access_required
from NEMO_smart_lab.views.helpers import _parse_float, _resolve


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
    # "Download as image" (see js/charts's download link handler) - left out of the PNG
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


@smart_lab_access_required
@require_GET
def tool_chart_data(request, tool_id):
    """JSON counterpart to tool_chart() (chart.png) - same data, consumed by
    static/NEMO_smart_lab/js/charts/ to draw an interactive chart instead of a static
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
