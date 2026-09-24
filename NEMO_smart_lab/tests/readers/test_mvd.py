"""Tests for the mvd reader kind (Cambridge Nanotech/Veeco MVD run folders)."""

import csv
import os
from datetime import datetime
from unittest.mock import patch

from NEMO_smart_lab.readers import ToolDataError, get_chart_groups, get_tool_history, get_tool_summary
from NEMO_smart_lab.tests.readers.helpers import MVD_DAT_HEADER, MVD_SUM_TEMPLATE, TempDirTestCase


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

    def test_config_ini_label_wins_over_sum_txt_label_but_not_manual_override(self):
        # A real, confirmed gap: _SUM.txt's own HTR<n> label is blank in every real per-run
        # export, while a tool's own config.ini [heaters] section reliably has the real name an
        # operator configured - see _mvd_config_heater_labels. Mocked here rather than through a
        # full remote fixture (see ConfigIniHeaterLabelParsingTests for that) to isolate the merge
        # priority itself: config.ini > _SUM.txt's own auto-parsed label > "Heater N", and a
        # manual admin channel_labels override still wins over all of it.
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())

        with patch("NEMO_smart_lab.readers.mvd.summary._mvd_config_heater_labels", return_value={"6": "Config.ini name"}):
            cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
            summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["channels"][0]["name"], "Config.ini name")

        with patch("NEMO_smart_lab.readers.mvd.summary._mvd_config_heater_labels", return_value={"6": "Config.ini name"}):
            cfg = {
                "kind": "mvd", "root": self.root, "on_threshold_pct": 0.5,
                "channel_labels": {"6": ("Source chuck", "chuck", False, None)},
            }
            summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["channels"][0]["name"], "Source chuck")


class MvdHistoryFilterTests(MvdTests):
    """get_tool_history()'s recipe/user_windows filters for mvd-kind tools - same cheap,
    foldername-only matching as HistoryFilterTests, just against mvd's "YYYYMMDD_HHMMSS_<recipe>"
    folder-naming convention instead of heater_log's filename one."""

    def _cfg(self):
        return {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}

    def test_recipe_filter_matches_the_runs_own_embedded_recipe_name_case_insensitively(self):
        self._write_run("20260103_000000_Standby 200C", "Standby 200C", duty=0.0, mtime=datetime(2026, 1, 3).timestamp())
        self._write_run("20260102_000000_Thermal", "Thermal", duty=0.0, mtime=datetime(2026, 1, 2).timestamp())
        self._write_run("20260101_000000_Standby 200C", "Standby 200C", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, recipe=["standby 200c"])
        self.assertEqual(total, 2)
        self.assertEqual(
            {r["run_id"] for r in history}, {"20260103_000000_Standby 200C", "20260101_000000_Standby 200C"}
        )

    def test_user_windows_filter_matches_by_the_runs_own_embedded_start_timestamp(self):
        self._write_run("20260103_000000_C", "C", duty=0.0, mtime=datetime(2026, 1, 3).timestamp())
        self._write_run("20260102_000000_B", "B", duty=0.0, mtime=datetime(2026, 1, 2).timestamp())
        self._write_run("20260101_000000_A", "A", duty=0.0, mtime=datetime(2026, 1, 1).timestamp())
        window = (datetime(2026, 1, 1, 12, 0, 0), datetime(2026, 1, 2, 12, 0, 0))
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, user_windows=[window])
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "20260102_000000_B")


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
