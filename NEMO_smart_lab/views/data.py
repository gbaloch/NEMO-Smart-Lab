"""A tool's Data page: recipes and configuration files."""

from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.config import invalidate_tool_sources_cache
from NEMO_smart_lab.configs import find_active_config_file, get_config_file_detail
from NEMO_smart_lab.models import SmartLabTool
from NEMO_smart_lab.readers import count_runs_for_recipe, get_latest_run_id
from NEMO_smart_lab.recipes import (
    _strip_txt_suffixes,
    base_pressure_recipe_targets,
    find_duplicate_recipes,
    get_recipe_detail,
)
from NEMO_smart_lab.views.access import smart_lab_access_required
from NEMO_smart_lab.views.helpers import _grouped_config_files, _grouped_recipes, _resolve


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
