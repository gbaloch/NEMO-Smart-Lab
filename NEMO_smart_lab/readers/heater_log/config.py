"""Reading the tool's live Setup.ini for the real MFC label."""

import re

from NEMO_smart_lab import remote_sync


_INI_MFC1_LABEL_RE = re.compile(r'^MFC1LABEL\s*=\s*"([^"]*)"', re.IGNORECASE | re.MULTILINE)


def _heater_log_config_mfc_label(cfg):
    """Real label for the one MFC channel a heater_log-kind tool's run log actually carries a
    continuous flow reading for (see _heater_log_chart_groups' "mfc_flow" group) - confirmed live
    across all four real heater_log tools (fiji1/2/3/savannah) that the run log's own "MFC 1"
    column is always this one specific channel, never any other, regardless of how many MFCs the
    tool physically has (fiji1's own Setup.ini.txt confirms 8, MFC0 through MFC7 - the rest are
    plain on/off gas valves with no continuous flow telemetry wired into this log format at all,
    not something readers.py is failing to parse). Setup.ini.txt's own "[MFC]" section names that
    one channel specifically, e.g. `MFC1LABEL="MFC 1 Ar Plasma (sccm)"` - reading it real rather
    than showing the generic, physically-ambiguous "MFC 1" the log file's own header always says.

    Only meaningful for heater_log-kind tools with config_subdir configured (see
    NEMO_smart_lab.configs) - returns None (not an error) otherwise, or if no Setup.ini-named file
    is found, or nothing parses. Never guesses at which config file is "the" current one beyond
    matching the canonical "setup.ini.txt" name exactly (see
    NEMO_smart_lab.configs.find_active_config_file, shared with the config file browser's own
    "currently in use" badge so the two can never disagree) - confirmed live that older/renamed
    copies ("Setup.ini - Copy.txt", "Setup.ini fiji1 old.txt") can sit right alongside it."""
    if cfg.get("kind") != "heater_log" or not cfg.get("config_subdir"):
        return None
    from NEMO_smart_lab.configs import find_active_config_file, get_config_file_detail

    try:
        entry = find_active_config_file(cfg)
        if entry is None:
            return None
        detail = get_config_file_detail(cfg, entry["id"])
    except remote_sync.RemoteSyncError:
        return None
    if detail is None or detail.get("raw_text") is None:
        return None
    match = _INI_MFC1_LABEL_RE.search(detail["raw_text"])
    return match.group(1).strip() or None if match else None
