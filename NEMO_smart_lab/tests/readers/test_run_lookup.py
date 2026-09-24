"""Tests for run lookups, counts and the recipe/user/date history filters."""

import os
from datetime import date, datetime

from NEMO_smart_lab.readers import get_tool_history
from NEMO_smart_lab.tests.readers.helpers import _write_heater_log
from NEMO_smart_lab.tests.readers import test_heater_log as _test_heater_log


class GetLatestRunIdTests(_test_heater_log.SiblingRunDataTests):
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


class GetRunPageNumberTests(_test_heater_log.SiblingRunDataTests):
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


class GetRunTimeRangeTests(_test_heater_log.SiblingRunDataTests):
    """get_run_time_range() - bounds the one remote lookup find_user_run_windows makes when a
    user-filter search finds nothing locally (see that function's own docstring)."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_returns_earliest_and_latest_run_timestamps(self):
        from NEMO_smart_lab.readers import get_run_time_range

        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_02-00-00-00_B.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        earliest, latest = get_run_time_range(self._cfg())
        self.assertEqual(earliest, datetime(2026, 1, 1, 0, 0, 0))
        self.assertEqual(latest, datetime(2026, 1, 3, 0, 0, 0))

    def test_none_none_when_no_runs_at_all(self):
        from NEMO_smart_lab.readers import get_run_time_range

        # No heater log files written - the Heater Data folder exists (setUp) but is empty, which
        # _list_heater_log_entries treats as a ToolDataError.
        self.assertEqual(get_run_time_range(self._cfg()), (None, None))

    def test_none_none_for_a_kind_with_no_linear_run_list(self):
        from NEMO_smart_lab.readers import get_run_time_range

        self.assertEqual(get_run_time_range({"kind": "cobra_job", "root": self.root}), (None, None))


class CountRunsForRecipeTests(_test_heater_log.SiblingRunDataTests):
    """count_runs_for_recipe() - shown on a recipe's own detail page (see views.tool_recipe_detail)
    so "how much has this recipe actually been used" is visible at a glance."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_counts_only_matching_runs_case_insensitively(self):
        from NEMO_smart_lab.readers import count_runs_for_recipe

        self._write_heater_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        self.assertEqual(count_runs_for_recipe(self._cfg(), "standby 200c"), 2)

    def test_zero_for_a_recipe_with_no_matching_runs(self):
        from NEMO_smart_lab.readers import count_runs_for_recipe

        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        self.assertEqual(count_runs_for_recipe(self._cfg(), "Never Run"), 0)

    def test_zero_for_a_blank_recipe_name(self):
        from NEMO_smart_lab.readers import count_runs_for_recipe

        self.assertEqual(count_runs_for_recipe(self._cfg(), ""), 0)

    def test_none_for_a_kind_with_no_linear_run_list(self):
        from NEMO_smart_lab.readers import count_runs_for_recipe

        self.assertIsNone(count_runs_for_recipe({"kind": "cobra_job", "root": self.root}, "Test Recipe"))


