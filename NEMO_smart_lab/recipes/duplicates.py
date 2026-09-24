"""Finding recipes whose content is identical."""

import hashlib

from NEMO_smart_lab.recipes.listing import _category_sort_priority, list_recipes
from NEMO_smart_lab.recipes.parsing import _prewarm_recipe_files, get_recipe_detail


def _recipe_content_fingerprint(steps):
    """A hash of a recipe's own *parsed* step content (command/channel/value/unit per step) -
    deliberately not the raw file bytes, so two copies that differ only in trivial formatting
    noise (a stray blank line, CRLF vs LF, trailing whitespace - see _parse_steps' own tolerance
    for ragged lines) still compare equal, while two recipes with genuinely different programs
    never coincidentally collide."""
    normalized = tuple((s["command"].strip().lower(), s["channel"].strip(), s["value"].strip(), s["unit"].strip()) for s in steps)
    return hashlib.sha1(repr(normalized).encode()).hexdigest()[:16]


def find_duplicate_recipes(cfg):
    """Groups of recipes on this tool that are identical in their own step *content* (not just
    name) - confirmed live that the exact same recipe routinely gets copied verbatim into several
    different per-user folders on Oak, not a rare edge case (see find_recipe_by_name's own
    docstring). Fetches and parses every recipe's full content once (same bounded, real cost as
    the "auto-detect standby recipes" admin action already pays for the same reason - recipe files
    are individually small, and a tool's whole recipe count is realistically dozens to ~100, not
    thousands).

    Returns [{"content_hash", "recipes": [...]}, ...] - only groups with 2 or more recipes (a
    recipe with no duplicate isn't included at all), largest group first, each group's own recipes
    sorted the same way list_recipes always does (canonical folders first). [] if this tool has no
    recipe_subdir configured, or nothing parses, or every recipe is unique."""
    all_recipes = list_recipes(cfg)
    _prewarm_recipe_files(cfg, all_recipes)

    by_hash = {}
    for recipe in all_recipes:
        detail = get_recipe_detail(cfg, recipe["id"])
        if not detail or not detail["steps"]:
            continue
        content_hash = _recipe_content_fingerprint(detail["steps"])
        by_hash.setdefault(content_hash, []).append(recipe)

    pinned = cfg.get("pinned_recipe_categories") or []
    groups = []
    for content_hash, recipes in by_hash.items():
        if len(recipes) < 2:
            continue
        recipes.sort(key=lambda r: (_category_sort_priority(r["category"], pinned), r["category"].lower(), r["name"].lower()))
        groups.append({"content_hash": content_hash, "recipes": recipes})
    groups.sort(key=lambda g: (-len(g["recipes"]), g["recipes"][0]["name"].lower()))
    return groups
