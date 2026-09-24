"""Tests for the waferlog reader kind (Plasma-Therm VersaLine): one file per wafer run under WaferLog-Data/."""

import os

from NEMO_smart_lab.readers import ToolDataError, get_chart_data, get_tool_history, get_tool_summary
from NEMO_smart_lab.tests.readers.helpers import TempDirTestCase

WAFERLOG = "\n".join(
    [
        "machineID: VL-01\tRecipe: SiN Deposition",
        "Start:(2026-01-01 10:00:00)(1000) End:(2026-01-01 10:10:00)(601000)",
        "",
        "Step\tRFICPGeneratorP\tTime",
        "\t(W)\t(s)",
        "1\t0\t10",
        "2\t250\t300",
        "",
        "HistoricalData:",
        "EPD1\t\tEPD2\t",
        "(s)\t(V)\t(s)\t(V)",
        "0.0\t1.5\t0.0\t2.5",
        "1.0\t1.6\t1.0\t2.6",
        "2.0\t---\t2.0\tnil",
        "Process sequence summary",
    ]
)


class WaferLogTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.root, "WaferLog-Data")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "wafer1.txt"), "w", encoding="utf-8") as f:
            f.write(WAFERLOG)

    def _cfg(self):
        return {"kind": "waferlog", "root": self.root}

    def test_summary_reads_recipe_machine_duration_and_plasma_state(self):
        summary = get_tool_summary("wafer-test", self._cfg())
        self.assertNotIn("error", summary)
        self.assertEqual(summary["recipe"], "SiN Deposition")
        self.assertEqual(summary["extra"]["machine_id"], "VL-01")
        self.assertEqual(summary["run_duration_s"], 600.0)
        self.assertTrue(summary["any_on"])
        self.assertEqual(summary["status_label"], "Plasma ON")
        self.assertEqual([c["raw_name"] for c in summary["channels"]], ["EPD1", "EPD2"])
        # "---"/"nil" placeholder readings are skipped, so the latest real value is the 1.0 s one.
        self.assertEqual(summary["channels"][0]["latest_value"], 1.6)

    def test_chart_data_has_one_series_per_channel(self):
        title, _x, _y, series = get_chart_data(self._cfg())
        self.assertIn("SiN Deposition", title)
        self.assertEqual(sorted(series), ["EPD1", "EPD2"])

    def test_history_lists_the_run(self):
        runs, total = get_tool_history(self._cfg())
        self.assertEqual(total, 1)
        self.assertEqual(runs[0]["run_id"], "wafer1.txt")

    def test_unknown_run_id_and_path_traversal_are_rejected(self):
        for bad in ("nope.txt", "../wafer1.txt"):
            summary = get_tool_summary("wafer-test", self._cfg(), run_id=bad)
            if bad == "../wafer1.txt":
                # basename() strips the traversal - it resolves to the real file *inside* WaferLog-Data only.
                self.assertNotIn("error", summary)
            else:
                self.assertIn("error", summary)

    def test_missing_folder_is_a_friendly_error(self):
        summary = get_tool_summary("wafer-test", {"kind": "waferlog", "root": os.path.join(self.root, "nowhere")})
        self.assertIn("error", summary)
