"""
Tests for NEMO_smart_lab.remote_cache - every test mocks NEMO_smart_lab.remote_sync's actual
transfer functions (list_remote/sync_file_from_remote/sync_tool_from_remote), so nothing here ever
touches the network. Uses Django's real cache framework (whatever settings.CACHES configures for
tests - LocMemCache in this project) rather than mocking `django.core.cache.cache` itself, so TTL
gating is exercised for real; `cache.clear()` in setUp keeps tests isolated from each other.
"""

import os
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.remote_cache import ensure_cached, list_remote_dir
from NEMO_smart_lab.remote_sync import RemoteSyncError

RAW_LISTING = """drwxr-sr-x         36,864 2026/09/02 14:58:05 .
-rwxr-xr-x         28,929 2026/05/14 17:30:44 2026_05_14-19-19-39_Thermal Al2O3 STANDARD.txt.txt
-rwxr-xr-x          1,709 2026/05/14 20:40:56 2026_05_14-22-40-19_20 - STANDBY 200C.txt
drwxr-sr-x          4,096 2026/09/09 07:42:07 some_run_folder
"""


class ListRemoteDirTests(TestCase):
    def setUp(self):
        cache.clear()
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )

    def test_parses_listing_lines(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=RAW_LISTING):
            entries = list_remote_dir(self.endpoint, "Fiji1/Logfile/Heater Data")
        names = {name: (mtime, size, is_dir) for name, mtime, size, is_dir in entries}
        self.assertNotIn(".", names)  # the directory's own self-entry is skipped
        self.assertIn("2026_05_14-22-40-19_20 - STANDBY 200C.txt", names)
        _mtime, size, is_dir = names["2026_05_14-22-40-19_20 - STANDBY 200C.txt"]
        self.assertEqual(size, 1709)
        self.assertFalse(is_dir)
        self.assertTrue(names["some_run_folder"][2])  # is_dir

    def test_second_call_within_ttl_does_not_re_list(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=RAW_LISTING) as mock_list:
            list_remote_dir(self.endpoint, "Fiji1/Logfile/Heater Data")
            list_remote_dir(self.endpoint, "Fiji1/Logfile/Heater Data")
        mock_list.assert_called_once()

    def test_different_paths_are_cached_independently(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=RAW_LISTING) as mock_list:
            list_remote_dir(self.endpoint, "Fiji1/Logfile/Heater Data")
            list_remote_dir(self.endpoint, "Fiji2/Logfile/Heater Data")
        self.assertEqual(mock_list.call_count, 2)

    def test_listing_failure_propagates(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", side_effect=RemoteSyncError("boom")):
            with self.assertRaises(RemoteSyncError):
                list_remote_dir(self.endpoint, "Fiji1/Logfile/Heater Data")


class EnsureCachedTests(TestCase):
    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1",
            kind="heater_log",
            local_root=self._tmp.name,
            sync_endpoint=self.endpoint,
            remote_subdir="Fiji1",
        )

    def test_fetches_and_returns_local_path(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", return_value="ok") as mock_sync:
            path = ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        self.assertEqual(path, os.path.join(self.tool.local_root, "Logfile/Heater Data/run1.txt"))
        mock_sync.assert_called_once()
        _local_path, endpoint, remote_relpath_full = mock_sync.call_args[0]
        self.assertIs(endpoint, self.endpoint)
        self.assertEqual(remote_relpath_full, "Fiji1/Logfile/Heater Data/run1.txt")

    def test_updates_last_sync_fields_on_success(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", return_value="sent 1 file"):
            ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        self.tool.refresh_from_db()
        self.assertTrue(self.tool.last_sync_ok)
        self.assertEqual(self.tool.last_sync_message, "sent 1 file")
        self.assertIsNotNone(self.tool.last_synced)

    def test_second_call_within_ttl_skips_refetch(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", return_value="ok") as mock_sync:
            ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
            ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        mock_sync.assert_called_once()

    def test_directory_fetch_uses_sync_tool_from_remote(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_tool_from_remote", return_value="ok") as mock_sync:
            path = ensure_cached(self.tool, "log/data/20260101_000000_Recipe", is_dir=True)
        self.assertEqual(path, os.path.join(self.tool.local_root, "log/data/20260101_000000_Recipe"))
        local_root_arg, endpoint, remote_subdir_arg = mock_sync.call_args[0]
        self.assertEqual(remote_subdir_arg, "Fiji1/log/data/20260101_000000_Recipe")

    def test_falls_back_to_stale_local_copy_on_failure(self):
        local_path = os.path.join(self.tool.local_root, "Logfile", "Heater Data", "run1.txt")
        os.makedirs(os.path.dirname(local_path))
        with open(local_path, "w") as f:
            f.write("stale but present")

        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=RemoteSyncError("down")):
            path = ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        self.assertEqual(path, local_path)
        self.tool.refresh_from_db()
        # A graceful stale-serve isn't a recorded sync failure - last_sync_ok is left untouched.
        self.assertIsNone(self.tool.last_sync_ok)

    def test_raises_when_no_local_fallback_exists(self):
        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=RemoteSyncError("down")):
            with self.assertRaises(RemoteSyncError):
                ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        self.tool.refresh_from_db()
        self.assertFalse(self.tool.last_sync_ok)
        self.assertEqual(self.tool.last_sync_message, "down")
