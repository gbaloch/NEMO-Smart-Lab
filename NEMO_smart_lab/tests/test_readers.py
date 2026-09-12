"""
Tests for NEMO_smart_lab.readers - all self-contained: each test builds a tiny synthetic
version of the real file/database format in a temp directory, so nothing here depends on any
real tool data being present on disk.
"""

import csv
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.readers import ToolDataError, get_chart_groups, get_tool_history, get_tool_summary


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


# -------------------- heater_log (Fiji/Savannah) --------------------


def _write_heater_log(path, header_cols, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t" + "\t".join(header_cols) + "\n")
        for row in rows:
            f.write("\t" + "\t".join(row) + "\n")


class HeaterLogTests(TempDirTestCase):
    FULL_HEADER = (
        ["Heater Time"] + [f"Heater {n}" for n in range(6, 18)] + ["Program Time", "MFC 1", "MFC Time", "Cycles Remaining", "Recipe", "Loop"]
    )

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, "Logfile", "Heater Data"))

    def _cfg(self, threshold=35.0):
        return {"kind": "heater_log", "root": self.root, "on_threshold_c": threshold}

    def test_full_width_row_all_twelve_channels(self):
        # Fiji-style: every row has all 12 heater columns (row length matches header length).
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "My Recipe", ""]
        _write_heater_log(
            os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row]
        )
        summary = get_tool_summary("fiji-test", self._cfg())
        self.assertNotIn("error", summary)
        self.assertEqual(len(summary["channels"]), 12)
        self.assertTrue(all(c["on"] for c in summary["channels"]))
        self.assertEqual(summary["recipe"], "My Recipe")

    def test_short_row_only_maps_present_channels(self):
        # Savannah-style: the tool only has 8 physical heater zones, so data rows are shorter
        # than the header (omit the trailing 4 heater columns entirely) instead of padding with
        # placeholders. This used to silently misalign "Program Time" etc. into heater columns.
        row = ["0.0"] + ["150.0"] * 8 + ["5245023.0", "4.95", "0.989", "20", "clear0", ""]
        _write_heater_log(
            os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row]
        )
        summary = get_tool_summary("savannah-test", self._cfg())
        self.assertNotIn("error", summary)
        self.assertEqual(len(summary["channels"]), 8)
        names = {c["name"] for c in summary["channels"]}
        self.assertEqual(names, {f"Heater {n}" for n in range(6, 14)})
        # "Program Time" (5245023.0) must NOT have been read in as a heater temperature.
        self.assertTrue(all(c["latest_value"] < 1000 for c in summary["channels"]))

    def test_ambient_channel_is_off(self):
        row = ["0.0", "200.0", "27.1"] + ["0.0"] * 10 + ["100.0", "19.9", "1.5", "0", "R", ""]
        _write_heater_log(
            os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row]
        )
        summary = get_tool_summary("t", self._cfg(threshold=35.0))
        by_name = {c["name"]: c for c in summary["channels"]}
        self.assertTrue(by_name["Heater 6"]["on"])  # 200.0 > 35.0
        self.assertFalse(by_name["Heater 7"]["on"])  # 27.1 (ambient) is not "on"

    def test_unknown_run_id_is_not_found(self):
        row = ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        summary = get_tool_summary("t", self._cfg(), run_id="../../../../windows/win.ini")
        self.assertIn("error", summary)

    def test_history_pagination(self):
        row = ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        for i in range(5):
            path = os.path.join(self.root, "Logfile", "Heater Data", f"run{i}.txt")
            _write_heater_log(path, self.FULL_HEADER, [row])
            # Force distinct, known mtimes so ordering is deterministic.
            t = datetime(2026, 1, 1 + i).timestamp()
            os.utime(path, (t, t))
        history, total = get_tool_history(self._cfg(), page=1, page_size=2)
        self.assertEqual(total, 5)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["run_id"], "run4.txt")  # newest mtime first

    def test_ordering_uses_the_runs_own_filename_timestamp_not_mtime(self):
        # Regression, confirmed live on Oak: a batch re-upload/re-sync can give an old run a fresh
        # mtime, which used to make it sort as "the newest run" even though its own filename says
        # otherwise. The filename's embedded timestamp - not any filesystem mtime - must win.
        row = ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        old_run = os.path.join(self.root, "Logfile", "Heater Data", "2020_01_01-00-00-00_Old Recipe.txt")
        new_run = os.path.join(self.root, "Logfile", "Heater Data", "2026_01_01-00-00-00_New Recipe.txt")
        _write_heater_log(old_run, self.FULL_HEADER, [row])
        _write_heater_log(new_run, self.FULL_HEADER, [row])
        # The genuinely older run gets a *newer* mtime than the genuinely newer one - simulating a
        # re-upload of archival data landing on disk after the real latest run was already synced.
        os.utime(old_run, (datetime(2030, 1, 1).timestamp(),) * 2)
        os.utime(new_run, (datetime(2020, 6, 1).timestamp(),) * 2)

        summary = get_tool_summary("fiji-test", self._cfg())
        self.assertEqual(summary["run_id"], "2026_01_01-00-00-00_New Recipe.txt")

    def test_filename_timestamp_without_seconds_is_still_parsed(self):
        # Real, confirmed bug: savannah's own filenames omit the seconds field entirely
        # ("2026_08_25-09-18_clear0.txt", minutes only) unlike fiji1/2/3's always-present seconds
        # ("2026_09_10-17-59-31_...") - this pattern silently never matched savannah's filenames at
        # all before, falling back to (unreliable) mtime for every one of its runs.
        from NEMO_smart_lab.readers import _heater_log_filename_timestamp

        self.assertEqual(_heater_log_filename_timestamp("2026_08_25-09-18_clear0.txt"), datetime(2026, 8, 25, 9, 18, 0))
        self.assertEqual(_heater_log_filename_timestamp("2026_09_10-17-59-31_Recipe.txt"), datetime(2026, 9, 10, 17, 59, 31))

    def test_displayed_last_update_comes_from_the_filename_not_mtime(self):
        # The run's filename says it started at 10:00:00 and (from its own last elapsed-seconds
        # row) ran for 30s, so it should display as ending at 10:00:30 - regardless of whatever
        # the file's mtime on disk happens to say (set here to something wildly different).
        rows = [
            ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""],
            ["30.0"] + ["1.0"] * 12 + ["30.0", "0.0", "0.0", "0", "R", ""],
        ]
        path = os.path.join(self.root, "Logfile", "Heater Data", "2026_06_15-10-00-00_R.txt")
        _write_heater_log(path, self.FULL_HEADER, rows)
        os.utime(path, (datetime(2099, 1, 1).timestamp(),) * 2)

        summary = get_tool_summary("fiji-test", self._cfg())
        self.assertEqual(summary["last_update"], datetime(2026, 6, 15, 10, 0, 30))

    def test_channel_label_override_replaces_name_but_keeps_raw_name(self):
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        cfg = self._cfg()
        cfg["channel_labels"] = {"Heater 10": ("Source chuck", "chuck", False, None)}
        summary = get_tool_summary("fiji-test", cfg)
        by_raw = {c["raw_name"]: c for c in summary["channels"]}
        self.assertEqual(by_raw["Heater 10"]["name"], "Source chuck")
        self.assertEqual(by_raw["Heater 10"]["role"], "chuck")
        # Untouched channels keep their raw name and a None role.
        self.assertEqual(by_raw["Heater 6"]["name"], "Heater 6")
        self.assertIsNone(by_raw["Heater 6"]["role"])

    def test_role_subtitle_suppressed_when_only_one_channel_has_it(self):
        # Heater 10 is the only "chuck"-role channel on this tool - showing "Chuck" as a subtitle
        # under its own name/"Source chuck" display name is pure noise, so the role should be
        # dropped. Heater 11/12 both share "cone" - a real, useful grouping - so their role stays.
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        cfg = self._cfg()
        cfg["channel_labels"] = {
            "Heater 10": ("Source chuck", "chuck", False, None),
            "Heater 11": ("Cone A", "reactor", False, None),
            "Heater 12": ("Cone B", "reactor", False, None),
        }
        summary = get_tool_summary("fiji-test", cfg)
        by_raw = {c["raw_name"]: c for c in summary["channels"]}
        # role itself is untouched (still real classification data) - only the display flag drops.
        self.assertEqual(by_raw["Heater 10"]["role"], "chuck")
        self.assertFalse(by_raw["Heater 10"]["role_shown"])
        self.assertEqual(by_raw["Heater 11"]["role"], "reactor")
        self.assertTrue(by_raw["Heater 11"]["role_shown"])
        self.assertTrue(by_raw["Heater 12"]["role_shown"])

    def test_channel_num_is_offset_to_the_physical_recipe_channel_not_the_raw_log_number(self):
        # Confirmed live (see SmartLabTool.recipe_channel_offset's docstring): fiji1/2/3's log
        # "Heater 11" is physically/on-the-recipe channel 17, a +6 offset - showing the raw,
        # un-offset "11" here was a real bug (reads as a wrong channel number to anyone comparing
        # against a recipe file or the tool's physical labeling).
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        cfg = self._cfg()
        cfg["recipe_channel_offset"] = 6
        summary = get_tool_summary("fiji-test", cfg)
        by_raw = {c["raw_name"]: c for c in summary["channels"]}
        self.assertEqual(by_raw["Heater 11"]["channel_num"], 17)
        self.assertEqual(by_raw["Heater 6"]["channel_num"], 12)

    def test_channel_num_with_no_offset_configured_matches_the_raw_log_number(self):
        # savannah's recipe_channel_offset is 0 (the default) - its log numbering already matches
        # the physical/recipe channel number directly.
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        summary = get_tool_summary("fiji-test", self._cfg())
        by_raw = {c["raw_name"]: c for c in summary["channels"]}
        self.assertEqual(by_raw["Heater 11"]["channel_num"], 11)

    def test_channel_level_on_threshold_overrides_tool_wide_default(self):
        # 28C would read as "off" against the tool-wide 35C default, but Heater 10 has its own
        # lower 25C threshold configured (e.g. a precursor jacket run cooler than the reactor).
        row = ["0.0"] + ["28.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        cfg = self._cfg(threshold=35.0)
        cfg["channel_labels"] = {"Heater 10": ("Precursor Jacket", "precursor_line", False, 25.0)}
        summary = get_tool_summary("fiji-test", cfg)
        by_raw = {c["raw_name"]: c for c in summary["channels"]}
        self.assertTrue(by_raw["Heater 10"]["on"])  # 28.0 > 25.0 (channel override)
        self.assertFalse(by_raw["Heater 6"]["on"])  # 28.0 < 35.0 (tool-wide default)

    def test_hidden_channel_is_excluded_from_summary_and_chart(self):
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        cfg = self._cfg()
        cfg["channel_labels"] = {"Heater 17": ("", "other", True, None)}
        summary = get_tool_summary("fiji-test", cfg)
        self.assertNotIn("Heater 17", {c["raw_name"] for c in summary["channels"]})
        self.assertEqual(len(summary["channels"]), 11)  # 12 channels total, minus the hidden one

        from NEMO_smart_lab.readers import get_chart_data

        _title, _x, _y, series = get_chart_data(cfg)
        self.assertNotIn("Heater 17", series)

    def test_mfc_flow_is_its_own_chart_group(self):
        # "19.9" (index matching "MFC 1") was already present in this fixture row but never
        # charted anywhere before get_chart_groups() existed - it's a real flow reading (sccm).
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "My Recipe", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        groups = get_chart_groups(self._cfg())
        self.assertEqual(groups[0]["key"], "temperature")
        by_key = {g["key"]: g for g in groups}
        self.assertIn("mfc_flow", by_key)
        self.assertEqual(by_key["mfc_flow"]["series"]["MFC 1"][1], [19.9])

    def test_mfc_flow_group_omitted_when_column_is_blank(self):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "", "1.5", "0", "My Recipe", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), self.FULL_HEADER, [row])
        groups = get_chart_groups(self._cfg())
        self.assertNotIn("mfc_flow", {g["key"] for g in groups})


