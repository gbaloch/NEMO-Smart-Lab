"""
Tests for NEMO_smart_lab.recipes - every remote call is mocked at the same seam
test_remote_cache.py already establishes (NEMO_smart_lab.remote_cache.remote_sync's actual
transfer functions), so nothing here ever touches the network.
"""

import os
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool, SmartLabToolChannel
from NEMO_smart_lab.recipes import (
    _category_sort_priority,
    _parse_steps,
    _summarize_steps,
    find_duplicate_recipes,
    find_recipe,
    find_recipe_by_name,
    get_recipe_detail,
    list_recipes,
    suggest_base_pressure_recipes,
    total_cycles_run,
)

RAW_TREE = (
    "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
    "-r--r--r--           498 2026/08/27 08:26:53 20 - STANDBY 200C\n"
    "drwxr-sr-x         4,096 2026/08/14 10:41:31 STANDARD\n"
    "-rwxr-xr-x           365 2026/06/17 15:59:02 STANDARD/Plasma Al2O3 STANDARD.txt\n"
    "drwxr-sr-x         4,096 2026/05/07 17:20:46 Didem\n"
    "drwxr-sr-x         4,096 2026/05/14 03:48:43 Didem/valve three\n"
    "-rwxr-xr-x         1,277 2026/05/14 03:48:43 Didem/valve three/Plasma IWO 10s.txt\n"
    # Confirmed live: a stray spreadsheet someone dropped in a per-user recipe folder - never a
    # recipe itself, so list_recipes must drop it rather than list it as one.
    "-rwxr-xr-x         9,216 2026/05/14 03:48:43 Didem/valve three/notes.xlsx\n"
)

RECIPE_TEXT = (
    "flow\t0\t20\tsccm\r\n"
    "stabilize\t12\t\r\n"
    "wait\t\t3\tsec\r\n"
    "heater\t17\t150\t\r\n"
    "heater\t17\t150\t\r\n"
    "pulse\t2\t0.06\tsec\r\n"
    "goto\t11\t100\tcycles\r\n"
)


class CategorySortPriorityTests(TestCase):
    def test_ordering(self):
        categories = [
            "Didem",
            "Digilens Standard",  # a per-project folder that merely contains "standard"
            "process",
            "Production",
            "(root)",
            "Maintenance",
            "STANDARD",
        ]
        ordered = sorted(categories, key=lambda c: (_category_sort_priority(c), c.lower()))
        self.assertEqual(
            ordered,
            ["(root)", "STANDARD", "Maintenance", "Production", "process", "Didem", "Digilens Standard"],
        )

    def test_only_exact_folder_names_get_the_priority_boost(self):
        # A folder that merely *contains* one of these words (a per-project variant) is not the
        # tool's actual shared folder - confirmed live a tool can have both.
        self.assertEqual(_category_sort_priority("Digilens Standard"), 5)
        self.assertEqual(_category_sort_priority("Digilens Maintenance"), 5)
        self.assertEqual(_category_sort_priority("Production Line A"), 5)
        self.assertEqual(_category_sort_priority("Process Notes"), 5)

    def test_recognizes_plural_and_case_variants(self):
        self.assertEqual(_category_sort_priority("standard recipes"), 1)
        self.assertEqual(_category_sort_priority("PRODUCTION RECIPES"), 3)
        self.assertEqual(_category_sort_priority("Process Recipe"), 4)

    def test_pinned_folder_sorts_ahead_of_everything_including_top_level(self):
        self.assertEqual(_category_sort_priority("Didem", pinned=["Didem"]), -1)
        self.assertEqual(_category_sort_priority("(root)", pinned=["Didem"]), 0)
        ordered = sorted(["(root)", "STANDARD", "Didem"], key=lambda c: _category_sort_priority(c, pinned=["Didem"]))
        self.assertEqual(ordered, ["Didem", "(root)", "STANDARD"])


