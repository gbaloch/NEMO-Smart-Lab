"""Total ALD cycles run on a tool, estimated from each run's recipe."""

from NEMO_smart_lab.readers import count_runs_by_recipe_name
from NEMO_smart_lab.recipes.listing import find_recipe_by_name
from NEMO_smart_lab.recipes.parsing import _prewarm_recipe_files, get_recipe_detail


# Bounds how many *distinct* recipe names total_cycles_run resolves synchronously within one
# request - a tool's run history can embed dozens of distinct recipe names (including ones long
# since renamed/deleted, which resolve to None quickly with no fetch at all), but a genuinely
# unseen one needs a real fetch (~1.5s round trip when remote and not yet cached - the same per-
# file cost already documented throughout this module/remote_cache). Processed most-run-first (see
# total_cycles_run), so the names covering the most actual runs get counted first regardless of
# where this bound lands - a repeat page load picks up more as remote_cache's own listing/content
# caching warms.
_MAX_DISTINCT_RECIPES_PER_REQUEST = 40


def total_cycles_run(cfg):
    """Best-effort total ALD cycle count ever run on this tool: sum, across every run in its whole
    history, of that run's own recipe's CURRENT cycle count (from the recipe's own "goto" step,
    see _summarize_steps) - the real metric fabs use for reactor/seal-wear PM scheduling, since it
    reflects actual usage intensity, not just calendar time.

    Deliberately approximate, not exact, in two ways - both surfaced via the return value rather
    than hidden: (1) it uses each matched recipe's CURRENT cycle count, not necessarily what it
    was at the time each historical run actually happened - a recipe edited since to change its
    cycle count misattributes cycles for runs before that edit; (2) a run whose recipe no longer
    exists, or is ambiguous (see find_recipe_by_name), or has no "goto" step at all, is excluded
    rather than guessed at, and only the `_MAX_DISTINCT_RECIPES_PER_REQUEST` distinct recipe names
    covering the most runs are even attempted per request (see that constant).

    Returns (total_cycles, counted_runs, total_runs) - `counted_runs`/`total_runs` let a caller
    show "based on N of M runs" so how much of the history was actually covered stays visible."""
    counts_by_name = count_runs_by_recipe_name(cfg)
    total_runs = sum(counts_by_name.values())
    if not total_runs:
        return 0, 0, 0

    most_run_first = sorted(counts_by_name.items(), key=lambda item: item[1], reverse=True)
    candidates = [
        (recipe, run_count)
        for recipe_name, run_count in most_run_first[:_MAX_DISTINCT_RECIPES_PER_REQUEST]
        for recipe in [find_recipe_by_name(cfg, recipe_name)]
        if recipe is not None
    ]

    # See _prewarm_recipe_files - this runs on EVERY heater_log/mvd tool overview page load (see
    # views.tool_detail), so fetching up to _MAX_DISTINCT_RECIPES_PER_REQUEST (40) recipe files one
    # at a time on a cold cache was a major, easy-to-miss chunk of "the tool overview is slow".
    _prewarm_recipe_files(cfg, [recipe for recipe, _run_count in candidates])

    total_cycles = 0
    counted_runs = 0
    for recipe, run_count in candidates:
        detail = get_recipe_detail(cfg, recipe["id"])
        if not detail or detail.get("cycles") is None:
            continue
        total_cycles += detail["cycles"] * run_count
        counted_runs += run_count
    return total_cycles, counted_runs, total_runs
