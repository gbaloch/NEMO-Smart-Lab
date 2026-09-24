"""Tests for tools that read through a remote sync endpoint (Oak), including listing failures."""

import os
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab import remote_sync
from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.readers import get_tool_history, get_tool_summary
from NEMO_smart_lab.tests.readers.helpers import _write_heater_log
from NEMO_smart_lab.tests.readers import test_heater_log as _test_heater_log


class RemoteModeHeaterLogTests(TestCase):
    """Proves readers.py's lazy-caching wiring actually works end to end: "most recent run" is
    resolved from a *remote* listing (never touching local mtimes, which would silently miss a
    run that was never fetched before), and only that one winning run is ever fetched - all
    without ever having run `sync_remote_data` first. Every network call
    (NEMO_smart_lab.remote_sync.list_remote/sync_file_from_remote) is mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1",
            kind="heater_log",
            local_root=self._tmp.name,
            sync_endpoint=endpoint,
            remote_subdir="Fiji1",
            on_threshold_c=35.0,
        )

    def test_most_recent_run_resolved_from_remote_listing_and_fetched_on_demand(self):
        raw_listing = (
            "drwxr-sr-x       4,096 2026/05/01 00:00:00 .\n"
            "-rwxr-xr-x       1,000 2026/05/01 00:00:00 old_run.txt\n"
            "-rwxr-xr-x       1,000 2026/06/01 00:00:00 new_run.txt\n"
        )
        row = ["0.0"] + ["200.0"] * 12 + ["0.0", "0.0", "0.0", "0", "New Recipe", ""]

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            _write_heater_log(local_path, _test_heater_log.HeaterLogTests.FULL_HEADER, [row])
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=raw_listing),
            patch(
                "NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file
            ) as mock_sync,
        ):
            cfg = self.tool.as_source_config()
            self.assertIn("remote_tool", cfg)
            summary = get_tool_summary("fiji1", cfg)

        self.assertNotIn("error", summary)
        self.assertEqual(summary["run_id"], "new_run.txt")
        self.assertEqual(summary["recipe"], "New Recipe")
        # Only the winning run was ever fetched - "old_run.txt" was listed but never downloaded.
        mock_sync.assert_called_once()
        self.assertIn("new_run.txt", mock_sync.call_args[0][2])
        self.assertNotIn("old_run.txt", mock_sync.call_args[0][2])


class RemoteListingFailureTests(TestCase):
    """A transient remote-host failure (Oak unreachable, DNS blip, etc.) while listing a tool's
    runs must degrade to a friendly "error" on the summary, not an unhandled 500 - confirmed live:
    a real DNS resolution failure against dtn.oak.stanford.edu crashed the tool overview page with
    a raw RemoteSyncError traceback, because _list_heater_log_entries (unlike the rest of this
    module's remote_cache callers) didn't convert that into the ToolDataError get_tool_summary
    already knows how to catch. Covers the fix for all three of the listing helpers that had this
    same gap (heater_log/mvd/waferlog all list via remote_cache.list_remote_dir the same way)."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji2",
            kind="heater_log",
            local_root=self._tmp.name,
            sync_endpoint=endpoint,
            remote_subdir="Fiji2",
            on_threshold_c=35.0,
        )

    def test_a_failed_listing_is_remembered_so_a_burst_of_requests_fails_fast(self):
        # Regression: a wedged SSH connection made every listing call wait out its own full 60s
        # rsync timeout, repeatedly, across one page's several listing calls.
        cfg = self.tool.as_source_config()
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.list_remote",
            side_effect=remote_sync.RemoteSyncError("rsync timed out after 60s"),
        ) as mock_list:
            first = get_tool_summary("fiji2", cfg)
            second = get_tool_summary("fiji2", cfg)
        self.assertIn("timed out", first["error"])
        self.assertIn("timed out", second["error"])
        self.assertEqual(mock_list.call_count, 1)

    def test_dns_failure_listing_runs_yields_friendly_error_not_a_crash(self):
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.list_remote",
            side_effect=remote_sync.RemoteSyncError(
                "rsync exited 255: ssh: Could not resolve hostname dtn.oak.stanford.edu: Temporary failure in name resolution"
            ),
        ):
            cfg = self.tool.as_source_config()
            summary = get_tool_summary("fiji2", cfg)

        self.assertIn("error", summary)
        self.assertIn("dtn.oak.stanford.edu", summary["error"])

    def test_dns_failure_listing_runs_history_page_degrades_to_empty_not_a_crash(self):
        with patch(
            "NEMO_smart_lab.remote_cache.remote_sync.list_remote",
            side_effect=remote_sync.RemoteSyncError("name resolution failure"),
        ):
            cfg = self.tool.as_source_config()
            runs, total = get_tool_history(cfg)

        self.assertEqual(runs, [])
        self.assertEqual(total, 0)