class ParseStepsTests(TestCase):
    def test_ragged_fields_are_padded_not_dropped(self):
        steps = _parse_steps("flow\t0\t20\r\n", {})
        self.assertEqual(len(steps), 1)
        self.assertEqual(
            steps[0],
            {
                "line": 1,
                "command": "flow",
                "channel": "0",
                "channel_label": None,
                "channel_role": None,
                "value": "20",
                "unit": "",
                "raw": "flow\t0\t20",
            },
        )

    def test_blank_lines_are_skipped(self):
        steps = _parse_steps("flow\t0\t20\r\n\r\n\t\t\t\r\nwait\t\t3\tsec\r\n", {})
        self.assertEqual([s["command"] for s in steps], ["flow", "wait"])

    def test_heater_channel_label_is_resolved(self):
        channel_labels = {"17": ("ALD Valves", "other", False, None)}
        steps = _parse_steps("heater\t17\t150\t\r\n", channel_labels)
        self.assertEqual(steps[0]["channel_label"], "ALD Valves")

    def test_non_heater_command_never_gets_a_label_even_if_channel_number_matches(self):
        channel_labels = {"0": ("Some Heater", "other", False, None)}
        steps = _parse_steps("flow\t0\t20\r\n", channel_labels)
        self.assertIsNone(steps[0]["channel_label"])

    def test_unmapped_heater_channel_has_no_label(self):
        steps = _parse_steps("heater\t99\t150\t\r\n", {"17": ("ALD Valves", "other", False, None)})
        self.assertIsNone(steps[0]["channel_label"])

    def test_heater_log_style_key_resolves_via_configured_offset(self):
        # heater_log-kind tools (fiji1/2/3) store channel_labels keyed by the log file's own
        # header text ("Heater N"), not the recipe's own physical channel number - confirmed live
        # that recipe channel 12 ("Cone") is the log's "Heater 6", a +6 offset for these tools.
        channel_labels = {"Heater 6": ("Cone", "chamber", False, None)}
        steps = _parse_steps("heater\t12\t200\t\r\n", channel_labels, channel_offset=6)
        self.assertEqual(steps[0]["channel_label"], "Cone")

    def test_heater_log_style_key_stays_unresolved_without_the_offset_configured(self):
        # Without recipe_channel_offset explicitly set (default 0), "12" - 0 = "Heater 12", which
        # doesn't match "Heater 6" - deliberately unresolved rather than guessing wrong.
        channel_labels = {"Heater 6": ("Cone", "chamber", False, None)}
        steps = _parse_steps("heater\t12\t200\t\r\n", channel_labels)
        self.assertIsNone(steps[0]["channel_label"])

    def test_zero_offset_matches_heater_log_style_key_directly(self):
        # savannah's recipe channel numbers already match its log header directly - confirmed the
        # default offset of 0 is correct there, no per-tool configuration needed.
        channel_labels = {"Heater 9": ("Inner Heater", "other", False, None)}
        steps = _parse_steps("heater\t9\t200\t\r\n", channel_labels, channel_offset=0)
        self.assertEqual(steps[0]["channel_label"], "Inner Heater")

    def test_non_numeric_channel_never_crashes_offset_lookup(self):
        steps = _parse_steps("heater\t\t200\t\r\n", {"Heater 6": ("Cone", "chamber", False, None)}, channel_offset=6)
        self.assertIsNone(steps[0]["channel_label"])


