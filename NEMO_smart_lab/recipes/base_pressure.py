"""Which recipes count as a tool's standby/base-pressure recipe, and auto-detecting candidates."""

from NEMO_smart_lab.recipes.listing import _strip_txt_suffixes, find_recipe_by_name, list_recipes
from NEMO_smart_lab.recipes.parsing import _prewarm_recipe_files, get_recipe_detail


def base_pressure_recipe_targets(cfg):
    """{normalized name, ...} for this tool's configured base_pressure_recipe_names (comma-
    separated, ".txt"-stripped and lowercased the same way find_recipe_by_name/_recipe_from_run_id
    already normalize a recipe name for comparison) - lets a recipe's own detail page cheaply check
    "is this one of the tool's designated standby/base-pressure recipes" without re-parsing the raw
    config string itself. Empty set if nothing is configured."""
    raw = cfg.get("base_pressure_recipe_names") or ""
    return {_strip_txt_suffixes(name.strip()).lower() for name in raw.split(",") if name.strip()}


def get_base_pressure_recipe_links(cfg):
    """[{"name": <configured name>, "recipe": <matching list_recipes() entry, or None>}, ...] for
    this tool's configured base_pressure_recipe_names (see SmartLabTool.base_pressure_recipe_names
    and suggest_base_pressure_recipes) - lets the base-pressure chart's own description link
    straight to each recipe's detail page (via find_recipe_by_name's same matching/ambiguity rules)
    instead of just naming it as plain text. "recipe" is None (not a guess) for a name that isn't
    found or is ambiguous - see find_recipe_by_name's own docstring for exactly when that happens.
    [] if nothing is configured."""
    raw = cfg.get("base_pressure_recipe_names") or ""
    names = [name.strip() for name in raw.split(",") if name.strip()]
    return [{"name": name, "recipe": find_recipe_by_name(cfg, name)} for name in names]


def suggest_base_pressure_recipes(cfg, standby_keywords, exclude_keywords=None):
    """Candidate recipe names for SmartLabTool.base_pressure_recipe_names, found rather than
    guessed: a recipe qualifies only if its own name looks like a standby recipe (matched against
    `standby_keywords` - the same comma-separated list NEMO_smart_lab.status already matches
    against for the dashboard's "Ready - standby" state) AND its last real step is a "wait" - the
    long settle-then-measure step this whole feature depends on (a user described adding exactly
    this, a ~30 second wait, to the end of their standby recipe). Several real standby variants on
    the same tool can legitimately both qualify (confirmed live), which is exactly why
    base_pressure_recipe_names takes a list rather than one recipe.

    `exclude_keywords` (typically the tool's own valve_clean_recipe_keywords) drops any recipe
    whose name ALSO looks like a valve-clean pass, even if it too ends in a wait step - confirmed
    live that a "... - Valve Clean" standby variant can vent the chamber partway through (one real
    reading spiked to 163 Torr against an otherwise ~0.1-0.2 Torr baseline), which is exactly the
    "different standby-ish variants have genuinely different baseline pressure" failure mode this
    whole feature is designed to avoid mixing together.

    Returns the exact, de-duplicated recipe names (".txt" stripped, ready to paste into
    base_pressure_recipe_names) - never writes anything itself; the admin action that calls this
    decides whether/how to save the result. Only ever suggests from recipes that still exist in
    the tree today - a recipe a historical run actually used but that's since been renamed or
    deleted on the tool PC won't be found this way (confirmed live: this can genuinely happen), so
    an admin may still need to add such a name by hand for a tool's full run history to be covered."""
    keywords = [k.strip().lower() for k in (standby_keywords or "").split(",") if k.strip()]
    if not keywords:
        return []
    excluded = [k.strip().lower() for k in (exclude_keywords or "").split(",") if k.strip()]
    keyword_matches = [
        recipe
        for recipe in list_recipes(cfg)
        if any(keyword in recipe["name"].lower() for keyword in keywords)
        and not any(keyword in recipe["name"].lower() for keyword in excluded)
    ]
    _prewarm_recipe_files(cfg, keyword_matches)

    seen = set()
    candidates = []
    for recipe in keyword_matches:
        detail = get_recipe_detail(cfg, recipe["id"])
        if not detail or not detail["steps"]:
            continue
        if detail["steps"][-1]["command"].strip().lower() != "wait":
            continue
        name = recipe["name"]
        while name.lower().endswith(".txt"):
            name = name[: -len(".txt")]
        if name not in seen:
            seen.add(name)
            candidates.append(name)
    return candidates