class SiblingRunDataTests(HeaterLogTests):
    """Pressure Data/RF Data (Logfile/<subdir>/<same run filename> - confirmed live, identical
    format across fiji1/2/3/savannah) - a real gap this used to have entirely."""

    RUN_ID = "run1.txt"

    def _write_run(self, run_row=None):
        row = run_row or ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "My Recipe", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", self.RUN_ID), self.FULL_HEADER, [row])

    def _write_sibling(self, subdir, header_cols, rows):
        d = os.path.join(self.root, "Logfile", subdir)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.RUN_ID), "w", encoding="utf-8") as f:
            f.write("\t" + "\t".join(header_cols) + "\n")
            for row in rows:
                f.write("\t" + "\t".join(row) + "\n")

    def test_pressure_data_becomes_a_chart_group(self):
        self._write_run()
        self._write_sibling(
            "Pressure Data",
            ["Pressure Time", "Pressure ", "Cycles Remaining", "Recipe", "Loop"],
            [["0.021", "0.190", "0", "My Recipe", ""], ["0.171", "0.169", "0", "My Recipe", ""]],
        )
        groups = get_chart_groups(self._cfg())
        by_key = {g["key"]: g for g in groups}
        self.assertIn("pressure", by_key)
        self.assertEqual(by_key["pressure"]["y_label"], "Pressure (Torr)")
        self.assertEqual(by_key["pressure"]["series"]["Pressure"][1], [0.19, 0.169])

    def test_rf_data_becomes_a_chart_group_with_two_series(self):
        self._write_run()
        self._write_sibling(
            "RF Data",
            ["RF Time", "For Pwr (W) ", "Refl Pwr (W)", "Cycles Remaining", "Recipe", "Loop"],
            [["0.5", "0.0", "0.0", "0", "My Recipe", ""], ["3.5", "300.0", "5.0", "0", "My Recipe", ""]],
        )
        groups = get_chart_groups(self._cfg())
        by_key = {g["key"]: g for g in groups}
        self.assertIn("rf_power", by_key)
        self.assertEqual(set(by_key["rf_power"]["series"].keys()), {"For Pwr (W)", "Refl Pwr (W)"})
        self.assertEqual(by_key["rf_power"]["series"]["For Pwr (W)"][1], [0.0, 300.0])

    def test_missing_sibling_folder_is_simply_omitted(self):
        self._write_run()
        groups = get_chart_groups(self._cfg())
        self.assertNotIn("pressure", {g["key"] for g in groups})
        self.assertNotIn("rf_power", {g["key"] for g in groups})

    def test_pressure_group_omitted_when_every_value_is_blank(self):
        self._write_run()
        self._write_sibling(
            "Pressure Data",
            ["Pressure Time", "Pressure ", "Cycles Remaining", "Recipe", "Loop"],
            [["0.021", "", "0", "My Recipe", ""]],
        )
        groups = get_chart_groups(self._cfg())
        self.assertNotIn("pressure", {g["key"] for g in groups})

    def test_pressure_comes_right_after_temperature_even_with_mfc_flow_present(self):
        # A real ordering request: pressure belongs right next to temperature, not pushed down by
        # whatever other groups (MFC flow here) a given run also happens to have.
        self._write_run(["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "My Recipe", ""])
        self._write_sibling(
            "Pressure Data",
            ["Pressure Time", "Pressure ", "Cycles Remaining", "Recipe", "Loop"],
            [["0.021", "0.190", "0", "My Recipe", ""]],
        )
        groups = get_chart_groups(self._cfg())
        keys = [g["key"] for g in groups]
        self.assertEqual(keys[:3], ["temperature", "pressure", "mfc_flow"])


class HeaterLogEventTests(HeaterLogTests):
    """Event Files (Logfile/Event Files) - logged per *program session*, not one file per run
    (confirmed live: a single file held two separate Run Started/Run Ended pairs), with a
    different filename convention than Heater Data - see get_heater_log_run_events."""

    def _write_run(self, mtime, duration_s):
        # Two rows (start at t=0, end at t=duration_s) so data["time_s"][-1] - what
        # get_heater_log_run_events actually uses to derive the run's own start time - is really
        # the intended duration, not just whatever the single row happens to say.
        rows = [
            ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""],
            [str(duration_s)] + ["1.0"] * 12 + [str(duration_s), "0.0", "0.0", "0", "R", ""],
        ]
        path = os.path.join(self.root, "Logfile", "Heater Data", "run1.txt")
        _write_heater_log(path, self.FULL_HEADER, rows)
        os.utime(path, (mtime, mtime))

    def _write_event_file(self, session_start, lines):
        d = os.path.join(self.root, "Logfile", "Event Files")
        os.makedirs(d, exist_ok=True)
        name = session_start.strftime("%y%m%d_%H_%M_%S") + "- Event.txt"
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            for ts, text in lines:
                f.write(f"{ts.strftime('%m/%d/%y %H:%M:%S.%f')[:-3]}: {text}\n")

    def test_finds_run_started_and_ended_within_the_enclosing_session_file(self):
        from NEMO_smart_lab.readers import get_heater_log_run_events

        session_start = datetime(2026, 9, 10, 9, 51, 47)
        run_start = datetime(2026, 9, 10, 12, 34, 3)
        run_end = run_start + timedelta(seconds=30)
        self._write_event_file(
            session_start,
            [(session_start, "Program Started"), (run_start, "Run Started"), (run_end, "Run Ended")],
        )
        self._write_run(mtime=run_end.timestamp(), duration_s=30)

        _title, points = get_heater_log_run_events(self._cfg())
        self.assertEqual([p[2] for p in points], ["Run Started", "Run Ended"])
        self.assertEqual(points[0][0], 0.0)  # offset relative to the run's own start

    def test_events_far_outside_the_runs_window_are_excluded(self):
        from NEMO_smart_lab.readers import get_heater_log_run_events

        session_start = datetime(2026, 9, 10, 9, 51, 47)
        run_start = datetime(2026, 9, 10, 12, 34, 3)
        run_end = run_start + timedelta(seconds=30)
        far_away = run_start - timedelta(hours=1)
        self._write_event_file(session_start, [(session_start, "Program Started"), (far_away, "MFC reset value")])
        self._write_run(mtime=run_end.timestamp(), duration_s=30)

        _title, points = get_heater_log_run_events(self._cfg())
        self.assertEqual(points, [])

    def test_summary_alarm_count_counts_only_fault_keyword_events_in_the_runs_window(self):
        session_start = datetime(2026, 9, 10, 9, 51, 47)
        run_start = datetime(2026, 9, 10, 12, 34, 3)
        run_end = run_start + timedelta(seconds=30)
        self._write_event_file(
            session_start,
            [
                (session_start, "Program Started"),
                (run_start, "Run Started"),
                (run_start + timedelta(seconds=5), "MFC1 Alarm: over range"),
                (run_start + timedelta(seconds=10), "Heater Fault: open loop"),
                (run_end, "Run Ended"),
            ],
        )
        self._write_run(mtime=run_end.timestamp(), duration_s=30)

        summary = get_tool_summary("fiji-test", self._cfg())
        self.assertEqual(summary["alarm_count"], 2)

    def test_summary_alarm_count_is_zero_with_no_fault_events(self):
        session_start = datetime(2026, 9, 10, 9, 51, 47)
        run_start = datetime(2026, 9, 10, 12, 34, 3)
        run_end = run_start + timedelta(seconds=30)
        self._write_event_file(session_start, [(session_start, "Program Started"), (run_start, "Run Started"), (run_end, "Run Ended")])
        self._write_run(mtime=run_end.timestamp(), duration_s=30)

        summary = get_tool_summary("fiji-test", self._cfg())
        self.assertEqual(summary["alarm_count"], 0)

    def test_recent_faulty_runs_includes_a_run_with_an_alarm(self):
        from NEMO_smart_lab.readers import get_recent_faulty_runs

        session_start = datetime(2026, 9, 10, 9, 51, 47)
        clean_start = datetime(2026, 9, 10, 11, 0, 0)
        clean_end = clean_start + timedelta(seconds=30)
        faulty_start = datetime(2026, 9, 10, 12, 34, 3)
        faulty_end = faulty_start + timedelta(seconds=30)
        self._write_event_file(
            session_start,
            [
                (session_start, "Program Started"),
                (clean_start, "Run Started"),
                (clean_end, "Run Ended"),
                (faulty_start, "Run Started"),
                (faulty_start + timedelta(seconds=5), "Heater Fault: open loop"),
                (faulty_end, "Run Ended"),
            ],
        )
        os.makedirs(os.path.join(self.root, "Logfile", "Heater Data"), exist_ok=True)
        row = ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "R", ""]
        clean_path = os.path.join(self.root, "Logfile", "Heater Data", "2026_09_10-11-00-00_Clean.txt")
        faulty_path = os.path.join(self.root, "Logfile", "Heater Data", "2026_09_10-12-34-03_Faulty.txt")
        _write_heater_log(clean_path, self.FULL_HEADER, [row, ["30.0"] + row[1:]])
        _write_heater_log(faulty_path, self.FULL_HEADER, [row, ["30.0"] + row[1:]])

        faulty_runs = get_recent_faulty_runs(self._cfg())
        self.assertEqual(len(faulty_runs), 1)
        self.assertEqual(faulty_runs[0]["run_id"], "2026_09_10-12-34-03_Faulty.txt")
        self.assertEqual(faulty_runs[0]["alarm_count"], 1)

    def test_recent_faulty_runs_empty_when_nothing_faulty(self):
        from NEMO_smart_lab.readers import get_recent_faulty_runs

        session_start = datetime(2026, 9, 10, 9, 51, 47)
        run_start = datetime(2026, 9, 10, 12, 34, 3)
        run_end = run_start + timedelta(seconds=30)
        self._write_event_file(session_start, [(session_start, "Program Started"), (run_start, "Run Started"), (run_end, "Run Ended")])
        self._write_run(mtime=run_end.timestamp(), duration_s=30)

        self.assertEqual(get_recent_faulty_runs(self._cfg()), [])

    def test_picks_the_session_active_when_the_run_started_not_a_later_one(self):
        from NEMO_smart_lab.readers import get_heater_log_run_events

        older_session = datetime(2026, 9, 8, 8, 0, 0)
        newer_session = datetime(2026, 9, 11, 8, 0, 0)  # starts *after* the run below
        run_start = datetime(2026, 9, 10, 12, 34, 3)
        run_end = run_start + timedelta(seconds=30)
        self._write_event_file(older_session, [(older_session, "Program Started"), (run_start, "Run Started")])
        self._write_event_file(newer_session, [(newer_session, "Program Started")])
        self._write_run(mtime=run_end.timestamp(), duration_s=30)

        _title, points = get_heater_log_run_events(self._cfg())
        self.assertEqual([p[2] for p in points], ["Run Started"])

    def test_no_matching_session_returns_no_points(self):
        from NEMO_smart_lab.readers import get_heater_log_run_events

        self._write_run(mtime=datetime(2026, 9, 10, 12, 34, 33).timestamp(), duration_s=30)
        _title, points = get_heater_log_run_events(self._cfg())
        self.assertEqual(points, [])


