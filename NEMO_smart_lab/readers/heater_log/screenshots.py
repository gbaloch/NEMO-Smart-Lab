"""Finding the post-run screenshot that belongs to a Fiji/Savannah run."""

import glob
import os

from NEMO_smart_lab import remote_cache, remote_sync


def _screenshot_base_name(run_id):
    base = run_id
    while base.lower().endswith(".txt"):
        base = base[: -len(".txt")]
    return base


def _matches_screenshot_pattern(name, base):
    # Equivalent to the local glob pattern glob.escape(base) + "*.jpg*" below: starts with the
    # run's own base name, contains ".jpg" somewhere after it (covering both the plain ".jpg" and
    # the odd ".jpg.txt" spelling - see the docstring below).
    return name.startswith(base) and ".jpg" in name[len(base) :].lower()


def _heater_log_screenshot_path(cfg, run_id):
    """Best-effort path to a run's post-run report screenshot, if the tool exports one:
    Logfile/Reports/ holds a JPEG per run, named after that run's own base filename - but with an
    inconsistent extra ".txt" suffix on top of ".jpg" for some runs and not others (real JPEG
    bytes either way - confirmed by inspecting real files, not just their names). This mirrors the
    same quirk already seen in some run filenames themselves (a recipe name that already ends in
    ".txt" gets a second ".txt" appended by the heater log export), so matching by the run's own
    base name - with any number of trailing ".txt" stripped - covers both spellings.

    Reports/ is a separate remote folder from Heater Data/, so resolving the run itself doesn't
    fetch a matching report image as a side effect (unlike mvd, where a run's whole folder -
    screenshot included - is fetched as one unit) - this does its own remote listing + fetch of
    just the one matching file when a sync_endpoint is configured."""
    base = _screenshot_base_name(run_id)
    tool = cfg.get("remote_tool")
    if tool is not None:
        try:
            entries = remote_cache.list_remote_dir(tool.sync_endpoint, f"{tool.remote_subdir_or_default}/Logfile/Reports")
        except remote_sync.RemoteSyncError:
            return None
        match = next((name for name, _mtime, _size, is_dir in entries if not is_dir and _matches_screenshot_pattern(name, base)), None)
        if not match:
            return None
        try:
            return remote_cache.ensure_cached(tool, f"Logfile/Reports/{match}")
        except remote_sync.RemoteSyncError:
            return None

    reports_dir = os.path.join(cfg["root"], "Logfile", "Reports")
    if not os.path.isdir(reports_dir):
        return None
    matches = glob.glob(os.path.join(reports_dir, glob.escape(base) + "*.jpg*"))
    return matches[0] if matches else None
