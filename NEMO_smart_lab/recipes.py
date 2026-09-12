"""
Read-only browsing of a tool's actual recipe files on its RemoteSyncEndpoint (e.g. Oak) - a
separate concern from NEMO_smart_lab.readers (which only ever reads *run* data - what already
happened), layered on the same NEMO_smart_lab.remote_cache primitives readers.py uses.

Recipe folders are laid out inconsistently per tool (a mix of a "standard recipes" folder under
whatever name a given tool happens to use, and per-user folders, nested arbitrarily deep) - rather
than guess at a "standard vs personal" split, this exposes the whole tree, grouped by top-level
folder name, and lets the UI decide what to do with that.

Recipe files themselves are small, tab-delimited, CRLF-terminated step programs (confirmed live
against real Oak data for all six tools), e.g.:
    flow\t0\t20\r\n
    heater\t17\t150\t\r\n
    goto\t11\t100\tcycles\r\n
Not every line has all four fields (some are blank/ragged) - _parse_steps tolerates that. Text
encoding is Windows Latin-1 (a "°C" unit is byte 0xB0, not valid UTF-8) - same reason
readers.FILE_ENCODING already exists; reused here rather than re-guessing an encoding.

Nothing in this module ever writes anywhere - see this module's and remote_cache's docstrings for
why (recipes drive physical hardware; editing is a deliberately deferred future phase, not this
one).
"""

import hashlib
import re

from django.utils import timezone

from NEMO_smart_lab import remote_cache
from NEMO_smart_lab.readers import FILE_ENCODING

RECIPE_TREE_TTL = remote_cache.RECIPE_TREE_TTL
RECIPE_CONTENT_TTL = remote_cache.RECIPE_CONTENT_TTL


def _recipe_id(relpath):
    # Recipe relative paths routinely contain spaces, "%", "#", etc. (confirmed live) - hashing
    # sidesteps any URL-encoding edge case entirely, same trick remote_cache._cache_key and
    # reservations._cache_key already use for awkward strings.
    return hashlib.sha1(relpath.encode()).hexdigest()[:16]


_WORDS_RE = re.compile(r"[a-z0-9]+")


def _category_sort_priority(category, pinned=()):
    """A folder an admin/user has explicitly pinned (SmartLabTool.pinned_recipe_categories,
    toggled from the small pin icon next to each folder heading on the Recipes page) sorts first
    of all - even ahead of "(root)", since pinning is a deliberate "put this at the very top
    regardless" action. Otherwise, "(root)" and the tool's actual shared recipe folders sort
    next, in this order: STANDARD, Maintenance, Production, Process. Only a folder that IS (once
    normalized) exactly one of these bare names qualifies - a *substring* match alone isn't
    enough - confirmed live a tool can have both a real "STANDARD" folder and an unrelated,
    per-project "Digilens Standard" folder, and only the former is the one meant to sort first;
    the latter is exactly as "someone's own folder" as any other, and sorts with the rest."""
    if category in pinned:
        return -1
    if category == "(root)":
        return 0
    normalized = " ".join(_WORDS_RE.findall(category.lower()))
    if normalized in ("standard", "standard recipe", "standard recipes"):
        return 1
    if normalized in ("maintenance",):
        return 2
    if normalized in ("production", "production recipe", "production recipes"):
        return 3
    if normalized in ("process", "process recipe", "process recipes"):
        return 4
    return 5


def list_recipes(cfg):
    """[{"id", "relpath", "name", "category", "mtime", "size"}, ...] for every recipe *file* (not
    folder) under this tool's configured recipe_subdir, sorted by (category, name). Returns []
    if this tool has no recipe_subdir/remote_tool configured (the feature is opt-in per tool -
    see SmartLabTool.recipe_subdir)."""
    tool = cfg.get("remote_tool")
    recipe_subdir = cfg.get("recipe_subdir")
    if not tool or not recipe_subdir:
        return []

    root = f"{tool.remote_subdir_or_default}/{recipe_subdir}"
    entries = remote_cache.list_remote_tree(tool.sync_endpoint, root, ttl=RECIPE_TREE_TTL)
    pinned = cfg.get("pinned_recipe_categories") or []

    recipes = []
    for relpath, mtime, size, is_dir in entries:
        if is_dir:
            continue
        category = relpath.split("/", 1)[0] if "/" in relpath else "(root)"
        recipes.append(
            {
                "id": _recipe_id(relpath),
                "relpath": relpath,
                "name": relpath.rsplit("/", 1)[-1],
                "category": category,
                "mtime": timezone.make_aware(mtime) if timezone.is_naive(mtime) else mtime,
                "size": size,
            }
        )
    recipes.sort(key=lambda r: (_category_sort_priority(r["category"], pinned), r["category"].lower(), r["name"].lower()))
    return recipes


def find_recipe(cfg, recipe_id):
    return next((r for r in list_recipes(cfg) if r["id"] == recipe_id), None)


