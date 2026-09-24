"""Tests for reading channel labels out of an mvd tool's live config.ini."""

import csv
import os
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.readers import get_chart_groups
from NEMO_smart_lab.tests.readers.helpers import MVD_SUM_TEMPLATE


class ConfigIniHeaterLabelParsingTests(TestCase):
    """_mvd_config_heater_labels' own parsing - both real formats confirmed live: fiji5's
    `HTR13 = label:"UPPER", setpt:150, ...` and mvd's own `HTR6 = "EXHAUST TRAP",80,180,...`."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji5", config_subdir="configuration",
        )

    def _tree_with_config_ini(self, config_ini_text):
        return (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-rwxr-xr-x         1,000 2026/08/14 10:41:31 config.ini\n"
        ), config_ini_text

    def test_parses_fiji5_style_label_colon_quoted_format(self):
        from NEMO_smart_lab.readers import _mvd_config_heater_labels

        tree, text = self._tree_with_config_ini(
            '[heaters]\nHTR13 = label:"UPPER", setpt:150, alarmhi:350\nHTR14 = label:"", setpt:0\n'
        )

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(text)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            labels = _mvd_config_heater_labels(self.tool.as_source_config())
        self.assertEqual(labels.get("13"), "UPPER")
        # A blank label in config.ini itself is correctly treated as "nothing to offer" here too.
        self.assertNotIn("14", labels)

    def test_parses_mvd_style_bare_quoted_format(self):
        from NEMO_smart_lab.readers import _mvd_config_heater_labels

        tree, text = self._tree_with_config_ini(
            'HTR6 = "EXHAUST TRAP",80,180,5,3,180,5.000,3.375,0.675,0,100\nHTR17 = "",0,75,5,3,180\n'
        )

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(text)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            labels = _mvd_config_heater_labels(self.tool.as_source_config())
        self.assertEqual(labels.get("6"), "EXHAUST TRAP")
        self.assertNotIn("17", labels)

    def test_prefers_the_root_config_ini_over_a_nested_default_copy(self):
        from NEMO_smart_lab.readers import _mvd_config_heater_labels

        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-rwxr-xr-x         1,000 2026/08/14 10:41:31 config.ini\n"
            "drwxr-sr-x         4,096 2026/08/14 10:41:31 default\n"
            "-rwxr-xr-x         1,000 2026/08/14 10:41:31 default/config.ini\n"
        )

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            # The factory-default copy has a different (stale) name - proves the root one, not
            # this one, is what gets read.
            content = 'HTR6 = "Stale default name",0,0\n' if "default" in remote_relpath_full else 'HTR6 = "Live name",0,0\n'
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(content)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            labels = _mvd_config_heater_labels(self.tool.as_source_config())
        self.assertEqual(labels.get("6"), "Live name")

    def test_returns_empty_dict_when_config_subdir_not_set(self):
        from NEMO_smart_lab.readers import _mvd_config_heater_labels

        no_config_tool = SmartLabTool.objects.create(
            name="mvd-noconfig", kind="mvd", local_root=self._tmp.name, sync_endpoint=self.endpoint, remote_subdir="MVD2",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive") as mock_list:
            labels = _mvd_config_heater_labels(no_config_tool.as_source_config())
        self.assertEqual(labels, {})
        mock_list.assert_not_called()

    def test_returns_empty_dict_for_non_mvd_kind(self):
        from NEMO_smart_lab.readers import _mvd_config_heater_labels

        self.assertEqual(_mvd_config_heater_labels({"kind": "heater_log", "config_subdir": "configuration"}), {})


class MvdConfigMfcLabelTests(TestCase):
    """_mvd_config_mfc_labels()/_mvd_mfc_display_name() - unlike heater channels, no per-run
    DAT/SUM file carries any MFC label field at all (confirmed live), so config.ini's own "[mfc]"
    section is the *only* source for a real name - without it every MFC series shows only its raw
    column name ("MFC0_reading", "MFC1_setpoint", ...)."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji5", config_subdir="configuration",
        )

    def _mock_config_ini(self, text):
        tree = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            "-rwxr-xr-x         1,000 2026/08/14 10:41:31 config.ini\n"
        )

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(text)
            return "ok"

        return (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive", return_value=tree),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        )

    def test_parses_real_mfc_section_bare_quoted_format(self):
        from NEMO_smart_lab.readers import _mvd_config_mfc_labels

        text = '[mfc]\nMFC0 = "CARRIER (Ar)",0,100,0.0,5.0,0.0,5.0,1.000,10,10\nMFC1 = "PLASMA (Ar)",0,500\n'
        mock_list, mock_sync = self._mock_config_ini(text)
        with mock_list, mock_sync:
            labels = _mvd_config_mfc_labels(self.tool.as_source_config())
        self.assertEqual(labels.get("0"), "CARRIER (Ar)")
        self.assertEqual(labels.get("1"), "PLASMA (Ar)")

    def test_returns_empty_dict_without_config_subdir(self):
        from NEMO_smart_lab.readers import _mvd_config_mfc_labels

        no_config_tool = SmartLabTool.objects.create(
            name="mvd-noconfig", kind="mvd", local_root=self._tmp.name, sync_endpoint=self.endpoint, remote_subdir="MVD2",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote_recursive") as mock_list:
            self.assertEqual(_mvd_config_mfc_labels(no_config_tool.as_source_config()), {})
        mock_list.assert_not_called()

    def test_display_name_renames_setpoint_and_reading_columns(self):
        from NEMO_smart_lab.readers import _mvd_mfc_display_name

        labels = {"0": "CARRIER (Ar)", "1": "PLASMA (Ar)"}
        self.assertEqual(_mvd_mfc_display_name("MFC0_setpoint", labels), "CARRIER (Ar) (MFC0) setpoint")
        self.assertEqual(_mvd_mfc_display_name("MFC0_reading", labels), "CARRIER (Ar) (MFC0) reading")
        # mvd's own (non-fiji5) DAT format has no setpoint/reading split, just a bare "MFC0".
        self.assertEqual(_mvd_mfc_display_name("MFC1", labels), "PLASMA (Ar) (MFC1)")

    def test_display_name_unchanged_when_no_label_for_that_channel(self):
        from NEMO_smart_lab.readers import _mvd_mfc_display_name

        self.assertEqual(_mvd_mfc_display_name("MFC5_setpoint", {"0": "CARRIER (Ar)"}), "MFC5_setpoint")

    def test_display_name_unchanged_for_a_non_mfc_column(self):
        from NEMO_smart_lab.readers import _mvd_mfc_display_name

        self.assertEqual(_mvd_mfc_display_name("PlasmaForwardPower", {"0": "CARRIER (Ar)"}), "PlasmaForwardPower")

    def test_chart_groups_use_the_real_mfc_label_end_to_end(self):
        run_dir = os.path.join(self._tmp.name, "log", "data", "20260101_000000_A")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "20260101_000000_A_SUM.txt"), "w", encoding="utf-8") as f:
            f.write(MVD_SUM_TEMPLATE.format(recipe="Recipe A"))
        with open(os.path.join(run_dir, "20260101_000000_A_DAT.txt"), "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Time(sec)", "MFC0_setpoint(sccm)", "MFC0_reading(sccm)"])
            writer.writerow(["0.5", "10.0", "9.8"])

        mock_list, mock_sync = self._mock_config_ini('[mfc]\nMFC0 = "CARRIER (Ar)",0,100\n')
        cfg = {
            "kind": "mvd", "root": self._tmp.name, "on_threshold_pct": 0.5,
            "config_subdir": "configuration", "remote_tool": self.tool,
        }
        with mock_list, mock_sync:
            # An explicit run_id (rather than "find the latest run") skips the remote *listing*
            # step entirely and goes straight to remote_cache.ensure_cached for this one known
            # path - which, since it already exists locally (written above), is trusted outright
            # with no network call at all (see remote_cache.ensure_cached's own docstring).
            groups = get_chart_groups(cfg, run_id="20260101_000000_A")
        by_key = {g["key"]: g for g in groups}
        self.assertEqual(
            set(by_key["sccm"]["series"].keys()), {"CARRIER (Ar) (MFC0) setpoint", "CARRIER (Ar) (MFC0) reading"}
        )