class SummarizeStepsTests(TestCase):
    def test_cycles_parsed_from_first_goto(self):
        steps = _parse_steps(RECIPE_TEXT, {})
        summary = _summarize_steps(steps)
        self.assertEqual(summary["cycles"], 100)

    def test_no_goto_line_means_no_cycles(self):
        summary = _summarize_steps(_parse_steps("flow\t0\t20\r\n", {}))
        self.assertIsNone(summary["cycles"])

    def test_heater_setpoints_deduplicated_by_channel_first_seen_order(self):
        steps = _parse_steps(RECIPE_TEXT, {})
        summary = _summarize_steps(steps)
        self.assertEqual(len(summary["heater_setpoints"]), 1)
        self.assertEqual(summary["heater_setpoints"][0]["channel"], "17")

    def test_step_count_matches_parsed_steps(self):
        steps = _parse_steps(RECIPE_TEXT, {})
        self.assertEqual(_summarize_steps(steps)["step_count"], len(steps))

    def test_heater_setpoint_unit_is_always_the_degree_symbol_regardless_of_the_raw_recipe_unit(self):
        # Real recipe files spell a heater line's own unit field inconsistently (blank, "deg C",
        # "C", ...) - a "heater" command is always a temperature setpoint, so the summary's own
        # unit is never ambiguous even when the raw recipe text is.
        steps = _parse_steps("heater\t6\t25\tdeg C\r\nheater\t7\t200\t\r\n", {})
        summary = _summarize_steps(steps)
        self.assertEqual({s["unit"] for s in summary["heater_setpoints"]}, {"°C"})

    def test_role_shown_when_more_than_one_setpoint_shares_a_role(self):
        # Same "only show the role subtitle when it actually distinguishes something" rule as the
        # regular tool detail page's own heater channel table (readers._mark_shared_roles) - here
        # scoped to just this recipe's own heater setpoints, e.g. real "Reactor 1"/"Reactor 2"
        # channels that both carry role "reactor".
        channel_labels = {
            "1": ("Reactor 1", "reactor", False, None),
            "2": ("Reactor 2", "reactor", False, None),
            "3": ("Cone", "other", False, None),
        }
        steps = _parse_steps("heater\t1\t300\t\r\nheater\t2\t300\t\r\nheater\t3\t300\t\r\n", channel_labels)
        summary = _summarize_steps(steps)
        by_channel = {s["channel"]: s for s in summary["heater_setpoints"]}
        self.assertTrue(by_channel["1"]["role_shown"])
        self.assertTrue(by_channel["2"]["role_shown"])
        # "other" is never shown as a subtitle (see _mark_shared_roles), and here it's also the
        # only channel with that role either way.
        self.assertFalse(by_channel["3"]["role_shown"])

    def test_role_not_shown_when_only_one_setpoint_has_that_role(self):
        channel_labels = {"1": ("Chuck", "chuck", False, None), "2": ("Cone", "other", False, None)}
        steps = _parse_steps("heater\t1\t300\t\r\nheater\t2\t300\t\r\n", channel_labels)
        summary = _summarize_steps(steps)
        by_channel = {s["channel"]: s for s in summary["heater_setpoints"]}
        self.assertFalse(by_channel["1"]["role_shown"])


