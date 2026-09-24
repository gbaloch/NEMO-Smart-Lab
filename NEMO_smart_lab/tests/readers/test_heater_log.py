"""Tests for the heater_log reader kind (Veeco Fiji/Savannah): runs, parsing, events, sibling logs, config-derived labels."""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.readers import get_chart_groups, get_tool_history, get_tool_summary
from NEMO_smart_lab.tests.readers.helpers import TempDirTestCase, _write_heater_log


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

    def test_latest_run_skips_header_only_files_left_by_aborted_runs(self):
        # Regression: savannah's newest files were 188-byte header-only stubs (runs that never got
        # going), which made the whole tool's summary an error instead of falling back to its
        # newest run that actually has data.
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "Real Recipe", ""]
        heater_dir = os.path.join(self.root, "Logfile", "Heater Data")
        _write_heater_log(os.path.join(heater_dir, "2026_09_01-10-00-00_Real Recipe.txt"), self.FULL_HEADER, [row])
        _write_heater_log(os.path.join(heater_dir, "2026_09_17-14-00-00_.txt"), self.FULL_HEADER, [])
        _write_heater_log(os.path.join(heater_dir, "2026_09_17-13-40-00_.txt"), self.FULL_HEADER, [])
        summary = get_tool_summary("savannah-test", self._cfg())
        self.assertNotIn("error", summary)
        self.assertEqual(summary["recipe"], "Real Recipe")

    def test_only_header_only_files_still_reports_the_empty_file_error(self):
        heater_dir = os.path.join(self.root, "Logfile", "Heater Data")
        _write_heater_log(os.path.join(heater_dir, "2026_09_17-14-00-00_.txt"), self.FULL_HEADER, [])
        summary = get_tool_summary("savannah-test", self._cfg())
        self.assertIn("no data rows", summary["error"])

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


class HeaterLogConfigMfcLabelTests(TestCase):
    """_heater_log_config_mfc_label() - the run log's own "MFC 1" column is confirmed live to
    always be this one specific, tool-configured MFC channel (fiji1/2/3/savannah can physically
    have several more, but only ever stream this one's continuous flow into the log - see that
    function's own docstring) - reads Setup.ini.txt's real label for it instead of the generic,
    physically-ambiguous "MFC 1" the log file's own header always says."""

    ROOT_LISTING = (
        "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
        "-rwxr-xr-x         9,026 2026/08/21 07:17:00 Setup.ini.txt\n"
        "-rwxr-xr-x         9,120 2020/08/27 00:00:00 Setup.ini - Copy.txt\n"
    )

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji1", config_subdir=".",
        )

    def _mock_setup_ini(self, text):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(text)
            return "ok"

        return (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=self.ROOT_LISTING),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        )

    def test_parses_the_real_mfc1label_field(self):
        from NEMO_smart_lab.readers import _heater_log_config_mfc_label

        text = (
            "[MFC]\n"
            'MFC0LABEL="MFC 0 Ar Carrier (sccm)"\n'
            'MFC1LABEL="MFC 1 Ar Plasma (sccm)"\n'
            'MFC2LABEL="MFC 2 N2 Plasma (sccm)"\n'
        )
        mock_list, mock_sync = self._mock_setup_ini(text)
        with mock_list, mock_sync:
            label = _heater_log_config_mfc_label(self.tool.as_source_config())
        self.assertEqual(label, "MFC 1 Ar Plasma (sccm)")

    def test_ignores_the_stale_copy_and_reads_only_setup_ini_txt(self):
        from NEMO_smart_lab.readers import _heater_log_config_mfc_label

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            content = 'MFC1LABEL="Stale copy"\n' if "Copy" in remote_relpath_full else 'MFC1LABEL="Live label"\n'
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(content)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=self.ROOT_LISTING),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            label = _heater_log_config_mfc_label(self.tool.as_source_config())
        self.assertEqual(label, "Live label")

    def test_none_when_field_is_missing(self):
        from NEMO_smart_lab.readers import _heater_log_config_mfc_label

        mocks = self._mock_setup_ini("[MFC]\nMFC0LABEL=\"MFC 0 Ar Carrier (sccm)\"\n")
        with mocks[0], mocks[1]:
            self.assertIsNone(_heater_log_config_mfc_label(self.tool.as_source_config()))

    def test_none_without_config_subdir_set(self):
        from NEMO_smart_lab.readers import _heater_log_config_mfc_label

        no_config_tool = SmartLabTool.objects.create(
            name="fiji2", kind="heater_log", local_root=self._tmp.name, sync_endpoint=self.endpoint, remote_subdir="Fiji2",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote") as mock_list:
            self.assertIsNone(_heater_log_config_mfc_label(no_config_tool.as_source_config()))
        mock_list.assert_not_called()

    def test_none_for_non_heater_log_kind(self):
        from NEMO_smart_lab.readers import _heater_log_config_mfc_label

        self.assertIsNone(_heater_log_config_mfc_label({"kind": "mvd", "config_subdir": "."}))
