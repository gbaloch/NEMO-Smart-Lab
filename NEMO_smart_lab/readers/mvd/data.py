"""Whole-run MVD data assembly, plus the small cached per-run history summary."""

import os

from NEMO_smart_lab.readers.common import FILE_ENCODING, _cached_file_parse, _find_one, _last_non_null
from NEMO_smart_lab.readers.mvd.parsing import _parse_mvd_dat, _parse_mvd_summary_text
from NEMO_smart_lab.readers.mvd.runs import _mvd_run_end, _resolve_mvd_run_dir


def _mvd_run_data_for_dir(run_dir):
    # Resolving which two files this run actually has is a cheap directory scan (_find_one) -
    # only the parse of their *contents* (_parse_mvd_dat especially, on a potentially large DAT
    # file) is expensive enough to be worth caching - see _cached_file_parse.
    sum_path = _find_one(run_dir, "_SUM.txt")
    dat_path = _find_one(run_dir, "_DAT.txt")
    return _cached_file_parse("mvd_run", [sum_path, dat_path], lambda: _mvd_run_data_for_dir_uncached(run_dir, sum_path, dat_path))


def _mvd_run_history_summary(run_dir, threshold):
    """The tiny handful of history-list fields _mvd_history actually needs per run (run_id,
    recipe, end timestamp, duration, on/off status, completion status, faulty flag) - cached under
    its OWN, much smaller key, separate from _mvd_run_data_for_dir's full parsed series, the same
    "cache the small derived values, not the huge raw ones" lesson _mvd_run_maintenance_signals
    already documents. Without this, `get_tool_history` (which underpins the run history page
    itself, get_recent_faulty_runs, get_recent_runs, AND get_fault_rate_trend's own scan) paid the
    full cost of unpickling/deep-copying an entire run's 200,000-row parsed series back out of
    cache for every single run in the scan, even on an otherwise fully warm cache - confirmed live:
    get_fault_rate_trend's own scan_limit=60 mvd scan measured at ~3.5s even with every underlying
    file's own parse already cached, entirely from that copy cost alone, dominating the "Trends"
    tab's own load time far more than any real parsing work left to do.

    `threshold` (cfg's own on_threshold_pct) is folded into the cache key's own "kind" string
    (not just passed to `compute`) so a tool with a different configured threshold never shares a
    (would-be-wrong) cached on/off status with one that has a different threshold."""
    sum_path = _find_one(run_dir, "_SUM.txt")
    dat_path = _find_one(run_dir, "_DAT.txt")

    def compute():
        data = _mvd_run_data_for_dir(run_dir)
        latest_duties = [v for v in (_last_non_null(v) for v in data["duty_series"].values()) if v is not None]
        any_on = any(v > threshold for v in latest_duties)
        completion_status = data["summary"]["completion_status"]
        return {
            "run_id": data["run_id"],
            "recipe": data["summary"]["recipe"] or "(unknown)",
            "timestamp": _mvd_run_end(data),
            "duration_s": data["time_s"][-1] if data["time_s"] else None,
            "any_on": any_on,
            "status_label": "ON" if any_on else "Idle",
            "status_class": "warning" if any_on else "success",
            "completion_status": completion_status,
            "faulty": bool(completion_status) and completion_status.strip().lower() != "successfully completed",
        }

    return _cached_file_parse(f"mvd_history_summary_{threshold}", [sum_path, dat_path], compute)


def _mvd_run_data_for_dir_uncached(run_dir, sum_path, dat_path):
    with open(sum_path, encoding=FILE_ENCODING) as f:
        summary = _parse_mvd_summary_text(f.read())
    time_s, temp_series, duty_series, ramp_rate_series, other_series = _parse_mvd_dat(dat_path)
    return {
        "run_dir": run_dir,
        "run_id": os.path.basename(run_dir),
        "mtime": os.path.getmtime(dat_path),
        "summary": summary,
        "time_s": time_s,
        "temp_series": temp_series,
        "duty_series": duty_series,
        "ramp_rate_series": ramp_rate_series,
        "other_series": other_series,
    }


def _mvd_run_data(cfg, run_id=None):
    return _mvd_run_data_for_dir(_resolve_mvd_run_dir(cfg, run_id))
