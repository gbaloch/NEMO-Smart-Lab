"""Reading channel labels (heaters, MFCs) out of the tool's live config.ini."""

import re

from NEMO_smart_lab import remote_sync


_INI_HEATER_LABEL_RE = re.compile(r'^HTR(\d+)\s*=\s*(?:label:)?"([^"]*)"', re.IGNORECASE | re.MULTILINE)


def _mvd_config_heater_labels(cfg):
    """Real heater channel names, read from this tool's own config.ini (the "(root)"-category
    one under config_subdir - see NEMO_smart_lab.configs) rather than left to a per-run _SUM.txt's
    own HTR<n> label field (_HEATER_LABEL_RE, used as `labels` below already) - confirmed live
    that field is blank in every real per-run export, while config.ini's own [heaters] section (a
    persistent, tool-wide file, not a per-run one) reliably carries the real names an operator
    actually configured, confirmed live in two different real formats depending on software
    version: fiji5's `HTR13 = label:"UPPER", setpt:150, ...` and mvd's own
    `HTR6 = "EXHAUST TRAP",80,180,...` - both just a quoted label right after "=", optionally
    behind a "label:" prefix, which is all this looks for.

    Only meaningful for mvd-kind tools with config_subdir configured; returns {} (not an error)
    otherwise, or if no config.ini is found, or nothing parses - a nice-to-have that should never
    be able to break a channel table just because a config file couldn't be read."""
    text = _mvd_config_ini_text(cfg)
    if text is None:
        return {}
    return {num: label for num, label in _INI_HEATER_LABEL_RE.findall(text) if label.strip()}


def _mvd_config_ini_text(cfg):
    """Raw text of this mvd-kind tool's own root config.ini (see
    NEMO_smart_lab.configs.find_active_config_file for exactly which file that is), shared by
    every config.ini-based label lookup (_mvd_config_heater_labels, _mvd_config_mfc_labels) so
    each doesn't re-implement the same "find and fetch config.ini" lookup. None (not an error) for
    a non-mvd-kind tool, one with no config_subdir configured, one with no config.ini found, or a
    remote listing/fetch failure."""
    if cfg.get("kind") != "mvd" or not cfg.get("config_subdir"):
        return None
    from NEMO_smart_lab.configs import find_active_config_file, get_config_file_detail

    try:
        entry = find_active_config_file(cfg)
        if entry is None:
            return None
        detail = get_config_file_detail(cfg, entry["id"])
    except remote_sync.RemoteSyncError:
        return None
    if detail is None:
        return None
    return detail.get("raw_text")


_INI_MFC_LABEL_RE = re.compile(r'^MFC(\d+)\s*=\s*"([^"]*)"', re.IGNORECASE | re.MULTILINE)


_MFC_CHANNEL_RE = re.compile(r"^MFC(\d+)(_setpoint|_reading)?$", re.IGNORECASE)


def _mvd_config_mfc_labels(cfg):
    """Real MFC channel names, read from this mvd-kind tool's own config.ini "[mfc]" section
    (e.g. fiji5's `MFC1 = "PLASMA (Ar)",0,500,...` / mvd's own `MFC0 = "CARRIER N2",0,100,...`) -
    confirmed live that, unlike heater channels, the per-run DAT/SUM files never carry any MFC
    label field at all, so this is the *only* source for a real name; without it, every MFC series
    shows only its raw column name ("MFC0_reading", "MFC1_setpoint", ...). Same
    config_subdir-gated, never-guesses, {} (not an error) on any failure contract as
    _mvd_config_heater_labels - see that function's own docstring."""
    text = _mvd_config_ini_text(cfg)
    if text is None:
        return {}
    return {num: label for num, label in _INI_MFC_LABEL_RE.findall(text) if label.strip()}


def _mvd_mfc_display_name(name, mfc_labels):
    """Renames an "other_series" column base name like "MFC1_setpoint"/"MFC0" to its real config
    label (see _mvd_config_mfc_labels), e.g. "PLASMA (Ar) (MFC1) setpoint" - the "(MFC<n>)" suffix
    mirrors the exact same disambiguation heater channels already get (_mvd_chart_groups' own
    htr_label: "<name> (HTR<n>)"), in case two MFCs coincidentally share a label. Returns `name`
    unchanged if it doesn't look like an MFC column at all, or there's no real label for its
    channel number (config.ini missing/not configured, or that specific MFC# has no label)."""
    match = _MFC_CHANNEL_RE.match(name)
    if not match:
        return name
    num, suffix = match.group(1), (match.group(2) or "").lower()
    label = mfc_labels.get(num, "").strip()
    if not label:
        return name
    suffix_display = {"_setpoint": " setpoint", "_reading": " reading", "": ""}[suffix]
    return f"{label} (MFC{num}){suffix_display}"
