"""A tool's paginated, filterable run history page."""

from urllib.parse import urlencode
import math

from django.http import HttpResponseNotFound
from django.shortcuts import render
from django.views.decorators.http import require_GET

from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.readers import (
    DEFAULT_HISTORY_LIMIT,
    get_latest_run_id,
    get_run_time_range,
    get_tool_history,
)
from NEMO_smart_lab.reservations import annotate_run_usage, find_user_run_windows, list_tool_usernames
from NEMO_smart_lab.views.access import smart_lab_access_required
from NEMO_smart_lab.views.helpers import _page_numbers, _parse_date_param, _recipe_name_choices, _resolve


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
