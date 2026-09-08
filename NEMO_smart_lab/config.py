"""
Configuration for the Smart Lab plugin.

Every configured tool lives in the database as a NEMO_smart_lab.models.SmartLabTool row,
managed from the Django admin (Tool Data > Smart Lab tools) - not in settings.py - so a lab
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
                       align a dev/demo database's Tool ids with a real production instance.

Optionally, instead of (or before) pointing local_root at a live network share, a tool's raw
data can be pulled down from any SSH-reachable remote file server (Stanford's Oak, a
departmental fileserver, etc.) with the `sync_remote_data` management command. That needs one
NEMO_smart_lab.models.RemoteSyncEndpoint row (Django admin: Tool Data > Remote sync endpoints)
describing the remote host/user/key/base path, and the SmartLabTool's own `sync_endpoint` +
`remote_subdir` fields pointing at it - see NEMO_smart_lab/remote_sync.py.

See README.md for a full worked example.
"""

from NEMO_smart_lab.models import SmartLabTool


def get_tool_sources():
    """
    Returns the {"<tool name>": {"kind": ..., "root": ..., ...}} mapping NEMO_smart_lab.readers
    expects, built fresh from the SmartLabTool table on every call (cheap - a handful of rows at
    most sites) so admin edits take effect immediately, with no server restart.
    """
    return {tool.name: tool.as_source_config() for tool in SmartLabTool.objects.filter(enabled=True)}