def get_recently_updated_recipes(cfg, limit=5):
    """The `limit` most recently-modified recipes (see list_recipes' own "mtime") - shown on the
    tool detail overview page, side by side with "Last run", as a quick "what's someone been
    editing on this tool lately" signal a user wouldn't otherwise notice without digging through
    the full recipe browser."""
    return sorted(list_recipes(cfg), key=lambda r: r["mtime"], reverse=True)[:limit]


def _heater_channel_label(channel, channel_labels, channel_offset):
    """Resolves a recipe "heater" line's channel number to this tool's curated display name,
    trying both key shapes SmartLabToolChannel.channel_key can be stored in (see
    SmartLabTool.recipe_channel_offset's docstring for how this was verified per tool):

    - mvd-kind tools (mvd/fiji5): channel_labels is keyed by the bare recipe channel number
      itself (e.g. "10") - confirmed live that these already match directly, no translation.
    - heater_log-kind tools (fiji1/2/3/savannah): channel_labels is keyed by the *log file's own
      header text* ("Heater 6", "Heater 12", ...) - a different, tool-internal numbering from the
      physical channel number a recipe references. recipe_channel_offset (0 unless explicitly
      configured) is the confirmed, hand-verified translation for this specific tool; e.g. fiji1's
      recipe channel "12" ("Cone") is the log's "Heater 6" (offset 6), while savannah's recipe
      channel numbers already match its log header directly (offset 0, the default - no config
      needed). Never guesses: an unconfigured/wrong offset just means no label here, not a wrong
      one - see this function's callers for why that trade-off is deliberate.
    """
    if channel in channel_labels:
        return channel_labels[channel][0]
    try:
        header_key = f"Heater {int(channel) - channel_offset}"
    except ValueError:
        return None
    return channel_labels[header_key][0] if header_key in channel_labels else None


def _parse_steps(raw_text, channel_labels, channel_offset=0):
    """Tab-delimited "<command>\\t<channel/arg>\\t<value>\\t<unit>" lines - not every line has all
    four fields, so this pads rather than requiring an exact column count. channel_label is only
    ever resolved for "heater" lines - see _heater_channel_label."""
    channel_labels = channel_labels or {}
    steps = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        fields += [""] * (4 - len(fields))
        command, channel, value, unit = (f.strip() for f in fields[:4])

        label = _heater_channel_label(channel, channel_labels, channel_offset) if command.lower() == "heater" else None

        steps.append(
            {
                "line": line_no,
                "command": command,
                "channel": channel,
                "channel_label": label,
                "value": value,
                "unit": unit,
                "raw": line,
            }
        )
    return steps


def _summarize_steps(steps):
    """"the parameters" at a glance, without reading the full step table: how many cycles the
    recipe loops (from its first "goto" line), and every distinct heater setpoint it sets
    (first-seen order, one entry per channel even if set more than once)."""
    cycles = None
    for step in steps:
        if step["command"].lower() == "goto":
            try:
                cycles = int(float(step["value"]))
            except ValueError:
                cycles = None
            break

    heater_setpoints = []
    seen_channels = set()
    for step in steps:
        if step["command"].lower() != "heater" or step["channel"] in seen_channels:
            continue
        seen_channels.add(step["channel"])
        heater_setpoints.append(
            {
                "channel": step["channel"],
                "label": step["channel_label"],
                "value": step["value"],
                # Always "°C", regardless of whatever the recipe file's own unit field happens to
                # say for this particular line (some are blank, some say "deg C", some "C" - a
                # "heater" command is always a temperature setpoint, so this is never ambiguous).
                "unit": "°C",
            }
        )

    return {"step_count": len(steps), "cycles": cycles, "heater_setpoints": heater_setpoints}


def get_recipe_detail(cfg, recipe_id):
    """Full detail for one recipe: the list_recipes() entry, plus raw_text/steps/summary fields.
    Returns None if recipe_id doesn't match anything currently in the tree (e.g. it was deleted
    or renamed on Oak since the tree was last listed)."""
    tool = cfg.get("remote_tool")
    recipe_subdir = cfg.get("recipe_subdir")
    entry = find_recipe(cfg, recipe_id)
    if not tool or not recipe_subdir or entry is None:
        return None

    local_path = remote_cache.ensure_cached(tool, f"{recipe_subdir}/{entry['relpath']}", ttl=RECIPE_CONTENT_TTL)
    with open(local_path, encoding=FILE_ENCODING) as f:
        raw_text = f.read()

    steps = _parse_steps(raw_text, cfg.get("channel_labels"), cfg.get("recipe_channel_offset", 0))
    summary = _summarize_steps(steps)
    return {**entry, "raw_text": raw_text, "steps": steps, **summary}


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
    seen = set()
    candidates = []
    for recipe in list_recipes(cfg):
        name_lower = recipe["name"].lower()
        if not any(keyword in name_lower for keyword in keywords):
            continue
        if any(keyword in name_lower for keyword in excluded):
            continue
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
