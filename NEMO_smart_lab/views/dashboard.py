"""The Smart Lab landing page and the staging machine's sync-map endpoint."""

from concurrent.futures import ThreadPoolExecutor

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from NEMO_smart_lab.config import get_tool_sources
from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.views.access import smart_lab_access_required, staging_api_key_required
from NEMO_smart_lab.views.helpers import UNCATEGORIZED, _named_summary_and_status, _tool_group_label


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
