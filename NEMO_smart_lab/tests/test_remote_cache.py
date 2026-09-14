"""
Tests for NEMO_smart_lab.remote_cache - every test mocks NEMO_smart_lab.remote_sync's actual
transfer functions (list_remote/sync_file_from_remote/sync_tool_from_remote), so nothing here ever
touches the network. Uses Django's real cache framework (whatever settings.CACHES configures for
tests - LocMemCache in this project) rather than mocking `django.core.cache.cache` itself, so TTL
gating is exercised for real; `cache.clear()` in setUp keeps tests isolated from each other.
"""

import os
import tempfile
import threading
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.remote_cache import ensure_cached, ensure_cached_many, list_remote_dir, warm_in_background
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
        self.assertEqual(path, os.path.join(self.tool.local_root, "Logfile", "Heater Data", "run1.txt"))
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
        self.assertEqual(path, os.path.join(self.tool.local_root, "log", "data", "20260101_000000_Recipe"))
        local_root_arg, endpoint, remote_subdir_arg = mock_sync.call_args[0]
        self.assertEqual(remote_subdir_arg, "Fiji1/log/data/20260101_000000_Recipe")

    def test_falls_back_to_stale_local_copy_on_failure(self):
        # A local copy that only appears *during* a failed fetch attempt (rsync's own --partial
        # flag can leave a partial file behind on a failed transfer - see remote_sync's actual
        # rsync invocation) - not a copy that was already there before this call, which now never
        # reaches _fetch at all (see test_local_existence_skips_network_entirely below).
        local_path = os.path.join(self.tool.local_root, "Logfile", "Heater Data", "run1.txt")

        def fake_partial_then_fail(local_path_arg, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path_arg), exist_ok=True)
            with open(local_path_arg, "w") as f:
                f.write("partial content from an interrupted transfer")
            raise RemoteSyncError("down")

        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_partial_then_fail
        ) as mock_sync:
            path = ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        mock_sync.assert_called_once()
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

    def test_local_existence_skips_network_entirely(self):
        """A local copy that already exists - even on a stone-cold cache (nothing in the "synced"
        TTL cache saying so) - is trusted outright, no fetch attempted at all. This is the fix for
        the real incident where a process restart wiping the in-memory freshness cache turned "a
        few hundred already-local, already-finished files" into a full re-verification round trip
        for every single one of them."""
        local_path = os.path.join(self.tool.local_root, "Logfile", "Heater Data", "run1.txt")
        os.makedirs(os.path.dirname(local_path))
        with open(local_path, "w") as f:
            f.write("already here")

        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote") as mock_sync:
            path = ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        mock_sync.assert_not_called()
        self.assertEqual(path, local_path)

    def test_failed_fetch_is_not_retried_within_failure_ttl(self):
        """A path with no local fallback that just failed shouldn't pay another real network round
        trip on the very next call - see FAILURE_TTL. The second call here raises immediately from
        the remembered error, without invoking sync_file_from_remote again."""
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=RemoteSyncError("down")
        ) as mock_sync:
            with self.assertRaises(RemoteSyncError):
                ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
            with self.assertRaises(RemoteSyncError):
                ensure_cached(self.tool, "Logfile/Heater Data/run1.txt")
        mock_sync.assert_called_once()


class EnsureCachedManyTests(TestCase):
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

    def test_already_local_paths_are_never_fetched(self):
        local_path = os.path.join(self.tool.local_root, "Logfile", "Heater Data", "already_local.txt")
        os.makedirs(os.path.dirname(local_path))
        with open(local_path, "w") as f:
            f.write("here")

        with patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", return_value="ok") as mock_sync:
            ensure_cached_many(
                self.tool,
                ["Logfile/Heater Data/already_local.txt", "Logfile/Heater Data/missing.txt"],
            )
        mock_sync.assert_called_once()
        self.assertEqual(mock_sync.call_args[0][2], "Fiji1/Logfile/Heater Data/missing.txt")

    def test_a_failed_path_is_not_retried_within_failure_ttl(self):
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=RemoteSyncError("down")
        ) as mock_sync:
            ensure_cached_many(self.tool, ["Logfile/Heater Data/missing.txt"])
            ensure_cached_many(self.tool, ["Logfile/Heater Data/missing.txt"])
        mock_sync.assert_called_once()


class WarmInBackgroundTests(TestCase):
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

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_fetches_in_background_and_returns_immediately(self):
        # _record_sync_result (a tool.save()) is mocked out here purely to keep this test's
        # background thread from touching the ORM at all - a plain TestCase's per-test transaction
        # isn't safe for a second thread to write through concurrently (confirmed live: doing so
        # here intermittently raised a real "database table is locked" from SQLite); the actual
        # thing under test is warm_in_background's own dedup/threading behavior, not
        # _record_sync_result, which already has its own coverage in EnsureCachedTests.
        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", return_value="ok") as mock_sync,
            patch("NEMO_smart_lab.remote_cache._record_sync_result"),
        ):
            warm_in_background(self.tool, "test-key", ["Logfile/Heater Data/run1.txt"])
            # Returns without waiting on the fetch - the call above must not itself block.
            self.assertTrue(self._wait_for(lambda: mock_sync.call_count >= 1))

    def test_a_second_call_for_the_same_key_while_running_is_a_no_op(self):
        started = threading.Event()
        release = threading.Event()

        def slow_fetch(local_path_arg, endpoint, remote_relpath_full):
            started.set()
            release.wait(timeout=2.0)
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=slow_fetch) as mock_sync,
            patch("NEMO_smart_lab.remote_cache._record_sync_result"),
        ):
            warm_in_background(self.tool, "dup-key", ["Logfile/Heater Data/run1.txt"])
            self.assertTrue(started.wait(timeout=2.0))
            # A repeat call for the same key while the first is still running must not spawn a
            # second, redundant fetch of the same path.
            warm_in_background(self.tool, "dup-key", ["Logfile/Heater Data/run1.txt"])
            release.set()
            self.assertTrue(self._wait_for(lambda: mock_sync.call_count >= 1))
            time.sleep(0.1)
        self.assertEqual(mock_sync.call_count, 1)
