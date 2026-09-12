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

    def test_mfc_flow_is_a_second_chart_group(self):
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

    def test_duty_cycle_above_threshold_is_on(self):
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertTrue(summary["any_on"])
        self.assertEqual(summary["channels"][0]["name"], 'EXHAUST TRAP (HTR6)')

    def test_channel_label_override_wins_over_auto_parsed_sum_txt_label(self):
        # _SUM.txt's own "HTR6 = "EXHAUST TRAP"" would normally be used as-is (previous test) -
        # an admin-configured override (keyed by the bare "6", not "HTR6") should win over it.
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5, "channel_labels": {"6": ("Source chuck", "chuck", False, None)}}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["channels"][0]["name"], "Source chuck (HTR6)")
        self.assertEqual(summary["channels"][0]["role"], "chuck")

    def test_without_override_falls_back_to_auto_parsed_label(self):
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertIsNone(summary["channels"][0]["role"])


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


if __name__ == "__main__":
    unittest.main()