class ListRecipesTests(TestCase):
    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji1", recipe_subdir="Recipes",
        )

    def test_no_recipe_subdir_returns_empty_without_any_remote_call(self):
        no_recipes_tool = SmartLabTool.objects.create(name="fiji9", kind="heater_log", local_root=self._tmp.name)
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive") as mock_list:
            self.assertEqual(list_recipes(no_recipes_tool.as_source_config()), [])
        mock_list.assert_not_called()

    def test_lists_files_only_grouped_by_top_level_folder(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            recipes = list_recipes(self.tool.as_source_config())
        by_relpath = {r["relpath"]: r for r in recipes}
        self.assertNotIn("STANDARD", by_relpath)  # the bare folder entry is excluded, only files listed
        self.assertEqual(by_relpath["20 - STANDBY 200C"]["category"], "(root)")
        self.assertEqual(by_relpath["STANDARD/Plasma Al2O3 STANDARD.txt"]["category"], "STANDARD")
        self.assertEqual(by_relpath["Didem/valve three/Plasma IWO 10s.txt"]["category"], "Didem")
        self.assertEqual(by_relpath["Didem/valve three/Plasma IWO 10s.txt"]["name"], "Plasma IWO 10s.txt")

    def test_non_flat_files_like_stray_spreadsheets_are_never_listed_as_recipes(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            recipes = list_recipes(self.tool.as_source_config())
        self.assertNotIn("notes.xlsx", {r["name"] for r in recipes})

    def test_top_level_and_standard_folders_sort_before_per_user_folders(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            recipes = list_recipes(self.tool.as_source_config())
        categories_in_order = list(dict.fromkeys(r["category"] for r in recipes))
        self.assertEqual(categories_in_order, ["(root)", "STANDARD", "Didem"])

    def test_pinned_category_sorts_ahead_of_top_level_and_standard(self):
        self.tool.pinned_recipe_categories = ["Didem"]
        self.tool.save(update_fields=["pinned_recipe_categories"])
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            recipes = list_recipes(self.tool.as_source_config())
        categories_in_order = list(dict.fromkeys(r["category"] for r in recipes))
        self.assertEqual(categories_in_order, ["Didem", "(root)", "STANDARD"])

    def test_find_recipe_looks_up_by_stable_id(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            cfg = self.tool.as_source_config()
            recipes = list_recipes(cfg)
            some_id = recipes[0]["id"]
            found = find_recipe(cfg, some_id)
        self.assertIsNotNone(found)
        self.assertEqual(found["relpath"], recipes[0]["relpath"])

    def test_find_recipe_returns_none_for_unknown_id(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            self.assertIsNone(find_recipe(self.tool.as_source_config(), "not-a-real-id"))

    def test_find_recipe_by_name_matches_a_runs_own_recorded_name(self):
        # A run's own recorded recipe name has no folder/extension - just "Plasma Al2O3 STANDARD",
        # matched against the recipe file "STANDARD/Plasma Al2O3 STANDARD.txt".
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            cfg = self.tool.as_source_config()
            found = find_recipe_by_name(cfg, "Plasma Al2O3 STANDARD")
        self.assertIsNotNone(found)
        self.assertEqual(found["relpath"], "STANDARD/Plasma Al2O3 STANDARD.txt")

    def test_find_recipe_by_name_is_case_insensitive_and_ignores_txt_suffixes(self):
        # Confirmed live: some recipes are themselves named with a ".txt" suffix, which then picks
        # up a *second* .txt from the heater log export on the run side - both spellings must
        # match the one real recipe file.
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            cfg = self.tool.as_source_config()
            found = find_recipe_by_name(cfg, "plasma al2o3 standard.txt")
        self.assertIsNotNone(found)
        self.assertEqual(found["relpath"], "STANDARD/Plasma Al2O3 STANDARD.txt")

    def test_find_recipe_by_name_returns_none_for_no_match(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            self.assertIsNone(find_recipe_by_name(self.tool.as_source_config(), "Not A Real Recipe"))

    def test_find_recipe_by_name_returns_none_for_blank_or_unknown(self):
        cfg = self.tool.as_source_config()
        self.assertIsNone(find_recipe_by_name(cfg, ""))
        self.assertIsNone(find_recipe_by_name(cfg, None))
        self.assertIsNone(find_recipe_by_name(cfg, "(unknown)"))

    def test_find_recipe_by_name_prefers_the_canonical_folder_over_a_per_user_duplicate(self):
        # Confirmed live: the exact same standard recipe is routinely copied into several
        # per-user folders too, not just its one canonical "STANDARD" copy - that canonical
        # location is a much stronger signal for "the" recipe than any one person's own copy.
        duplicated_tree = RAW_TREE + "-rwxr-xr-x       1,000 2026/05/01 00:00:00 SomeUser/Plasma Al2O3 STANDARD.txt\n"
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=duplicated_tree):
            cfg = self.tool.as_source_config()
            found = find_recipe_by_name(cfg, "Plasma Al2O3 STANDARD")
        self.assertIsNotNone(found)
        self.assertEqual(found["relpath"], "STANDARD/Plasma Al2O3 STANDARD.txt")

    def test_find_recipe_by_name_returns_none_when_only_per_user_copies_exist(self):
        # No canonical copy at all - two different users' own folders both have a recipe of the
        # same name, genuinely ambiguous, so this deliberately doesn't guess which one.
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "drwxr-sr-x         4,096 2026/05/07 17:20:46 UserA\n"
            "-rwxr-xr-x           365 2026/06/17 15:59:02 UserA/Some Recipe.txt\n"
            "drwxr-sr-x         4,096 2026/05/07 17:20:46 UserB\n"
            "-rwxr-xr-x           365 2026/06/17 15:59:02 UserB/Some Recipe.txt\n"
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree):
            cfg = self.tool.as_source_config()
            self.assertIsNone(find_recipe_by_name(cfg, "Some Recipe"))

    def test_find_recipe_by_name_returns_none_when_two_canonical_folders_both_match(self):
        # Both "(root)" and "STANDARD" are canonical locations - if a recipe of the same name
        # somehow exists in both, that's still genuinely ambiguous.
        tree = RAW_TREE + "-rwxr-xr-x       1,000 2026/05/01 00:00:00 Plasma Al2O3 STANDARD.txt\n"
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree):
            cfg = self.tool.as_source_config()
            self.assertIsNone(find_recipe_by_name(cfg, "Plasma Al2O3 STANDARD"))


class GetRecipeDetailTests(TestCase):
    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji1", recipe_subdir="Recipes",
        )
        SmartLabToolChannel.objects.create(tool=self.tool, channel_key="17", display_name="ALD Valves")

    def test_unknown_recipe_id_returns_none(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE):
            self.assertIsNone(get_recipe_detail(self.tool.as_source_config(), "not-a-real-id"))

    def test_fetches_and_parses_the_winning_recipe(self):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(RECIPE_TEXT)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file) as mock_sync,
        ):
            cfg = self.tool.as_source_config()
            recipe_id = list_recipes(cfg)[0]["id"]
            detail = get_recipe_detail(cfg, recipe_id)

        mock_sync.assert_called_once()
        self.assertEqual(detail["cycles"], 100)
        self.assertEqual(detail["heater_setpoints"][0]["label"], "ALD Valves")
        self.assertIn("goto\t11\t100\tcycles", detail["raw_text"])

    def test_heater_log_kind_tool_resolves_labels_via_configured_recipe_channel_offset(self):
        # End-to-end version of the offset behavior verified in test_recipes.ParseStepsTests,
        # through the real SmartLabTool.as_source_config()/SmartLabToolChannel path.
        self.tool.recipe_channel_offset = 6
        self.tool.save(update_fields=["recipe_channel_offset"])
        SmartLabToolChannel.objects.create(tool=self.tool, channel_key="Heater 6", display_name="Cone")

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write("heater\t12\t200\t\r\n")
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            cfg = self.tool.as_source_config()
            recipe_id = list_recipes(cfg)[0]["id"]
            detail = get_recipe_detail(cfg, recipe_id)

        self.assertEqual(detail["heater_setpoints"][0]["channel"], "12")
        self.assertEqual(detail["heater_setpoints"][0]["label"], "Cone")


