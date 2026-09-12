"""
Configuration for the Smart Lab plugin.

Every configured tool lives in the database as a NEMO_smart_lab.models.SmartLabTool row,
managed from the Django admin (Smart Lab > Smart Lab tools) - not in settings.py - so a lab
manager can add, edit, or disable a tool without a code deploy. `get_tool_sources()` below reads
that table and builds the {"<tool name>": {"kind": ..., "root": ..., ...}} mapping
NEMO_smart_lab.readers actually consumes.

Each SmartLabTool has:
    name             - must exactly match a real NEMO Tool.name.
    kind             - selects which reader in NEMO_smart_lab.readers is used, and what
                       local_root should point at:
      "heater_log" - Veeco Fiji/Savannah ALD style: tab-delimited files in
                     <local_root>/Logfile/Heater Data/*.txt, one file per run.
      "mvd"        - Cambridge Nanotech/Veeco MVD style: one folder per run under
                     <local_root>/log/data/<timestamp>_<recipe>/, with a *_SUM.txt and *_DAT.txt.
      "waferlog"   - Plasma-Therm VersaLine style: one file per wafer run under
                     <local_root>/WaferLog-Data/*.txt (step table + endpoint channel history).
      "cobra_job"  - Oxford Instruments PlasmaPro 100 Cobra (PTIQ) style: a single growing
                     SQLite database at <local_root>/Databases-Data/Jobs.db.
      "eventlog"   - KLA-DSE (Trikon/SPTS "fxPLPXTMC") style: a single growing CSV log at
                     <local_root>/EventLog-Data/CurrentEvents.csv.
    local_root       - path to the tool's raw data folder - e.g. a network share synced from
                       the tool PC, or a directory kept mirrored by `sync_remote_data` (below).
    on_threshold_c / on_threshold_pct
                     - kind-specific "is this channel on" thresholds - see the reader
                       docstrings in readers.py for defaults.
    stream_root / stream_module
                     - cobra_job only, both optional - adds a "Telemetry" section to the tool's
                       detail page, read from the tool's raw PTIQ StreamedData rather than
                       Jobs.db. stream_root is a synced copy of PTIQ/Databases/StreamedData
                       (NOT Databases-Data/Jobs.db's "Databases-Data" - this is the raw PTIQ
                       folder name, unrelated to how local_root is organized for this tool).
                       Needs the "msgpack" package - see the "Optional: PTIQ live telemetry"
                       section in readers.py for what this actually reads.
    real_id / real_category
                     - optional, used only by the seed_smart_lab_demo management command to
                       align a dev/demo database's Tool ids with a real production instance. Also
                       reused (real_id only) as "the Tool id on usage_reference_source" - see below.
    channel_labels (SmartLabToolChannel rows, admin: Smart Lab > Channel labels, or inline on the
                     tool itself) - optional per-channel {"Heater 3": ("Source chuck", "chuck")}
                     overrides consumed by readers.py to show a physical name/role instead of the
                     raw channel key. Nothing populates these automatically (see
                     NEMO_smart_lab.models.SmartLabToolChannel's docstring for why) - an admin has
                     to fill them in by hand, once, per tool.
    usage_reference_source
                     - optional NEMO_smart_lab.models.NemoApiSource - if set, and this tool's own
                       local Reservation/UsageEvent history has nothing for a given run,
                       NEMO_smart_lab.reservations.get_run_usage() falls back to a read-only GET
                       against that *other* NEMO instance's API (using real_id above as the Tool
                       id there) purely for display. Never used to write anything, anywhere.

Optionally, instead of (or before) pointing local_root at a live network share, a tool's raw
data can be pulled down from any SSH-reachable remote file server (Stanford's Oak, a
departmental fileserver, etc.) with the `sync_remote_data` management command. That needs one
NEMO_smart_lab.models.RemoteSyncEndpoint row (Django admin: Smart Lab > Remote sync endpoints)
describing the remote host/user/key/base path, and the SmartLabTool's own `sync_endpoint` +
`remote_subdir` fields pointing at it - see NEMO_smart_lab/remote_sync.py.

See README.md for a full worked example.
"""

from django.core.cache import cache

from NEMO_smart_lab.models import SmartLabTool

# Every single Smart Lab view calls get_tool_sources() at least once (most call it via _resolve()
# to look up just one tool) - without caching, that's one SmartLabTool table scan *plus* one
# per-tool channel_labels query (an actual N+1, not just a style nit - select_related can't help
# here since channel_labels is a reverse FK/reverse-related manager, hence the
# prefetch_related below) on every single page view, including AJAX chart-data/PNG requests fired
# repeatedly per page. The underlying table rarely changes (an admin editing tool config is a rare,
# deliberate action) so a short cache window trades a few seconds of "admin edit takes effect" lag
# for cutting that down to at most one query per TTL window, campus-wide traffic included - the
# same "short but not zero" trade already used for NEMO_smart_lab.status's live-status check.
TOOL_SOURCES_TTL = 30
_CACHE_KEY = "smart_lab:tool_sources"


def get_tool_sources():
    """
    Returns the {"<tool name>": {"kind": ..., "root": ..., ...}} mapping NEMO_smart_lab.readers
    expects, built from the SmartLabTool table and cached for TOOL_SOURCES_TTL seconds (see above)
    so admin edits take effect within a few seconds rather than needing a server restart, without
    paying a full table-plus-N+1-channel-labels query on every request.
    """
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached
    sources = {
        tool.name: tool.as_source_config() for tool in SmartLabTool.objects.filter(enabled=True).prefetch_related("channel_labels")
    }
    cache.set(_CACHE_KEY, sources, TOOL_SOURCES_TTL)
    return sources


def invalidate_tool_sources_cache():
    """Call after any in-app write to a SmartLabTool row (e.g. views.tool_recipe_toggle_pin) so
    the change is reflected on the very next request instead of waiting out TOOL_SOURCES_TTL -
    that TTL is fine for "an admin edited something in the Django admin", but a user-facing action
    that redirects straight back to a page reading get_tool_sources() needs to see its own write
    immediately."""
    cache.delete(_CACHE_KEY)
