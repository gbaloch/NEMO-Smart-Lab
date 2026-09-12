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

    recipes = []
    for relpath, mtime, size, is_dir in entries:
        if is_dir:
            continue
        category = relpath.split("/", 1)[0] if "/" in relpath else "(top level)"
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
    recipes.sort(key=lambda r: (r["category"].lower(), r["name"].lower()))
    return recipes


def find_recipe(cfg, recipe_id):
    return next((r for r in list_recipes(cfg) if r["id"] == recipe_id), None)


def _parse_steps(raw_text, channel_labels):
    """Tab-delimited "<command>\\t<channel/arg>\\t<value>\\t<unit>" lines - not every line has all
    four fields, so this pads rather than requiring an exact column count. channel_label is only
    resolved for "heater" lines, and only actually resolves for "mvd"-kind tools (mvd/fiji5) -
    confirmed live against real Oak data that their recipe channel numbers are the same raw HTR
    number already curated into SmartLabToolChannel (e.g. "10" -> "Valve Manifold" matches both).
    For "heater_log"-kind tools (fiji1/2/3/savannah) this intentionally never matches and falls
    back to a bare channel number: their SmartLabToolChannel keys are the *log file's own header
    text* ("Heater 6", "Heater 12", ...), a different, tool-internal numbering from the physical
    channel number a recipe file actually references (confirmed for fiji1: log header "Heater N"
    is physical channel N+6, but that offset was reverse-engineered by hand against one tool's
    data, not something derivable from the recipe/log files alone, and hasn't been verified for
    fiji2/fiji3/savannah individually - showing a *wrong* label here would be worse than none, so
    this deliberately stays unresolved rather than guessing at an unverified per-tool offset."""
    channel_labels = channel_labels or {}
    steps = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        fields += [""] * (4 - len(fields))
        command, channel, value, unit = (f.strip() for f in fields[:4])

        label = None
        if command.lower() == "heater" and channel in channel_labels:
            label = channel_labels[channel][0]

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
                "unit": step["unit"],
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

    steps = _parse_steps(raw_text, cfg.get("channel_labels"))
    summary = _summarize_steps(steps)
    return {**entry, "raw_text": raw_text, "steps": steps, **summary}