# -------------------- mvd --------------------


MVD_SUM_TEMPLATE = """[Recipe Status]
Recipe name = {recipe}
Recipe status = Recipe running
Completion status = Successfully completed

[Recipe Statistics]
Start time = 2026/01/01 00:00:00
End time = 2026/01/01 00:00:05
Run time = 5.0 sec

[heaters]
HTR6 = "EXHAUST TRAP",80,180,5,3,180,5.000,3.375,0.675,0,100
"""

MVD_DAT_HEADER = ["Time(sec)", "HTR6(C)", "HTR6(%)"]


class MvdTests(TempDirTestCase):
    def _write_run(self, name, recipe, duty, mtime):
        run_dir = os.path.join(self.root, "log", "data", name)
        os.makedirs(run_dir)
        sum_path = os.path.join(run_dir, f"{name}_SUM.txt")
        dat_path = os.path.join(run_dir, f"{name}_DAT.txt")
        with open(sum_path, "w", encoding="utf-8") as f:
            f.write(MVD_SUM_TEMPLATE.format(recipe=recipe))
        with open(dat_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(MVD_DAT_HEADER)
            writer.writerow(["0.5", "100.0", str(duty)])
        os.utime(dat_path, (mtime, mtime))
        return run_dir

    def test_latest_run_picked_by_dat_mtime_not_folder_mtime(self):
        # The run folder's own mtime reflects when it was copied onto disk (e.g. all at once,
        # in an arbitrary order), not when the run actually happened - only the *_DAT.txt
        # file's mtime (written once, at the end of the run) is a reliable recency signal.
        older_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        newer_dir = self._write_run("20260102_000000_B", "Recipe B", duty=0.0, mtime=datetime(2026, 1, 2).timestamp())
        # Deliberately make the folder mtimes disagree with the DAT file mtimes.
        os.utime(older_dir, (datetime(2026, 6, 1).timestamp(),) * 2)
        os.utime(newer_dir, (datetime(2026, 1, 1).timestamp(),) * 2)

        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["recipe"], "Recipe B")

    def test_ordering_uses_the_folder_names_own_timestamp_not_the_dat_files_mtime_either(self):
        # Regression, confirmed live on Oak: a re-upload/re-sync can give even the *_DAT.txt file
        # itself a fresh mtime (not just the folder's) - the run folder's own name, not any mtime
        # at all, must be what decides recency.
        older_dir = self._write_run("20200101_000000_Old", "Old Recipe", duty=0.0, mtime=datetime(2020, 1, 1).timestamp())
        newer_dir = self._write_run("20260101_000000_New", "New Recipe", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        # Both mtimes now lie in the *opposite* direction of what the folder names themselves say.
        os.utime(os.path.join(older_dir, "20200101_000000_Old_DAT.txt"), (datetime(2030, 1, 1).timestamp(),) * 2)
        os.utime(os.path.join(newer_dir, "20260101_000000_New_DAT.txt"), (datetime(2010, 1, 1).timestamp(),) * 2)

        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["recipe"], "New Recipe")

    def test_duty_cycle_above_threshold_is_on(self):
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertTrue(summary["any_on"])
        self.assertEqual(summary["channels"][0]["name"], "EXHAUST TRAP")
        self.assertEqual(summary["channels"][0]["raw_name"], "HTR6")
        # mvd's own HTR numbering already matches the physical/recipe channel number directly -
        # no offset applies here (unlike heater_log-kind tools, see _heater_log_channel_num).
        self.assertEqual(summary["channels"][0]["channel_num"], 6)

    def test_channel_label_override_wins_over_auto_parsed_sum_txt_label(self):
        # _SUM.txt's own "HTR6 = "EXHAUST TRAP"" would normally be used as-is (previous test) -
        # an admin-configured override (keyed by the bare "6", not "HTR6") should win over it.
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5, "channel_labels": {"6": ("Source chuck", "chuck", False, None)}}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["channels"][0]["name"], "Source chuck")
        self.assertEqual(summary["channels"][0]["raw_name"], "HTR6")
        self.assertEqual(summary["channels"][0]["role"], "chuck")

    def test_without_override_falls_back_to_auto_parsed_label(self):
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertIsNone(summary["channels"][0]["role"])