class GetRecipeDetailMvdChannelLabelTests(TestCase):
    """A recipe's own heater setpoint names, for mvd-kind tools (mvd/fiji5), fall back to this
    tool's config.ini-parsed heater names (readers._mvd_config_heater_labels) when there's no DB
    override - the same fallback the live "Heater channels" table already uses (readers._mvd_summary)
    - so a channel that only ever had a config.ini name (confirmed live on fiji5: e.g. channel 13
    "UPPER") shows that name here too, instead of the bare "Heater 13" placeholder a recipe with no
    matching DB override previously fell back to."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji5", recipe_subdir="Recipes", config_subdir="configuration",
        )

    def _get_detail(self, recipe_text):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(recipe_text)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=RAW_TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            cfg = self.tool.as_source_config()
            recipe_id = list_recipes(cfg)[0]["id"]
            return get_recipe_detail(cfg, recipe_id)

    def test_falls_back_to_config_ini_heater_label_when_no_db_override(self):
        with patch("NEMO_smart_lab.recipes.parsing._mvd_config_heater_labels", return_value={"13": "UPPER"}):
            detail = self._get_detail("heater\t13\t270\t\r\n")
        self.assertEqual(detail["heater_setpoints"][0]["label"], "UPPER")

    def test_db_override_still_wins_over_config_ini_label(self):
        SmartLabToolChannel.objects.create(tool=self.tool, channel_key="13", display_name="Custom Name")
        with patch("NEMO_smart_lab.recipes.parsing._mvd_config_heater_labels", return_value={"13": "UPPER"}):
            detail = self._get_detail("heater\t13\t270\t\r\n")
        self.assertEqual(detail["heater_setpoints"][0]["label"], "Custom Name")

    def test_role_shown_uses_tool_wide_counts_not_just_this_recipes_own_channels(self):
        # Two channels tool-wide share role "precursor_line" ("Jacket"), but this recipe only ever
        # sets ONE of them - without readers.tool_wide_role_counts, that would look like the only
        # channel with that role (within just this recipe's own setpoints) and hide the subtitle,
        # even though the live "Heater channels" table (which sees both channels at once) shows it.
        SmartLabToolChannel.objects.create(tool=self.tool, channel_key="19", display_name="Precursor 2", role="precursor_line")
        SmartLabToolChannel.objects.create(tool=self.tool, channel_key="20", display_name="Precursor 3", role="precursor_line")
        detail = self._get_detail("heater\t19\t75\t\r\n")
        self.assertTrue(detail["heater_setpoints"][0]["role_shown"])


class SuggestBasePressureRecipesTests(TestCase):
    """suggest_base_pressure_recipes() - a recipe only qualifies if its name matches a standby
    keyword AND its last step is a "wait" (the long settle-then-measure step this whole feature
    depends on) - confirmed live that more than one real standby variant can qualify at once,
    which is exactly why base_pressure_recipe_names takes a list."""

    TREE = (
        "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
        "-r--r--r--           100 2026/08/27 08:26:53 STANDBY A.txt\n"
        "-r--r--r--           100 2026/08/27 08:26:53 STANDBY B.txt\n"
        "-r--r--r--           100 2026/08/27 08:26:53 Al2O3 - STANDARD.txt\n"
    )

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji1", recipe_subdir="Recipes",
        )

    def _fake_sync_file(self, contents_by_name):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            name = remote_relpath_full.rsplit("/", 1)[-1]
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(contents_by_name.get(name, ""))
            return "ok"

        return fake_sync_file

    def test_only_standby_named_recipes_ending_in_wait_are_suggested(self):
        contents = {
            "STANDBY A.txt": "heater\t17\t150\t\r\nwait\t\t30\tsec\r\n",
            "STANDBY B.txt": "heater\t17\t150\t\r\ngoto\t11\t100\tcycles\r\n",  # standby-named, but doesn't end in wait
            "Al2O3 - STANDARD.txt": "wait\t\t30\tsec\r\n",  # ends in wait, but not standby-named
        }
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=self.TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            found = suggest_base_pressure_recipes(self.tool.as_source_config(), "standby")
        self.assertEqual(found, ["STANDBY A"])

    def test_valve_clean_variant_is_excluded_even_if_it_ends_in_wait(self):
        # Confirmed live: a "... - Valve Clean" standby variant can vent the chamber partway
        # through (one real reading spiked to 163 Torr against an otherwise ~0.1-0.2 Torr
        # baseline) - exactly the "different standby-ish variants have genuinely different
        # baseline pressure" mixing this whole feature is designed to avoid.
        contents = {
            "STANDBY A.txt": "wait\t\t30\tsec\r\n",
            "STANDBY B.txt": "wait\t\t30\tsec\r\n",  # renamed below to a Valve Clean variant
        }
        tree = self.TREE.replace("STANDBY B.txt", "STANDBY A - Valve Clean.txt")
        contents["STANDBY A - Valve Clean.txt"] = contents.pop("STANDBY B.txt")
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            found = suggest_base_pressure_recipes(self.tool.as_source_config(), "standby", exclude_keywords="valve clean")
        self.assertEqual(found, ["STANDBY A"])

    def test_multiple_qualifying_variants_are_all_returned(self):
        contents = {
            "STANDBY A.txt": "wait\t\t30\tsec\r\n",
            "STANDBY B.txt": "wait\t\t45\tsec\r\n",
            "Al2O3 - STANDARD.txt": "goto\t11\t100\tcycles\r\n",
        }
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=self.TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            found = suggest_base_pressure_recipes(self.tool.as_source_config(), "standby")
        self.assertEqual(set(found), {"STANDBY A", "STANDBY B"})

    def test_no_standby_keywords_returns_empty_without_scanning_anything(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive") as mock_list:
            found = suggest_base_pressure_recipes(self.tool.as_source_config(), "")
        self.assertEqual(found, [])
        mock_list.assert_not_called()

    def test_same_recipe_name_appearing_in_multiple_folders_is_deduplicated(self):
        # Confirmed live: the same recipe name can appear more than once across a tool's recipe
        # tree (a top-level copy and a per-user folder copy, etc.) - each is a separate list_recipes
        # entry, but the same *name* should only ever appear once in the suggestion.
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           100 2026/08/27 08:26:53 STANDBY A.txt\n"
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 Someone\n"
            "-r--r--r--           100 2026/08/27 08:26:53 Someone/STANDBY A.txt\n"
        )
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch(
                "NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote",
                side_effect=self._fake_sync_file({"STANDBY A.txt": "wait\t\t30\tsec\r\n"}),
            ),
        ):
            found = suggest_base_pressure_recipes(self.tool.as_source_config(), "standby")
        self.assertEqual(found, ["STANDBY A"])


class TotalCyclesRunTests(TestCase):
    """total_cycles_run() - sums each run's own recipe's CURRENT cycle count, weighted by how
    many times that recipe was actually run - the real metric fabs use for reactor/seal-wear PM
    scheduling, not just calendar time. Approximate by construction (see the function's own
    docstring), so tests focus on the weighting/exclusion logic, not exact-historical-accuracy."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji1", recipe_subdir="Recipes",
        )

    def _fake_sync_file(self, contents_by_name):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            name = remote_relpath_full.rsplit("/", 1)[-1]
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(contents_by_name.get(name, ""))
            return "ok"

        return fake_sync_file

    def test_sums_cycles_weighted_by_actual_run_count(self):
        recipe_tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           100 2026/08/27 08:26:53 Standby 200C.txt\n"
        )
        run_listing = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           500 2026/01/03 00:00:00 2026_01_03-00-00-00_Standby 200C.txt\n"
            "-r--r--r--           500 2026/01/02 00:00:00 2026_01_02-00-00-00_Standby 200C.txt\n"
        )
        contents = {"Standby 200C.txt": "heater\t17\t150\t\r\nwait\t\t30\tsec\r\ngoto\t11\t10\tcycles\r\n"}
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=recipe_tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=run_listing),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            total_cycles, counted_runs, total_runs = total_cycles_run(self.tool.as_source_config())
        self.assertEqual(total_runs, 2)
        self.assertEqual(counted_runs, 2)
        self.assertEqual(total_cycles, 20)  # 10 cycles/run * 2 runs

    def test_a_runs_recipe_that_no_longer_exists_is_excluded_not_guessed(self):
        recipe_tree = "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"  # no recipe files at all
        run_listing = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           500 2026/01/03 00:00:00 2026_01_03-00-00-00_Deleted Recipe.txt\n"
        )
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=recipe_tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=run_listing),
        ):
            total_cycles, counted_runs, total_runs = total_cycles_run(self.tool.as_source_config())
        self.assertEqual(total_runs, 1)
        self.assertEqual(counted_runs, 0)
        self.assertEqual(total_cycles, 0)

    def test_zero_zero_zero_with_no_run_history_at_all(self):
        run_listing = "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=run_listing):
            self.assertEqual(total_cycles_run(self.tool.as_source_config()), (0, 0, 0))


