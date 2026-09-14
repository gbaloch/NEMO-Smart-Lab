"""
Tests for NEMO_smart_lab.configs - the read-only configuration/settings-file browser. Every test
mocks the actual remote_sync transfer/listing functions, so nothing here ever touches the network.
"""

import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.configs import find_active_config_file, find_config_file, get_config_file_detail, list_config_files
from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool

# fiji5/mvd-style layout: a real named subfolder, itself nested further. Includes both a
# non-flat-file (manual.pdf - confirmed live these installation/manual files sit right alongside
# real config.ini/setup files on Oak, e.g. fiji1's ALD_Fiji.exe/.dll/.mxx/.aliases) and a flat file
# that still isn't offered a raw-text preview (changelog.log - not in _TEXT_EXTENSIONS), so the two
# distinct behaviors (dropped entirely vs. listed-but-no-preview) each have their own fixture entry.
SUBDIR_TREE = (
    "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
    "-rwxr-xr-x           498 2026/08/14 10:41:31 config.ini\n"
    "-rwxr-xr-x           365 2026/06/17 15:59:02 esmodels.txt\n"
    "-rwxr-xr-x           600 2026/04/01 00:00:00 changelog.log\n"
    "-rwxr-xr-x         1,277 2026/05/14 03:48:43 manual.pdf\n"
    "drwxr-sr-x         4,096 2026/05/07 17:20:46 SW versions\n"
    "-rwxr-xr-x           200 2026/05/07 17:20:46 SW versions/notes.txt\n"
)

# fiji1/2/3-style layout: loose files sitting at the tool's own root, side by side with
# Logfile/Recipes (which a shallow, non-recursive listing of just that one folder never sees) - and
# with the instrument software's own installation files (confirmed live, this exact set: the
# LabVIEW executable, its DLL/mxx dependencies, and a LabVIEW alias file).
ROOT_LISTING = (
    "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
    "-rwxr-xr-x         1,024 2026/06/17 15:59:02 Setup.ini.txt\n"
    "-rwxr-xr-x           900 2026/05/01 00:00:00 Setup.ini - Copy.txt\n"
    "-rwxr-xr-x            38 2026/01/27 20:59:00 ALD_Fiji.aliases\n"
    "-rwxr-xr-x   143,261,696 2026/01/27 20:59:00 ALD_Fiji.exe\n"
    "-rwxr-xr-x        27,648 2026/12/02 01:19:00 lvpidtkt.dll\n"
    "-rwxr-xr-x       434,509 2026/12/02 01:33:00 mxLvProvider.mxx\n"
    "drwxr-sr-x         4,096 2026/01/01 00:00:00 Logfile\n"
    "drwxr-sr-x         4,096 2026/01/01 00:00:00 Recipes\n"
)


class ListConfigFilesSubdirModeTests(TestCase):
    """config_subdir set to a real folder name (e.g. "configuration") - recursive, same shape as
    recipes.py's own list_recipes."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji5", config_subdir="configuration",
        )

    def test_no_config_subdir_returns_empty_without_any_remote_call(self):
        no_config_tool = SmartLabTool.objects.create(name="fiji9", kind="mvd", local_root=self._tmp.name)
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive") as mock_list:
            self.assertEqual(list_config_files(no_config_tool.as_source_config()), [])
        mock_list.assert_not_called()

    def test_lists_files_grouped_by_top_level_folder(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            files = list_config_files(self.tool.as_source_config())
        by_name = {f["name"]: f for f in files}
        self.assertIn("config.ini", by_name)
        self.assertEqual(by_name["config.ini"]["category"], "(root)")
        self.assertEqual(by_name["config.ini"]["fetch_path"], "configuration/config.ini")
        self.assertEqual(by_name["notes.txt"]["category"], "SW versions")
        self.assertEqual(by_name["notes.txt"]["fetch_path"], "configuration/SW versions/notes.txt")

    def test_non_flat_files_are_dropped_from_the_listing_entirely(self):
        # Not just denied a text preview - never shown at all (unlike changelog.log, a flat file
        # that still gets listed but with no raw-text preview - see GetConfigFileDetailTests).
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            files = list_config_files(self.tool.as_source_config())
        self.assertNotIn("manual.pdf", {f["name"] for f in files})

    def test_bare_folder_entries_are_excluded(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            files = list_config_files(self.tool.as_source_config())
        self.assertNotIn("SW versions", {f["name"] for f in files})

    def test_find_config_file_looks_up_by_stable_id(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            cfg = self.tool.as_source_config()
            files = list_config_files(cfg)
            some_id = files[0]["id"]
            found = find_config_file(cfg, some_id)
        self.assertIsNotNone(found)
        self.assertEqual(found["fetch_path"], files[0]["fetch_path"])

    def test_find_config_file_returns_none_for_unknown_id(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            self.assertIsNone(find_config_file(self.tool.as_source_config(), "not-a-real-id"))


class ListConfigFilesRootModeTests(TestCase):
    """config_subdir set to "." - a shallow, non-recursive listing of the tool's own root folder,
    never a full recursive walk (which would also pull in Logfile/Recipes)."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji1", config_subdir=".",
        )

    def test_lists_only_loose_root_files_not_logfile_or_recipes(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=ROOT_LISTING) as mock_list:
            files = list_config_files(self.tool.as_source_config())
        names = {f["name"] for f in files}
        self.assertEqual(names, {"Setup.ini.txt", "Setup.ini - Copy.txt"})
        self.assertNotIn("Logfile", names)
        self.assertNotIn("Recipes", names)
        # Shallow listing (list_remote_dir -> remote_sync.list_remote), never the recursive
        # tree-walker - a full recursive walk of the tool's own root would also enumerate every
        # run log and recipe file underneath Logfile/Recipes.
        mock_list.assert_called_once()

    def test_root_mode_entries_all_use_root_as_their_category(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=ROOT_LISTING):
            files = list_config_files(self.tool.as_source_config())
        self.assertTrue(all(f["category"] == "(root)" for f in files))

    def test_root_mode_fetch_path_is_the_bare_filename(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=ROOT_LISTING):
            files = list_config_files(self.tool.as_source_config())
        by_name = {f["name"]: f for f in files}
        self.assertEqual(by_name["Setup.ini.txt"]["fetch_path"], "Setup.ini.txt")


