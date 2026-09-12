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
from NEMO_smart_lab.recipes import _parse_steps, _summarize_steps, find_recipe, get_recipe_detail, list_recipes

RAW_TREE = (
    "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
    "-r--r--r--           498 2026/08/27 08:26:53 20 - STANDBY 200C\n"
    "drwxr-sr-x         4,096 2026/08/14 10:41:31 STANDARD\n"
    "-rwxr-xr-x           365 2026/06/17 15:59:02 STANDARD/Plasma Al2O3 STANDARD.txt\n"
    "drwxr-sr-x         4,096 2026/05/07 17:20:46 Didem\n"
    "drwxr-sr-x         4,096 2026/05/14 03:48:43 Didem/valve three\n"
    "-rwxr-xr-x         1,277 2026/05/14 03:48:43 Didem/valve three/Plasma IWO 10s.txt\n"
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


class ParseStepsTests(TestCase):
    def test_ragged_fields_are_padded_not_dropped(self):
        steps = _parse_steps("flow\t0\t20\r\n", {})
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0], {"line": 1, "command": "flow", "channel": "0", "channel_label": None, "value": "20", "unit": "", "raw": "flow\t0\t20"})

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
        self.assertEqual(by_relpath["20 - STANDBY 200C"]["category"], "(top level)")
        self.assertEqual(by_relpath["STANDARD/Plasma Al2O3 STANDARD.txt"]["category"], "STANDARD")
        self.assertEqual(by_relpath["Didem/valve three/Plasma IWO 10s.txt"]["category"], "Didem")
        self.assertEqual(by_relpath["Didem/valve three/Plasma IWO 10s.txt"]["name"], "Plasma IWO 10s.txt")

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
