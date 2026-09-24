"""Tests for chamber base pressure over time (get_base_pressure_history)."""

import os
from datetime import datetime

from NEMO_smart_lab.tests.readers.helpers import _write_heater_log
from NEMO_smart_lab.tests.readers import test_heater_log as _test_heater_log
from NEMO_smart_lab.tests.readers import test_mvd as _test_mvd


class BasePressureHistoryHeaterLogTests(_test_heater_log.SiblingRunDataTests):
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


class BasePressureHistoryMvdTests(_test_mvd.MvdPressureAndEventsTests):
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


class BasePressureHistoryMultipleRecipesTests(_test_heater_log.SiblingRunDataTests):
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

    def test_each_point_carries_its_own_recipe_name(self):
        # With more than one configured standby recipe, a bare pressure trend alone can't say which
        # recipe produced a given point - "recipe" lets the chart show that per point.
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        self._write_heater_run("2026_01_02-00-00-00_STANDBY - Valve Clean.txt", [(0.0, 0.9)])
        cfg = self._cfg()
        cfg["base_pressure_recipe_names"] = "STANDBY, STANDBY - Valve Clean"
        results = get_base_pressure_history(cfg)
        by_recipe = {r["recipe"]: r["value"] for r in results}
        self.assertAlmostEqual(by_recipe["STANDBY"], 0.2)
        self.assertAlmostEqual(by_recipe["STANDBY - Valve Clean"], 0.9)

    def test_extra_whitespace_and_blank_entries_in_the_list_are_tolerated(self):
        from NEMO_smart_lab.readers import get_base_pressure_history

        self._write_heater_run("2026_01_01-00-00-00_STANDBY.txt", [(0.0, 0.2)])
        cfg = self._cfg()
        cfg["base_pressure_recipe_names"] = "  STANDBY ,, "
        results = get_base_pressure_history(cfg)
        self.assertEqual(len(results), 1)
