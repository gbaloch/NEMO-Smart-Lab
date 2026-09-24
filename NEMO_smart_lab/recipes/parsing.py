"""Reading one recipe file: steps, channel labels, summary, plus prefetching many at once."""

from NEMO_smart_lab import remote_cache
from NEMO_smart_lab.readers import (
    FILE_ENCODING,
    _mark_shared_roles,
    _mvd_config_heater_labels,
    tool_wide_role_counts,
)
from NEMO_smart_lab.recipes.listing import RECIPE_CONTENT_TTL, find_recipe


def _heater_channel_label(channel, channel_labels, channel_offset):
    """Resolves a recipe "heater" line's channel number to this tool's curated (display_name,
    role) - see SmartLabToolChannel/_channel_label in readers.py for what "role" means (e.g.
    "chuck", "reactor") and how the *regular* tool detail page shows it as a small subtitle under
    a channel's name (readers._mark_shared_roles). Trying both key shapes
    SmartLabToolChannel.channel_key can be stored in (see SmartLabTool.recipe_channel_offset's
    docstring for how this was verified per tool):

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

    Returns (None, None) when nothing matches, so callers can unpack this unconditionally.
    """
    if channel in channel_labels:
        display_name, role = channel_labels[channel][0], channel_labels[channel][1]
        return display_name, role
    try:
        header_key = f"Heater {int(channel) - channel_offset}"
    except ValueError:
        return None, None
    if header_key in channel_labels:
        return channel_labels[header_key][0], channel_labels[header_key][1]
    return None, None


def _parse_steps(raw_text, channel_labels, channel_offset=0):
    """Tab-delimited "<command>\\t<channel/arg>\\t<value>\\t<unit>" lines - not every line has all
    four fields, so this pads rather than requiring an exact column count. channel_label/
    channel_role are only ever resolved for "heater" lines - see _heater_channel_label."""
    channel_labels = channel_labels or {}
    steps = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        fields += [""] * (4 - len(fields))
        command, channel, value, unit = (f.strip() for f in fields[:4])

        label, role = _heater_channel_label(channel, channel_labels, channel_offset) if command.lower() == "heater" else (None, None)

        steps.append(
            {
                "line": line_no,
                "command": command,
                "channel": channel,
                "channel_label": label,
                "channel_role": role,
                "value": value,
                "unit": unit,
                "raw": line,
            }
        )
    return steps


def _summarize_steps(steps, role_counts=None):
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
                "role": step["channel_role"],
                "value": step["value"],
                # Always "°C", regardless of whatever the recipe file's own unit field happens to
                # say for this particular line (some are blank, some say "deg C", some "C" - a
                # "heater" command is always a temperature setpoint, so this is never ambiguous).
                "unit": "°C",
            }
        )
    # Same "only show the role subtitle when it actually distinguishes something" rule as the
    # regular tool detail page's own heater channel table (readers._mark_shared_roles), and - when
    # `role_counts` is given (see readers.tool_wide_role_counts, threaded through from
    # get_recipe_detail below) - the SAME tool-wide counts that page uses, not just whichever
    # channels this one recipe happens to set: a role shared tool-wide (e.g. every precursor
    # channel is "jacket") should show its subtitle here too, even though this particular recipe
    # might only set one such channel, which used to look "unique" and hide the subtitle.
    _mark_shared_roles(heater_setpoints, role_counts)

    return {"step_count": len(steps), "cycles": cycles, "heater_setpoints": heater_setpoints}


def _recipe_channel_labels(cfg):
    """Channel labels for resolving a recipe's own heater setpoint names - the tool's admin-
    configured overrides (cfg["channel_labels"], see _heater_channel_label) merged with the same
    auto-parsed config.ini heater names (readers._mvd_config_heater_labels) the live "Heater
    channels" table already falls back to for mvd-kind tools when there's no DB override for a
    given channel. Confirmed live on fiji5: channels 14/15/16/17/19 have DB overrides (so both the
    recipe table and the live channel table already agreed), but 13/24/25/30 only ever had a name
    from config.ini ("UPPER"/"EXHAUST TEE"/"EXHAUST VALVE"/"LL TUNNEL") - the live table picked
    that up (see _mvd_summary), but a recipe's own setpoint table had no path to it at all and fell
    back to the bare "Heater <n>" placeholder. A DB override still wins where both exist (this only
    fills gaps, via setdefault) - same precedence _mvd_summary/_mvd_chart_groups already use."""
    labels = dict(cfg.get("channel_labels") or {})
    if cfg.get("kind") == "mvd":
        for num, label in _mvd_config_heater_labels(cfg).items():
            labels.setdefault(num, (label, None))
    return labels


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

    steps = _parse_steps(raw_text, _recipe_channel_labels(cfg), cfg.get("recipe_channel_offset", 0))
    summary = _summarize_steps(steps, tool_wide_role_counts(cfg))
    return {**entry, "raw_text": raw_text, "steps": steps, **summary}


def _prewarm_recipe_files(cfg, recipes):
    """Concurrently prefetches every recipe file in `recipes` (list_recipes()-shaped entries, each
    needing its own "relpath") before any of them are individually parsed via get_recipe_detail.
    Recipe files are each individually small, but get_recipe_detail's own ensure_cached() call is
    still a real ~1.5s round trip apiece when remote and not yet locally cached - fetching a whole
    tool's worth of them (dozens to ~100, see find_duplicate_recipes/total_cycles_run/
    suggest_base_pressure_recipes, every one of which needs to look at more than one recipe's own
    content) one at a time serializes that into a real, easy-to-miss multi-minute wait on a cold
    cache. No-op (no network call at all) for a tool with no sync_endpoint/recipe_subdir, or an
    empty `recipes` list - already-local files cost nothing extra either way, see
    ensure_cached_many's own fast path."""
    tool = cfg.get("remote_tool")
    recipe_subdir = cfg.get("recipe_subdir")
    if tool is not None and recipe_subdir and recipes:
        remote_cache.ensure_cached_many(tool, [f"{recipe_subdir}/{r['relpath']}" for r in recipes])