class HistoryFilterTests(_test_heater_log.SiblingRunDataTests):
    """get_tool_history()'s optional recipe/user_windows filters - both metadata-only (filename-
    based), applied before pagination, so filtering never needs to fetch/parse a run's own file
    content - see readers._filter_run_entries."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_recipe_filter_matches_the_runs_own_embedded_recipe_name_case_insensitively(self):
        self._write_heater_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, recipe=["standby 200c"])
        self.assertEqual(total, 2)
        self.assertEqual(
            {r["run_id"] for r in history},
            {"2026_01_03-00-00-00_Standby 200C.txt", "2026_01_01-00-00-00_Standby 200C.txt"},
        )

    def test_recipe_filter_with_no_matches_returns_empty_not_everything(self):
        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, recipe=["Not A Real Recipe"])
        self.assertEqual((history, total), ([], 0))

    def test_user_windows_filter_matches_by_the_runs_own_embedded_start_timestamp(self):
        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_02-00-00-00_B.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        # Only run B's own filename start timestamp (2026-01-02 00:00:00) falls in this window.
        window = (datetime(2026, 1, 1, 12, 0, 0), datetime(2026, 1, 2, 12, 0, 0))
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, user_windows=[window])
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "2026_01_02-00-00-00_B.txt")

    def test_user_windows_filter_with_an_empty_list_still_means_no_matches(self):
        # [] (a user query that matched zero reservations/usage events) must behave differently
        # from None (no filter requested at all) - a real bug this guards against would silently
        # treat "searched for a user with no history on this tool" as "show everything".
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, user_windows=[])
        self.assertEqual((history, total), ([], 0))

    def test_multiple_tagged_recipes_combine_as_or_not_and(self):
        # A run only ever has one recipe - requiring every tagged recipe to match at once would
        # always return nothing the moment a second tag is added, which isn't the intent of a
        # multi-select "tag" filter (see _filter_run_entries's own docstring).
        self._write_heater_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_heater_run("2026_01_01-00-00-00_Valve Clean.txt")
        history, total = get_tool_history(
            self._cfg(), page=1, page_size=25, recipe=["Standby 200C", "Thermal Al2O3"]
        )
        self.assertEqual(total, 2)
        self.assertEqual(
            {r["run_id"] for r in history},
            {"2026_01_03-00-00-00_Standby 200C.txt", "2026_01_02-00-00-00_Thermal Al2O3.txt"},
        )

    def test_recipe_and_user_windows_filters_combine_as_and(self):
        self._write_heater_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        window = (datetime(2026, 1, 1, 12, 0, 0), datetime(2026, 1, 2, 12, 0, 0))
        history, total = get_tool_history(
            self._cfg(), page=1, page_size=25, recipe=["Standby 200C"], user_windows=[window]
        )
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "2026_01_02-00-00-00_Standby 200C.txt")

    def test_date_filter_matches_the_runs_own_embedded_start_date_inclusive(self):
        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_02-00-00-00_B.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        history, total = get_tool_history(
            self._cfg(), page=1, page_size=25, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2)
        )
        self.assertEqual(total, 2)
        self.assertEqual(
            {r["run_id"] for r in history},
            {"2026_01_02-00-00-00_B.txt", "2026_01_01-00-00-00_A.txt"},
        )

    def test_date_filter_start_only_has_no_upper_bound(self):
        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, start_date=date(2026, 1, 2))
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "2026_01_03-00-00-00_C.txt")

    def test_date_filter_end_only_has_no_lower_bound(self):
        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        history, total = get_tool_history(self._cfg(), page=1, page_size=25, end_date=date(2026, 1, 2))
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "2026_01_01-00-00-00_A.txt")

    def test_a_single_day_picks_only_runs_from_that_day(self):
        # The literal "just choose one day" use case - same date for both start and end.
        self._write_heater_run("2026_01_02-08-00-00_B.txt")
        self._write_heater_run("2026_01_01-00-00-00_A.txt")
        self._write_heater_run("2026_01_03-00-00-00_C.txt")
        history, total = get_tool_history(
            self._cfg(), page=1, page_size=25, start_date=date(2026, 1, 2), end_date=date(2026, 1, 2)
        )
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "2026_01_02-08-00-00_B.txt")

    def test_date_and_recipe_filters_combine_as_and(self):
        self._write_heater_run("2026_01_02-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        history, total = get_tool_history(
            self._cfg(), page=1, page_size=25, recipe=["Standby 200C"], start_date=date(2026, 1, 2), end_date=date(2026, 1, 2)
        )
        self.assertEqual(total, 1)
        self.assertEqual(history[0]["run_id"], "2026_01_02-00-00-00_Standby 200C.txt")

    def test_get_run_page_number_respects_the_recipe_filter(self):
        from NEMO_smart_lab.readers import get_run_page_number

        self._write_heater_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        # With the recipe filter applied, "Thermal Al2O3" isn't in the filtered list at all.
        self.assertIsNone(
            get_run_page_number(
                self._cfg(), "2026_01_02-00-00-00_Thermal Al2O3.txt", page_size=25, recipe=["Standby 200C"]
            )
        )
        # The older "Standby 200C" run is index 1 (0-indexed) in the filtered, newest-first list.
        self.assertEqual(
            get_run_page_number(
                self._cfg(), "2026_01_01-00-00-00_Standby 200C.txt", page_size=1, recipe=["Standby 200C"]
            ),
            2,
        )


class CountRunsByRecipeNameTests(_test_heater_log.SiblingRunDataTests):
    """count_runs_by_recipe_name() - cheap, listing-only aggregation used by recipes.py's
    total_cycles_run and the recipe list's own "Runs" column (views._recipe_run_counts)."""

    def _write_heater_run(self, filename):
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "irrelevant", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", filename), self.FULL_HEADER, [row])

    def test_counts_by_each_runs_own_embedded_recipe_name(self):
        from NEMO_smart_lab.readers import count_runs_by_recipe_name

        self._write_heater_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_heater_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_heater_run("2026_01_01-00-00-00_Standby 200C.txt")
        counts = count_runs_by_recipe_name(self._cfg())
        self.assertEqual(counts, {"Standby 200C": 2, "Thermal Al2O3": 1})

    def test_empty_dict_for_a_kind_with_no_linear_run_list(self):
        from NEMO_smart_lab.readers import count_runs_by_recipe_name

        self.assertEqual(count_runs_by_recipe_name({"kind": "cobra_job", "root": self.root}), {})