class FindDuplicateRecipesTests(TestCase):
    """find_duplicate_recipes() - groups recipes with identical step *content*, confirmed live
    that the same recipe routinely gets copied verbatim into several per-user folders."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji1", recipe_subdir="Recipes",
        )

    def _fake_sync_file(self, contents_by_name):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            name = remote_relpath_full.rsplit("/", 1)[-1]
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(contents_by_name.get(name, ""))
            return "ok"

        return fake_sync_file

    def test_identical_content_under_different_names_and_folders_is_one_group(self):
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           100 2026/08/27 08:26:53 Plasma Al2O3 STANDARD.txt\n"
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 Someone\n"
            "-r--r--r--           100 2026/08/27 08:26:53 Someone/10x Plasma Al2O3.txt\n"
            "-r--r--r--           100 2026/08/27 08:26:53 Unique Recipe.txt\n"
        )
        same_content = "heater\t17\t150\t\r\nwait\t\t30\tsec\r\n"
        contents = {
            "Plasma Al2O3 STANDARD.txt": same_content,
            "10x Plasma Al2O3.txt": same_content,
            "Unique Recipe.txt": "heater\t17\t200\t\r\nwait\t\t60\tsec\r\n",
        }
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            groups = find_duplicate_recipes(self.tool.as_source_config())
        self.assertEqual(len(groups), 1)
        names = {r["name"] for r in groups[0]["recipes"]}
        self.assertEqual(names, {"Plasma Al2O3 STANDARD.txt", "10x Plasma Al2O3.txt"})

    def test_trivial_formatting_differences_still_count_as_the_same_content(self):
        # Different line endings/trailing whitespace on an otherwise identical program - the
        # comparison is on *parsed* steps, not raw bytes (see _recipe_content_fingerprint).
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           100 2026/08/27 08:26:53 A.txt\n"
            "-r--r--r--           100 2026/08/27 08:26:53 B.txt\n"
        )
        contents = {
            "A.txt": "heater\t17\t150\t\r\n",
            "B.txt": "heater\t17\t150\t  \n",  # LF instead of CRLF, trailing spaces on the value
        }
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            groups = find_duplicate_recipes(self.tool.as_source_config())
        self.assertEqual(len(groups), 1)

    def test_no_duplicates_returns_empty_list(self):
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-r--r--r--           100 2026/08/27 08:26:53 A.txt\n"
            "-r--r--r--           100 2026/08/27 08:26:53 B.txt\n"
        )
        contents = {"A.txt": "heater\t17\t150\t\r\n", "B.txt": "heater\t17\t200\t\r\n"}
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=self._fake_sync_file(contents)),
        ):
            groups = find_duplicate_recipes(self.tool.as_source_config())
        self.assertEqual(groups, [])

    def test_empty_for_no_recipe_subdir(self):
        no_recipes_tool = SmartLabTool.objects.create(name="fiji9", kind="heater_log", local_root=self._tmp.name)
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive") as mock_list:
            self.assertEqual(find_duplicate_recipes(no_recipes_tool.as_source_config()), [])
        mock_list.assert_not_called()