class MvdFaultyRunTests(MvdTests):
    """mvd/fiji5 have no alarm log wired into the history listing the way heater_log does -
    _SUM.txt's own completion_status (confirmed live: "Successfully completed" vs "Recipe stopped
    - Manual stop" are the two common real values) is the equivalent signal for "faulty" here."""

    def _write_run_with_completion_status(self, name, completion_status, mtime):
        run_dir = os.path.join(self.root, "log", "data", name)
        os.makedirs(run_dir)
        sum_text = MVD_SUM_TEMPLATE.format(recipe="Recipe A").replace(
            "Completion status = Successfully completed", f"Completion status = {completion_status}"
        )
        with open(os.path.join(run_dir, f"{name}_SUM.txt"), "w", encoding="utf-8") as f:
            f.write(sum_text)
        dat_path = os.path.join(run_dir, f"{name}_DAT.txt")
        with open(dat_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(MVD_DAT_HEADER)
            writer.writerow(["0.5", "100.0", "0.0"])
        os.utime(dat_path, (mtime, mtime))
        return run_dir

    def _cfg(self):
        return {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}

    def test_manual_stop_is_faulty(self):
        from NEMO_smart_lab.readers import get_recent_faulty_runs

        self._write_run_with_completion_status(
            "20260101_000000_Stopped", "Recipe stopped - Manual stop", mtime=datetime(2026, 1, 1).timestamp()
        )
        faulty_runs = get_recent_faulty_runs(self._cfg())
        self.assertEqual(len(faulty_runs), 1)
        self.assertEqual(faulty_runs[0]["completion_status"], "Recipe stopped - Manual stop")

    def test_successfully_completed_is_not_faulty(self):
        from NEMO_smart_lab.readers import get_recent_faulty_runs

        self._write_run_with_completion_status(
            "20260101_000000_OK", "Successfully completed", mtime=datetime(2026, 1, 1).timestamp()
        )
        self.assertEqual(get_recent_faulty_runs(self._cfg()), [])

    def test_successfully_completed_different_capitalization_is_not_faulty(self):
        # A real bug, caught live against fiji5 (a different mvd-kind tool instance/software
        # version than "mvd" itself): fiji5's own _SUM.txt says "Successfully Completed" (capital
        # C) rather than mvd's "Successfully completed" - a case-sensitive comparison here flagged
        # every single one of fiji5's normal, successful runs as "faulty".
        from NEMO_smart_lab.readers import get_recent_faulty_runs

        self._write_run_with_completion_status(
            "20260101_000000_OK", "Successfully Completed", mtime=datetime(2026, 1, 1).timestamp()
        )
        self.assertEqual(get_recent_faulty_runs(self._cfg()), [])


class ParseMvdDatTests(TempDirTestCase):
    """_parse_mvd_dat has a numpy-based fast path (whole-file bulk parse) for a clean, uniform
    numeric grid, falling back to the original per-cell loop for anything else - some real mvd/
    fiji5 DAT files run past 200,000 rows, where the fast path measured live at ~7x faster. Both
    paths must agree exactly on every input; that's what these tests check."""

    def _write_dat(self, rows_text):
        path = os.path.join(self.root, "test_DAT.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(rows_text)
        return path

    def test_clean_numeric_grid_matches_expected_values(self):
        from NEMO_smart_lab.readers import _parse_mvd_dat

        path = self._write_dat("Time(sec),HTR6(C),HTR6(%)\n0.1,20.0,0.5\n0.2,21.0,0.6\n0.3,22.0,0.7\n")
        time_s, temp_series, duty_series, ramp_rate_series, other_series = _parse_mvd_dat(path)
        self.assertEqual(time_s, [0.1, 0.2, 0.3])
        self.assertEqual(temp_series["6"], [20.0, 21.0, 22.0])
        self.assertEqual(duty_series["6"], [0.5, 0.6, 0.7])

    def test_blank_cell_falls_back_and_still_parses_correctly(self):
        from NEMO_smart_lab.readers import _parse_mvd_dat

        path = self._write_dat("Time(sec),HTR6(C),HTR6(%)\n0.1,20.0,0.5\n0.2,,0.6\n0.3,22.0,0.7\n")
        time_s, temp_series, duty_series, ramp_rate_series, other_series = _parse_mvd_dat(path)
        self.assertEqual(time_s, [0.1, 0.2, 0.3])
        self.assertEqual(temp_series["6"], [20.0, None, 22.0])
        self.assertEqual(duty_series["6"], [0.5, 0.6, 0.7])

    def test_short_row_falls_back_and_still_parses_correctly(self):
        from NEMO_smart_lab.readers import _parse_mvd_dat

        path = self._write_dat("Time(sec),HTR6(C),HTR6(%)\n0.1,20.0,0.5\n0.2,21.0\n0.3,22.0,0.7\n")
        time_s, temp_series, duty_series, ramp_rate_series, other_series = _parse_mvd_dat(path)
        self.assertEqual(time_s, [0.1, 0.2, 0.3])
        self.assertEqual(temp_series["6"], [20.0, 21.0, 22.0])
        self.assertEqual(duty_series["6"], [0.5, None, 0.7])

    def test_large_clean_grid_fast_and_slow_paths_agree(self):
        """Explicitly cross-checks the numpy fast path against the original per-cell algorithm on
        the same, larger, clean input - the strongest guarantee available that this fast path
        can't be silently diverging from the always-correct one it's meant to shortcut."""
        from NEMO_smart_lab.readers import _parse_mvd_dat

        lines = ["Time(sec),HTR6(C),HTR6(%),HTR7(C)"]
        for i in range(2000):
            lines.append(f"{i / 10.0},{20.0 + i * 0.01},{i % 100 / 100.0},{15.0 + i * 0.02}")
        path = self._write_dat("\n".join(lines) + "\n")

        time_s, temp_series, duty_series, ramp_rate_series, other_series = _parse_mvd_dat(path)
        self.assertEqual(len(time_s), 2000)
        self.assertAlmostEqual(time_s[-1], 199.9)
        self.assertAlmostEqual(temp_series["6"][-1], 20.0 + 1999 * 0.01)
        self.assertAlmostEqual(temp_series["7"][-1], 15.0 + 1999 * 0.02)


class MvdPressureAndEventsTests(MvdTests):
    """mvd/fiji5's per-run "<timestamp>_PT.txt" (pressure gauges) and "<timestamp>_EVT.txt"
    (chronological event log) - confirmed live on real Oak data for both mvd (single "Torr" gauge)
    and fiji5 (three "Torr" gauges plus one "psia" gauge, a much larger EVT.txt) - see
    _parse_mvd_pt/_parse_mvd_evt's docstrings."""

    def _write_pt(self, run_dir, name, header_cols, rows):
        # header_cols already contains real, literal '"Name"(Unit)'-style text (matching the real
        # file format confirmed live) - written as a raw line, not through csv.writer, which would
        # otherwise CSV-escape those embedded quote characters (doubling them) instead of leaving
        # them exactly as a real PT.txt file has them.
        with open(os.path.join(run_dir, f"{name}_PT.txt"), "w", encoding="utf-8", newline="") as f:
            f.write(",".join(header_cols) + "\r\n")
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(row)

    def _write_evt(self, run_dir, name, header, lines):
        with open(os.path.join(run_dir, f"{name}_EVT.txt"), "w", encoding="utf-8") as f:
            f.write(header + "\n")
            for line in lines:
                f.write(line + "\n")

    def _cfg(self):
        return {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}

    def test_pressure_group_list_empty_without_a_pt_file(self):
        from NEMO_smart_lab.readers import _mvd_pressure_group_list

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self.assertEqual(_mvd_pressure_group_list(self._cfg()), [])
        self.assertTrue(os.path.isdir(run_dir))

    def test_single_unit_pt_file_is_one_group(self):
        from NEMO_smart_lab.readers import _mvd_pressure_group_list, get_mvd_pressure_group

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(
            run_dir, "20260101_000000",
            ["Time(sec)", '"Reactor"(Torr)', '"OptKitA"(Torr)'],
            [["0.5", "0.1", "9.9"], ["1.0", "0.2", "9.8"]],
        )
        groups = _mvd_pressure_group_list(self._cfg())
        self.assertEqual(groups, [{"key": "pressure_torr", "label": "Pressure (Torr)"}])

        group = get_mvd_pressure_group(self._cfg(), None, "pressure_torr")
        self.assertEqual(set(group["series"]), {"Reactor", "OptKitA"})
        self.assertEqual(group["series"]["Reactor"], ([0.5, 1.0], [0.1, 0.2]))
        self.assertEqual(group["y_label"], "Pressure (Torr)")
        # Multiple gauges on this tab - only the main chamber ("Reactor") should be checked by
        # default (see _mvd_default_visible_pressure_channel); "OptKitA" stays available via the
        # chart's own legend, just unchecked.
        self.assertEqual(group["default_visible"], ["Reactor"])

    def test_single_gauge_pt_file_has_no_default_visible_restriction(self):
        # Only one gauge at all - nothing to default-hide, so every series should show.
        from NEMO_smart_lab.readers import get_mvd_pressure_group

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(run_dir, "20260101_000000", ["Time(sec)", '"Reactor"(Torr)'], [["0.5", "0.1"]])
        group = get_mvd_pressure_group(self._cfg(), None, "pressure_torr")
        self.assertIsNone(group["default_visible"])

    def test_no_recognized_keyword_shows_all_gauges_by_default(self):
        from NEMO_smart_lab.readers import get_mvd_pressure_group

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(
            run_dir, "20260101_000000",
            ["Time(sec)", '"GaugeA"(Torr)', '"GaugeB"(Torr)'],
            [["0.5", "0.1", "0.2"]],
        )
        group = get_mvd_pressure_group(self._cfg(), None, "pressure_torr")
        self.assertIsNone(group["default_visible"])

    def test_fiji5_style_names_default_to_process_chamber(self):
        # fiji5's real gauges (confirmed live): "Process", "Chamber", "Load Lock" - none literally
        # named "Reactor", so the next-priority keyword ("process") should win.
        from NEMO_smart_lab.readers import get_mvd_pressure_group

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(
            run_dir, "20260101_000000",
            ["Time (sec)", '"Process"(Torr)', '"Chamber"(Torr)', '"Load Lock"(Torr)'],
            [["0.5", "0.1", "0.2", "0.3"]],
        )
        group = get_mvd_pressure_group(self._cfg(), None, "pressure_torr")
        self.assertEqual(group["default_visible"], ["Process"])

    def test_mixed_unit_pt_file_is_two_groups(self):
        # fiji5's real shape: three Torr gauges plus one psia gauge - mixed units on one axis
        # would be unreadable (near-vacuum Torr readings vs. ~15 psia), so each unit is its own tab.
        from NEMO_smart_lab.readers import _mvd_pressure_group_list, get_mvd_pressure_group

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(
            run_dir, "20260101_000000",
            ["Time (sec)", '"Chamber"(Torr)', '"LVPD"(psia)'],
            [["0.5", "0.05", "15.1"], ["1.0", "0.06", "15.2"]],
        )
        groups = _mvd_pressure_group_list(self._cfg())
        self.assertEqual(
            groups,
            [{"key": "pressure_torr", "label": "Pressure (Torr)"}, {"key": "pressure_psia", "label": "Pressure (psia)"}],
        )
        torr_group = get_mvd_pressure_group(self._cfg(), None, "pressure_torr")
        self.assertEqual(list(torr_group["series"]), ["Chamber"])
        psia_group = get_mvd_pressure_group(self._cfg(), None, "pressure_psia")
        self.assertEqual(list(psia_group["series"]), ["LVPD"])

    def test_unknown_pressure_group_key_raises(self):
        from NEMO_smart_lab.readers import get_mvd_pressure_group

        self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        with self.assertRaises(ToolDataError):
            get_mvd_pressure_group(self._cfg(), None, "pressure_psia")

    def test_events_parsed_with_offsets_relative_to_run_start(self):
        from NEMO_smart_lab.readers import get_mvd_run_events

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_evt(
            run_dir, "20260101_000000",
            "Date and Time,EventID,EventData",
            [
                "01/01/2026 00:00:00.0000,STATUS; Recipe started.",
                "01/01/2026 00:00:05.5000,MFC; MFC0 set to 20.00 (sccm)",
            ],
        )
        _title, points = get_mvd_run_events(self._cfg())
        self.assertEqual([p[0] for p in points], [0.0, 5.5])
        self.assertEqual(points[0][1], "STATUS")
        self.assertEqual(points[0][2], "Recipe started.")
        self.assertFalse(points[0][3])
        self.assertEqual(points[1][1], "MFC")
        self.assertEqual(points[1][2], "MFC0 set to 20.00 (sccm)")

    def test_message_with_its_own_embedded_comma_is_kept_intact(self):
        # Confirmed live on fiji5: a 4-column header ("...,Recipe Time (sec),EventID,EventData")
        # whose actual free-text message routinely contains its own commas.
        from NEMO_smart_lab.readers import get_mvd_run_events

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_evt(
            run_dir, "20260101_000000",
            "Date and Time,Recipe Time (sec),EventID,EventData",
            ["01/01/2026 00:00:00.0000,0.0,RECIPE; Step #0 - Recipe Line #0, instruction action executed: flow 0 20.000 sccm"],
        )
        _title, points = get_mvd_run_events(self._cfg())
        self.assertEqual(points[0][1], "RECIPE")
        self.assertEqual(points[0][2], "Step #0 - Recipe Line #0, instruction action executed: flow 0 20.000 sccm")

    def test_message_without_a_category_tag_falls_back_to_a_generic_category(self):
        from NEMO_smart_lab.readers import get_mvd_run_events

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_evt(
            run_dir, "20260101_000000",
            "Date and Time,EventID,EventData",
            ["01/01/2026 00:00:00.0000,some message with no category tag at all"],
        )
        _title, points = get_mvd_run_events(self._cfg())
        self.assertEqual(points[0][1], "Event")
        self.assertEqual(points[0][2], "some message with no category tag at all")

    def test_fault_keyword_is_flagged(self):
        from NEMO_smart_lab.readers import get_mvd_run_events

        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_evt(
            run_dir, "20260101_000000",
            "Date and Time,EventID,EventData",
            ["01/01/2026 00:00:00.0000,ALARM; Reactor pressure Fault detected"],
        )
        _title, points = get_mvd_run_events(self._cfg())
        self.assertTrue(points[0][3])

    def test_no_evt_file_returns_no_points_not_an_error(self):
        from NEMO_smart_lab.readers import get_mvd_run_events

        self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        _title, points = get_mvd_run_events(self._cfg())
        self.assertEqual(points, [])

    def test_chart_group_list_includes_pressure_and_events_when_present(self):
        run_dir = self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(run_dir, "20260101_000000", ["Time(sec)", '"Reactor"(Torr)'], [["0.5", "0.1"]])
        self._write_evt(run_dir, "20260101_000000", "Date and Time,EventID,EventData", ["01/01/2026 00:00:00.0000,STATUS; hi"])

        from NEMO_smart_lab.readers import get_chart_group_list

        keys = [g["key"] for g in get_chart_group_list(self._cfg())]
        self.assertIn("pressure_torr", keys)
        self.assertIn("events", keys)
        # Pressure belongs right after temperature (group 0), not appended after every other
        # group (duty/flow/power/etc.) and just before "events" - a real ordering request.
        self.assertEqual(keys[0], "temperature")
        self.assertEqual(keys[1], "pressure_torr")

    def test_chart_group_list_omits_pressure_and_events_when_absent(self):
        self._write_run("20260101_000000_A", "Recipe A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        from NEMO_smart_lab.readers import get_chart_group_list

        keys = [g["key"] for g in get_chart_group_list(self._cfg())]
        self.assertNotIn("events", keys)
        self.assertFalse(any(k.startswith("pressure") for k in keys))


class BasePressureHistoryHeaterLogTests(SiblingRunDataTests):
    """get_base_pressure_history() for heater_log-kind tools - averages the last window_s seconds
    of each matching standby run's sibling Pressure Data file. Matches by the *exact* recipe name
    embedded in the run's own filename (SmartLabTool.base_pressure_recipe_names), never a keyword -
    confirmed live a tool can have several standby-ish variants (e.g. one that also runs a valve
    clean pass) with genuinely different baseline pressure."""

    def _write_heater_run(self, filename, pressure_rows):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])
        d = os.path.join(self.root, "Logfile", "Pressure Data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
            f.write("\tPressure Time\tPressure \tCycles Remaining\tRecipe\tLoop\n")
            for t, p in pressure_rows:
                f.write(f"\t{t}\t{p}\t0\tSTANDBY\t\n")

    def _cfg_with_target(self, target):
        cfg = self._cfg()
        cfg["base_pressure_recipe_names"] = target
        return cfg

    def test_averages_the_last_window_seconds_of_a_matching_run(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run(
            "2026_01_01-00-00-00_STANDBY.txt",
            [(0.0, 1.0), (20.0, 1.0), (25.0, 0.2), (30.0, 0.2), (35.0, 0.2)],
        )
        results = get_base_pressure_history(self._cfg_with_target("STANDBY"), window_s=10.0)
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["value"], 0.2)
        self.assertEqual(results[0]["unit"], "Torr")
        self.assertEqual(results[0]["timestamp"], datetime(2026, 1, 1, 0, 0, 35))

    def test_non_matching_recipe_is_skipped_without_being_fetched(self):
        # A run whose filename says a *different* recipe must never even have its Pressure Data
        # file opened - only the exact configured recipe's runs are ever touched.
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_Al2O3 - STANDARD.txt", [(0.0, 5.0)])
        results = get_base_pressure_history(self._cfg_with_target("STANDBY"))
        self.assertEqual(results, [])

    def test_standby_variant_with_a_different_exact_name_is_not_conflated(self):
        # "STANDBY" and "STANDBY - Valve Clean" are different recipes with potentially different
        # baseline pressure - only an exact match counts, confirmed live these coexist on real
        # tools.
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        self._write_heater_run("2026_01_02-00-00-00_STANDBY - Valve Clean.txt", [(0.0, 0.9)])
        results = get_base_pressure_history(self._cfg_with_target("STANDBY"))
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["value"], 0.2)

    def test_multiple_matching_runs_are_sorted_oldest_first(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_02-00-00-00_STANDBY.txt", [(0.0, 0.3)])
        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        results = get_base_pressure_history(self._cfg_with_target("STANDBY"))
        self.assertEqual([r["timestamp"].day for r in results], [1, 2])

    def test_no_target_recipe_configured_returns_empty(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        self.assertEqual(get_base_pressure_history(self._cfg()), [])

    def test_matching_is_case_insensitive_and_trims_whitespace(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_Standby.txt", [(0.0, 0.2)])
        results = get_base_pressure_history(self._cfg_with_target("  STANDBY  "))
        self.assertEqual(len(results), 1)


class BasePressureHistoryMvdTests(MvdPressureAndEventsTests):
    """get_base_pressure_history() for mvd/fiji5 - same exact-recipe-match semantics as
    heater_log, but reading the run's own _PT.txt and picking the primary chamber gauge (see
    _mvd_default_visible_pressure_channel) rather than a single fixed "Pressure" column."""

    def _cfg_with_target(self, target):
        cfg = self._cfg()
        cfg["base_pressure_recipe_names"] = target
        return cfg

    def test_averages_the_last_window_seconds_using_the_primary_gauge(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        run_dir = self._write_run("20260101_000000_STANDBY", "STANDBY", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(
            run_dir, "20260101_000000",
            ["Time(sec)", '"Reactor"(Torr)', '"OptKitA"(Torr)'],
            [["0.0", "1.0", "9.9"], ["20.0", "0.3", "9.9"], ["30.0", "0.3", "9.9"]],
        )
        results = get_base_pressure_history(self._cfg_with_target("STANDBY"), window_s=10.0)
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["value"], 0.3)
        self.assertEqual(results[0]["timestamp"], datetime(2026, 1, 1, 0, 0, 30))

    def test_no_pt_file_is_skipped_not_an_error(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_run("20260101_000000_STANDBY", "STANDBY", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self.assertEqual(get_base_pressure_history(self._cfg_with_target("STANDBY")), [])

    def test_non_matching_recipe_is_skipped(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        run_dir = self._write_run("20260101_000000_Al2O3_40_cycles", "Al2O3", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        self._write_pt(run_dir, "20260101_000000", ["Time(sec)", '"Reactor"(Torr)'], [["0.0", "1.0"]])
        self.assertEqual(get_base_pressure_history(self._cfg_with_target("STANDBY")), [])


class BasePressureHistoryMultipleRecipesTests(SiblingRunDataTests):
    """base_pressure_recipe_names is a *list* - several distinct standby variants can legitimately
    all be tracked together (confirmed live), unlike the earlier single-recipe design."""

    def _write_heater_run(self, filename, pressure_rows):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])
        d = os.path.join(self.root, "Logfile", "Pressure Data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
            f.write("\tPressure Time\tPressure \tCycles Remaining\tRecipe\tLoop\n")
            for t, p in pressure_rows:
                f.write(f"\t{t}\t{p}\t0\tSTANDBY\t\n")

    def test_matches_any_recipe_in_the_comma_separated_list(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        self._write_heater_run("2026_01_02-00-00-00_STANDBY - Valve Clean.txt", [(0.0, 0.9)])
        cfg = self._cfg()
        cfg["base_pressure_recipe_names"] = "STANDBY, STANDBY - Valve Clean"
        results = get_base_pressure_history(cfg)
        self.assertEqual(len(results), 2)
        self.assertEqual({round(r["value"], 1) for r in results}, {0.2, 0.9})

    def test_extra_whitespace_and_blank_entries_in_the_list_are_tolerated(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        cfg = self._cfg()
        cfg["base_pressure_recipe_names"] = "  STANDBY ,, "
        results = get_base_pressure_history(cfg)
        self.assertEqual(len(results), 1)


class GetLatestRunIdTests(SiblingRunDataTests):
    """get_latest_run_id() - used to tell whether an explicit ?run=<id> on the tool detail page
    happens to be the tool's own actual latest run (reached via the overview page's "View full
    details" link) rather than a genuinely earlier one, so the "Viewing a past run" banner isn't
    shown for it."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_returns_the_most_recent_run(self):
        from NEMO_smart_lab.readers import get_latest_run_id

        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        self._write_heater_run("2026_01_02-00-00-00_B.txt")
        self.assertEqual(get_latest_run_id(self._cfg()), "2026_01_02-00-00-00_B.txt")

    def test_none_with_no_runs_at_all(self):
        from NEMO_smart_lab.readers import get_latest_run_id

        self.assertIsNone(get_latest_run_id(self._cfg()))


class GetRunPageNumberTests(SiblingRunDataTests):
    """get_run_page_number() - lets a past run's own detail page's "View run history" link jump
    straight to the history page that run is actually on, instead of always landing on page 1."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_first_page(self):
        from NEMO_smart_lab.readers import get_run_page_number

        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_02-00-00-00_B.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        # Newest-first: C is index 0, on page 1 with page_size=2.
        self.assertEqual(get_run_page_number(self._cfg(), "2026_01_03-00-00-00_C.txt", page_size=2), 1)

    def test_later_page(self):
        from NEMO_smart_lab.readers import get_run_page_number

        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_02-00-00-00_B.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        # Newest-first: A is index 2, which is page 2 with page_size=2 (index // page_size + 1).
        self.assertEqual(get_run_page_number(self._cfg(), "2026_01_01-00-00-00_A.txt", page_size=2), 2)

    def test_none_for_unknown_run_id(self):
        from NEMO_smart_lab.readers import get_run_page_number

        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        self.assertIsNone(get_run_page_number(self._cfg(), "does_not_exist.txt", page_size=25))


class MvdChartGroupsTests(TempDirTestCase):
    """get_chart_groups() for mvd-kind tools classifies every DAT column generically by its own
    unit suffix (see readers._UNIT_SUFFIX_RE) - covers fiji5's much richer column set (ramp
    rates, MFC setpoint/reading, plasma power, turbo speed, chuck bias, match network) without
    hardcoding any of those column names."""

    def _write_run(self, header, row):
        run_dir = os.path.join(self.root, "log", "data", "20260101_000000_A")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "20260101_000000_A_SUM.txt"), "w", encoding="utf-8") as f:
            f.write(MVD_SUM_TEMPLATE.format(recipe="Recipe A"))
        with open(os.path.join(run_dir, "20260101_000000_A_DAT.txt"), "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerow(row)

    def _cfg(self):
        return {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}

    def test_full_column_classification(self):
        header = [
            "Time(sec)", "HTR6(C)", "HTR6(%)", "HTR6_RR(C)",
            "MFC0_setpoint(sccm)", "MFC0_reading(sccm)",
            "PlasmaForwardPower(W)", "ReactorTurboSpeed(rpm)", "ChuckBiasVoltageSetpoint(V)",
            "LoadSetpoint(%)", "xAxisTorque",
        ]
        row = ["0.5", "100.0", "50.0", "2.0", "10.0", "9.8", "300.0", "1000.0", "5.0", "80.0", "0.1"]
        self._write_run(header, row)
        groups = get_chart_groups(self._cfg())
        by_key = {g["key"]: g for g in groups}

        self.assertEqual(list(by_key["temperature"]["series"].values())[0][1], [100.0])
        self.assertEqual(list(by_key["duty"]["series"].values())[0][1], [50.0])
        self.assertEqual(list(by_key["ramp_rate"]["series"].values())[0][1], [2.0])
        self.assertEqual(set(by_key["sccm"]["series"].keys()), {"MFC0_setpoint", "MFC0_reading"})
        self.assertEqual(by_key["W"]["series"]["PlasmaForwardPower"][1], [300.0])
        self.assertEqual(by_key["rpm"]["series"]["ReactorTurboSpeed"][1], [1000.0])
        self.assertEqual(by_key["V"]["series"]["ChuckBiasVoltageSetpoint"][1], [5.0])
        # A non-heater "%" column is its own group, never merged into "duty".
        self.assertEqual(by_key["percent_other"]["series"]["LoadSetpoint"][1], [80.0])
        self.assertNotIn("LoadSetpoint", by_key["duty"]["series"])
        # A column with no parenthesized unit at all falls into a final catch-all.
        self.assertEqual(by_key["other"]["series"]["xAxisTorque"][1], [0.1])

    def test_group_omitted_when_column_present_but_every_value_is_blank(self):
        header = ["Time(sec)", "HTR6(C)", "HTR6(%)", "HTR6_RR(C)"]
        row = ["0.5", "100.0", "50.0", ""]
        self._write_run(header, row)
        groups = get_chart_groups(self._cfg())
        keys = {g["key"] for g in groups}
        self.assertEqual(keys, {"temperature", "duty"})
        self.assertNotIn("ramp_rate", keys)

    def test_group_absent_from_header_entirely_is_simply_not_present(self):
        header = ["Time(sec)", "HTR6(C)", "HTR6(%)"]
        row = ["0.5", "100.0", "50.0"]
        self._write_run(header, row)
        groups = get_chart_groups(self._cfg())
        keys = {g["key"] for g in groups}
        self.assertEqual(keys, {"temperature", "duty"})
        self.assertNotIn("other", keys)


# -------------------- cobra_job --------------------


def _new_guid_bytes():
    return uuid.uuid4().bytes_le


class CobraJobTests(TempDirTestCase):
    def _build_db(self, status="Finished", recipe="Test Recipe"):
        db_dir = os.path.join(self.root, "Databases-Data")
        os.makedirs(db_dir)
        db_path = os.path.join(db_dir, "Jobs.db")
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("CREATE TABLE Jobs (TaskID BLOB, LotID TEXT, StartDate TEXT, EndDate TEXT, Status TEXT, Recipe TEXT)")
        cur.execute("CREATE TABLE Wafers (WaferID BLOB, TaskID BLOB, Name TEXT, Status TEXT)")
        cur.execute("CREATE TABLE WaferActions (ID INTEGER PRIMARY KEY, ActionID BLOB, WaferID BLOB, ActionType TEXT)")
        cur.execute("CREATE TABLE RecipeStepEntries (ID INTEGER PRIMARY KEY, ActionID BLOB, StepID INTEGER, StartTime TEXT, EndTime TEXT, Name TEXT)")
        cur.execute("CREATE TABLE RecipePhaseEntries (PhaseID INTEGER, StepID INTEGER, StartTime TEXT, EndTime TEXT, Name TEXT)")

        task_id = _new_guid_bytes()
        wafer_id = _new_guid_bytes()
        action_id = _new_guid_bytes()

        cur.execute(
            "INSERT INTO Jobs VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "LOT1", "2026-01-01 00:00:00.000000Z", "2026-01-01 00:05:00.000000Z", status, recipe),
        )
        cur.execute("INSERT INTO Wafers VALUES (?, ?, ?, ?)", (wafer_id, task_id, "W1", "Complete"))
        cur.execute("INSERT INTO WaferActions (ActionID, WaferID, ActionType) VALUES (?, ?, ?)", (action_id, wafer_id, "Process"))
        cur.execute(
            "INSERT INTO RecipeStepEntries (ActionID, StepID, StartTime, EndTime, Name) VALUES (?, ?, ?, ?, ?)",
            (action_id, 1, "2026-01-01 00:00:00.000000Z", "2026-01-01 00:02:00.000000Z", "Etch"),
        )
        step_row_id = cur.lastrowid
        cur.execute(
            "INSERT INTO RecipePhaseEntries VALUES (?, ?, ?, ?, ?)",
            (1, step_row_id, "2026-01-01 00:00:00.000000Z", "2026-01-01 00:01:00.000000Z", "Dose"),
        )
        con.commit()
        con.close()
        return str(uuid.UUID(bytes_le=task_id))

    def test_reads_most_recent_job(self):
        self._build_db(status="Finished", recipe="MaN Etch")
        cfg = {"kind": "cobra_job", "root": self.root}
        summary = get_tool_summary("cobra-test", cfg)
        self.assertNotIn("error", summary)
        self.assertEqual(summary["recipe"], "MaN Etch")
        self.assertEqual(summary["status_class"], "success")
        self.assertEqual(summary["extra"]["step_count"], 1)
        self.assertEqual(summary["extra"]["phase_count"], 1)

    def test_aborted_status_is_danger(self):
        self._build_db(status="Aborted")
        cfg = {"kind": "cobra_job", "root": self.root}
        summary = get_tool_summary("cobra-test", cfg)
        self.assertEqual(summary["status_class"], "danger")

    def test_specific_run_id_is_a_valid_guid_lookup(self):
        task_id = self._build_db()
        cfg = {"kind": "cobra_job", "root": self.root}
        summary = get_tool_summary("cobra-test", cfg, run_id=task_id)
        self.assertNotIn("error", summary)
        self.assertEqual(summary["run_id"], task_id)

    def test_unknown_guid_is_not_found(self):
        self._build_db()
        cfg = {"kind": "cobra_job", "root": self.root}
        summary = get_tool_summary("cobra-test", cfg, run_id=str(uuid.uuid4()))
        self.assertIn("error", summary)


# -------------------- eventlog --------------------


EVENTLOG_FIELDS = ["Date/Time", "Module", "Event", "Info"]


class EventLogTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.root, "EventLog-Data"))
        self.path = os.path.join(self.root, "EventLog-Data", "CurrentEvents.csv")

    def _write_rows(self, rows):
        # File is newest-first, matching the real tool's log ordering.
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=EVENTLOG_FIELDS)
            writer.writeheader()
            writer.writerows(reversed(rows))

    def test_clean_run_bounded_correctly(self):
        rows = [
            {"Date/Time": "01/01/2026 10:00:00", "Module": "Rapier", "Event": "State Changed To Ready", "Info": ""},
            {"Date/Time": "01/01/2026 10:00:01", "Module": "Rapier", "Event": "DO PROCESS", "Info": "Recipe: Foo - Wafer: W1"},
            {"Date/Time": "01/01/2026 10:00:05", "Module": "Rapier", "Event": "Some Step", "Info": ""},
            {"Date/Time": "01/01/2026 10:01:00", "Module": "Rapier", "Event": "State Changed To Idle", "Info": ""},
        ]
        self._write_rows(rows)
        cfg = {"kind": "eventlog", "root": self.root}
        summary = get_tool_summary("kla-test", cfg)
        self.assertNotIn("error", summary)
        self.assertEqual(summary["recipe"], "Foo")
        self.assertEqual(summary["extra"]["wafer"], "W1")
        self.assertEqual(summary["run_duration_s"], 59.0)
        self.assertEqual(summary["status_label"], "Clean run")
        self.assertFalse(summary["any_on"])

    def test_fault_during_run_is_flagged(self):
        rows = [
            {"Date/Time": "01/01/2026 10:00:00", "Module": "Rapier", "Event": "State Changed To Ready", "Info": ""},
            {"Date/Time": "01/01/2026 10:00:01", "Module": "Rapier", "Event": "DO PROCESS", "Info": "Recipe: Foo - Wafer: W1"},
            {"Date/Time": "01/01/2026 10:00:03", "Module": "Rapier", "Event": "Pressure Fault", "Info": "high pressure"},
            {"Date/Time": "01/01/2026 10:01:00", "Module": "Rapier", "Event": "State Changed To Aborted", "Info": ""},
        ]
        self._write_rows(rows)
        cfg = {"kind": "eventlog", "root": self.root}
        summary = get_tool_summary("kla-test", cfg)
        self.assertTrue(summary["any_on"])
        self.assertEqual(summary["status_class"], "danger")
        self.assertEqual(len(summary["channels"]), 1)

    def test_history_orders_newest_first(self):
        rows = [
            {"Date/Time": "01/01/2026 09:00:00", "Module": "Rapier", "Event": "State Changed To Ready", "Info": ""},
            {"Date/Time": "01/01/2026 09:00:01", "Module": "Rapier", "Event": "DO PROCESS", "Info": "Recipe: First - Wafer: W1"},
            {"Date/Time": "01/01/2026 09:01:00", "Module": "Rapier", "Event": "State Changed To Idle", "Info": ""},
            {"Date/Time": "01/01/2026 10:00:00", "Module": "Rapier", "Event": "State Changed To Ready", "Info": ""},
            {"Date/Time": "01/01/2026 10:00:01", "Module": "Rapier", "Event": "DO PROCESS", "Info": "Recipe: Second - Wafer: W2"},
            {"Date/Time": "01/01/2026 10:01:00", "Module": "Rapier", "Event": "State Changed To Idle", "Info": ""},
        ]
        self._write_rows(rows)
        cfg = {"kind": "eventlog", "root": self.root}
        history, total = get_tool_history(cfg, page=1, page_size=10)
        self.assertEqual(total, 2)
        self.assertEqual(history[0]["recipe"], "Second")
        self.assertEqual(history[1]["recipe"], "First")


# -------------------- Lazy remote-caching integration (see NEMO_smart_lab.remote_cache) --------------------


class RemoteModeHeaterLogTests(TestCase):
    """Proves readers.py's lazy-caching wiring actually works end to end: "most recent run" is
    resolved from a *remote* listing (never touching local mtimes, which would silently miss a
    run that was never fetched before), and only that one winning run is ever fetched - all
    without ever having run `sync_remote_data` first. Every network call
    (NEMO_smart_lab.remote_sync.list_remote/sync_file_from_remote) is mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1",
            kind="heater_log",
            local_root=self._tmp.name,
            sync_endpoint=endpoint,
            remote_subdir="Fiji1",
            on_threshold_c=35.0,
        )

    def test_most_recent_run_resolved_from_remote_listing_and_fetched_on_demand(self):
        raw_listing = (
            "drwxr-sr-x       4,096 2026/05/01 00:00:00 .\n"
            "-rwxr-xr-x       1,000 2026/05/01 00:00:00 old_run.txt\n"
            "-rwxr-xr-x       1,000 2026/06/01 00:00:00 new_run.txt\n"
        )
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "New Recipe", ""]

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            _write_heater_log(local_path, HeaterLogTests.FULL_HEADER, [row])
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=raw_listing),
            patch(
                "NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file
            ) as mock_sync,
        ):
            cfg = self.tool.as_source_config()
            self.assertIn("remote_tool", cfg)
            summary = get_tool_summary("fiji1", cfg)

        self.assertNotIn("error", summary)
        self.assertEqual(summary["run_id"], "new_run.txt")
        self.assertEqual(summary["recipe"], "New Recipe")
        # Only the winning run was ever fetched - "old_run.txt" was listed but never downloaded.
        mock_sync.assert_called_once()
        self.assertIn("new_run.txt", mock_sync.call_args[0][2])
        self.assertNotIn("old_run.txt", mock_sync.call_args[0][2])


class RemoteListingFailureTests(TestCase):
    """A transient remote-host failure (Oak unreachable, DNS blip, etc.) while listing a tool's
    runs must degrade to a friendly "error" on the summary, not an unhandled 500 - confirmed live:
    a real DNS resolution failure against dtn.oak.stanford.edu crashed the tool overview page with
    a raw RemoteSyncError traceback, because _list_heater_log_entries (unlike the rest of this
    module's remote_cache callers) didn't convert that into the ToolDataError get_tool_summary
    already knows how to catch. Covers the fix for all three of the listing helpers that had this
    same gap (heater_log/mvd/waferlog all list via remote_cache.list_remote_dir the same way)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji2",
            kind="heater_log",
            local_root=self._tmp.name,
            sync_endpoint=endpoint,
            remote_subdir="Fiji2",
            on_threshold_c=35.0,
        )

    def test_dns_failure_listing_runs_yields_friendly_error_not_a_crash(self):
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.list_remote",
            side_effect=remote_sync.RemoteSyncError(
                "rsync exited 255: ssh: Could not resolve hostname dtn.oak.stanford.edu: Temporary failure in name resolution"
            ),
        ):
            cfg = self.tool.as_source_config()
            summary = get_tool_summary("fiji2", cfg)

        self.assertIn("error", summary)
        self.assertIn("dtn.oak.stanford.edu", summary["error"])

    def test_dns_failure_listing_runs_history_page_degrades_to_empty_not_a_crash(self):
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.list_remote",
            side_effect=remote_sync.RemoteSyncError("name resolution failure"),
        ):
            cfg = self.tool.as_source_config()
            runs, total = get_tool_history(cfg)

        self.assertEqual(runs, [])
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
