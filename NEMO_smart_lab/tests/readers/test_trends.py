"""Tests for the weekly maintenance/health trend calculations."""

import os
import unittest

from NEMO_smart_lab.tests.readers.helpers import _write_heater_log
from NEMO_smart_lab.tests.readers import test_heater_log as _test_heater_log


class GetFaultRateTrendTests(_test_heater_log.HeaterLogTests):
    """get_fault_rate_trend() - a rolling weekly "% of runs faulty" signal, bucketed from
    get_tool_history's own already-computed "faulty" field (no extra fetch/parse of its own)."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_buckets_fault_rate_by_week(self):
        from NEMO_smart_lab.readers import get_fault_rate_trend

        self._write_heater_run("2026_01_03-00-00-00_A.txt")  # week of 2025-12-29
        self._write_heater_run("2026_01_12-00-00-00_B.txt")  # week of 2026-01-12 (a different week)
        trend = get_fault_rate_trend(self._cfg(), scan_limit=25)
        self.assertEqual(len(trend), 2)
        self.assertEqual([entry["total_runs"] for entry in trend], [1, 1])
        # Oldest week first.
        self.assertLess(trend[0]["week_start"], trend[1]["week_start"])

    def test_empty_for_a_kind_with_no_concept_of_faulty(self):
        from NEMO_smart_lab.readers import get_fault_rate_trend

        self.assertEqual(get_fault_rate_trend({"kind": "cobra_job", "root": self.root}), [])


class PumpDownTimeTests(unittest.TestCase):
    """_pump_down_time_s() - a heuristic "time to reach the run's own settled baseline pressure",
    reading backward from the end so a brief early dip below threshold isn't mistaken for the
    real pump-down moment."""

    def test_finds_the_last_crossing_not_the_first(self):
        from NEMO_smart_lab.readers import _pump_down_time_s

        # Settled baseline = average of the last 2s (t=8..10): (2.0+1.0+1.0)/3 = 4/3, threshold =
        # 2.0. The trace dips below that briefly at t=2 (1.4) then rises back to 5.0 - a real
        # transient, not the actual settle - before finally dropping for good after t=7 (index 7,
        # value 5.0, is the LAST point still above threshold). The real pump-down moment is t=8
        # (elapsed 8s from t=0), not the earlier transient dip at t=2.
        time_s = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        values = [10.0, 8.0, 1.4, 5.0, 5.0, 5.0, 5.0, 5.0, 2.0, 1.0, 1.0]
        self.assertEqual(_pump_down_time_s(time_s, values, window_s=2.0, tolerance=1.5), 8)

    def test_none_when_no_settled_baseline_at_all(self):
        from NEMO_smart_lab.readers import _pump_down_time_s

        self.assertIsNone(_pump_down_time_s([], [], window_s=10.0))
        self.assertIsNone(_pump_down_time_s([0, 1], [None, None], window_s=10.0))

    def test_none_when_pressure_never_actually_settles(self):
        from NEMO_smart_lab.readers import _pump_down_time_s

        # Every value is well above the settled-baseline threshold except the tail itself, so
        # there's no earlier "still above threshold" point to measure elapsed time from - actually
        # the tail defines the baseline, so with only rising-then-settling data this should always
        # find a real crossing; this covers the genuinely degenerate case: threshold never crossed
        # because the WHOLE run is already at/under the settled baseline (index None).
        time_s = [0, 1, 2]
        values = [1.0, 1.0, 1.0]
        self.assertIsNone(_pump_down_time_s(time_s, values, window_s=10.0, tolerance=1.5))


class MvdMaintenanceSignalExtractorTests(unittest.TestCase):
    """_extract_mfc_drift/_extract_rf_reflected_fraction/_extract_turbo_speed - the per-run
    signal extraction used by get_mvd_maintenance_trends, tested directly against a synthetic
    "other_series" dict (the same shape _parse_mvd_dat produces) rather than a full DAT file."""

    def test_mfc_drift_averages_absolute_deviation_across_matched_pairs(self):
        from NEMO_smart_lab.readers import _extract_mfc_drift

        run_data = {
            "other_series": {
                "MFC0_setpoint": ("sccm", [10.0, 10.0]),
                "MFC0_reading": ("sccm", [9.0, 11.0]),  # |dev| = 1.0, 1.0
                "MFC1_setpoint": ("sccm", [20.0]),
                "MFC1_reading": ("sccm", [18.0]),  # |dev| = 2.0
            }
        }
        # Average of [1.0, 1.0, 2.0] = 4/3.
        self.assertAlmostEqual(_extract_mfc_drift(run_data), 4 / 3)

    def test_mfc_drift_none_without_a_setpoint_reading_pair(self):
        from NEMO_smart_lab.readers import _extract_mfc_drift

        # mvd's own single "MFC0(sccm)" column (no setpoint/reading split) never matches either
        # regex - see _MFC_SETPOINT_RE/_MFC_READING_RE.
        run_data = {"other_series": {"MFC0": ("sccm", [10.0])}}
        self.assertIsNone(_extract_mfc_drift(run_data))

    def test_rf_reflected_fraction_ignores_plasma_off_samples(self):
        from NEMO_smart_lab.readers import _extract_rf_reflected_fraction

        run_data = {
            "other_series": {
                "PlasmaForwardPower": ("W", [0.0, 100.0, 200.0]),
                "PlasmaReversePower": ("W", [0.0, 10.0, 10.0]),
            }
        }
        # The first sample (forward=0, plasma off) is excluded - average of [10/100, 10/200] * 100.
        self.assertAlmostEqual(_extract_rf_reflected_fraction(run_data), 100.0 * (0.1 + 0.05) / 2)

    def test_rf_reflected_fraction_none_without_both_columns(self):
        from NEMO_smart_lab.readers import _extract_rf_reflected_fraction

        self.assertIsNone(_extract_rf_reflected_fraction({"other_series": {}}))

    def test_turbo_speed_averages_non_null_values(self):
        from NEMO_smart_lab.readers import _extract_turbo_speed

        run_data = {"other_series": {"ReactorTurboSpeed": ("rpm", [40000.0, None, 42000.0])}}
        self.assertEqual(_extract_turbo_speed(run_data, "ReactorTurboSpeed"), 41000.0)

    def test_turbo_speed_none_when_series_missing(self):
        from NEMO_smart_lab.readers import _extract_turbo_speed

        self.assertIsNone(_extract_turbo_speed({"other_series": {}}, "LoadLock TurboSpeed"))
