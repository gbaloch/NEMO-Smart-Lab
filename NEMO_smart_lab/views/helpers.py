"""Small helpers shared by several Smart Lab views (tool lookup, query-param parsing, grouping for templates)."""

from datetime import date

from django.urls import reverse
from django.utils.html import format_html, format_html_join

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.config import get_tool_sources
from NEMO_smart_lab.configs import list_config_files
from NEMO_smart_lab.readers import count_runs_by_recipe_name, get_tool_summary
from NEMO_smart_lab.recipes import (
    _strip_txt_suffixes,
    base_pressure_recipe_targets,
    get_base_pressure_recipe_links,
    list_recipes,
)
from NEMO_smart_lab.reservations import get_run_usage
from NEMO_smart_lab.status import get_tool_status


UNCATEGORIZED = "Uncategorized"


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


def _parse_float(value):
    try:
        return float(value) if value else None
    except ValueError:
        return None