class GetConfigFileDetailTests(TestCase):
    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=endpoint, remote_subdir="Fiji5", config_subdir="configuration",
        )

    def test_text_file_returns_its_raw_content(self):
        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            import os
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write("[General]\nSomeKey=SomeValue\n")
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            cfg = self.tool.as_source_config()
            files = list_config_files(cfg)
            config_ini_id = next(f["id"] for f in files if f["name"] == "config.ini")
            detail = get_config_file_detail(cfg, config_ini_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["raw_text"], "[General]\nSomeKey=SomeValue\n")

    def test_non_text_file_has_no_raw_text_and_is_never_fetched(self):
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote") as mock_sync,
        ):
            cfg = self.tool.as_source_config()
            files = list_config_files(cfg)
            log_id = next(f["id"] for f in files if f["name"] == "changelog.log")
            detail = get_config_file_detail(cfg, log_id)
        self.assertIsNotNone(detail)
        self.assertIsNone(detail["raw_text"])
        mock_sync.assert_not_called()

    def test_unknown_id_returns_none(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            cfg = self.tool.as_source_config()
            self.assertIsNone(get_config_file_detail(cfg, "not-a-real-id"))


class FindActiveConfigFileTests(TestCase):
    """find_active_config_file() - the "In use" badge on the config file browser (see
    views.tool_configs/tool_config_detail) mirrors readers.py's own config-derived label lookups
    exactly, so a viewer can tell which of several similarly-named files is actually live."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )

    def test_heater_log_root_mode_picks_setup_ini_txt_not_the_copy(self):
        tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji1", config_subdir=".",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=ROOT_LISTING):
            entry = find_active_config_file(tool.as_source_config())
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Setup.ini.txt")

    def test_mvd_subdir_mode_picks_root_config_ini(self):
        tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji5", config_subdir="configuration",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=SUBDIR_TREE):
            entry = find_active_config_file(tool.as_source_config())
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "config.ini")
        self.assertEqual(entry["category"], "(root)")

    def test_mvd_prefers_root_config_ini_over_a_nested_default_copy(self):
        tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji5", config_subdir="configuration",
        )
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-rwxr-xr-x         1,000 2026/08/14 10:41:31 config.ini\n"
            "drwxr-sr-x         4,096 2026/08/14 10:41:31 default\n"
            "-rwxr-xr-x         1,000 2026/08/14 10:41:31 default/config.ini\n"
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree):
            entry = find_active_config_file(tool.as_source_config())
        self.assertEqual(entry["category"], "(root)")
        self.assertEqual(entry["fetch_path"], "configuration/config.ini")

    def test_none_without_config_subdir(self):
        tool = SmartLabTool.objects.create(
            name="fiji2", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji2",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote") as mock_list:
            self.assertIsNone(find_active_config_file(tool.as_source_config()))
        mock_list.assert_not_called()

    def test_none_when_no_matching_file_exists(self):
        tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji1", config_subdir=".",
        )
        listing_without_setup_ini = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-rwxr-xr-x           500 2026/06/17 15:59:02 notes.txt\n"
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=listing_without_setup_ini):
            self.assertIsNone(find_active_config_file(tool.as_source_config()))

    def test_none_for_a_kind_with_no_config_derived_label_lookup(self):
        tool = SmartLabTool.objects.create(
            name="cobra", kind="cobra_job", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Cobra", config_subdir=".",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=ROOT_LISTING):
            self.assertIsNone(find_active_config_file(tool.as_source_config()))
