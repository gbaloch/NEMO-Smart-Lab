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

from NEMO_smart_lab.readers import ToolDataError, get_tool_history, get_tool_summary


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
        cfg["channel_labels"] = {"Heater 10": ("Source chuck", "chuck")}
        summary = get_tool_summary("fiji-test", cfg)
        by_raw = {c["raw_name"]: c for c in summary["channels"]}
        self.assertEqual(by_raw["Heater 10"]["name"], "Source chuck")
        self.assertEqual(by_raw["Heater 10"]["role"], "chuck")
        # Untouched channels keep their raw name and a None role.
        self.assertEqual(by_raw["Heater 6"]["name"], "Heater 6")
        self.assertIsNone(by_raw["Heater 6"]["role"])


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
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5, "channel_labels": {"6": ("Source chuck", "chuck")}}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertEqual(summary["channels"][0]["name"], "Source chuck (HTR6)")
        self.assertEqual(summary["channels"][0]["role"], "chuck")

    def test_without_override_falls_back_to_auto_parsed_label(self):
        self._write_run("20260101_000000_A", "Recipe A", duty=12.5, mtime=datetime(2026, 1, 1).timestamp())
        cfg = {"kind": "mvd", "root": self.root, "on_threshold_pct": 0.5}
        summary = get_tool_summary("mvd-test", cfg)
        self.assertIsNone(summary["channels"][0]["role"])


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


if __name__ == "__main__":
    unittest.main()
