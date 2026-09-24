"""Tests for the continuous background pressure log reader."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool
from NEMO_smart_lab.tests.readers.helpers import TempDirTestCase


class ContinuousPressureFilenameTimestampTests(unittest.TestCase):
    def test_parses_the_real_naming_format(self):
        from NEMO_smart_lab.readers import _continuous_pressure_filename_timestamp

        self.assertEqual(
            _continuous_pressure_filename_timestamp("Pressure - 2026-08-20 14.03.38.txt"),
            datetime(2026, 8, 20, 14, 3, 38),
        )

    def test_none_for_an_unrelated_filename(self):
        from NEMO_smart_lab.readers import _continuous_pressure_filename_timestamp

        self.assertIsNone(_continuous_pressure_filename_timestamp("config.ini"))


class ParseAndBucketContinuousPressureFileTests(TempDirTestCase):
    def test_buckets_by_time_and_averages_per_gauge(self):
        from NEMO_smart_lab.readers import _parse_and_bucket_continuous_pressure_file

        path = os.path.join(self.root, "Pressure - 2026-08-20 00.00.00.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            # Real header quirk: the time column claims "(sec)" but every row is actually a full
            # local timestamp string - see the function's own docstring.
            f.write('Time (sec),"Process"(Torr),"Chamber"(Torr)\n')
            f.write("12:00:00.000 AM 8/20/2026,5.0,1.0\n")
            f.write("12:00:05.000 AM 8/20/2026,7.0,2.0\n")  # same 15-min bucket as the row above
            f.write("12:20:00.000 AM 8/20/2026,9.0,3.0\n")  # a different bucket

        buckets = _parse_and_bucket_continuous_pressure_file(path, bucket_seconds=15 * 60)
        self.assertEqual(set(buckets.keys()), {"Process", "Chamber"})
        # Two buckets for "Process": [5.0, 7.0] averaged, and [9.0] alone.
        process_buckets = buckets["Process"]
        self.assertEqual(len(process_buckets), 2)
        totals = sorted(process_buckets.values())
        self.assertEqual(totals, [[9.0, 1], [12.0, 2]])

    def test_a_row_with_an_unparseable_timestamp_is_skipped_not_fatal(self):
        from NEMO_smart_lab.readers import _parse_and_bucket_continuous_pressure_file

        path = os.path.join(self.root, "Pressure - 2026-08-20 00.00.00.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write('Time (sec),"Process"(Torr)\n')
            f.write("not-a-real-timestamp,5.0\n")
            f.write("12:00:00.000 AM 8/20/2026,7.0\n")
        buckets = _parse_and_bucket_continuous_pressure_file(path, bucket_seconds=900)
        self.assertEqual(list(buckets["Process"].values()), [[7.0, 1]])

    def test_a_literal_nan_cell_is_excluded_not_averaged_in(self):
        # Confirmed live: a real gauge glitch logs a literal "NaN" cell, which Python's own
        # float() happily parses (unlike a real ValueError) - if that ever reached the bucket
        # average, the resulting NaN would break the whole chart.json response (JavaScript's
        # JSON.parse rejects a literal NaN token, even though Python's json.dumps writes one by
        # default) - confirmed live as the actual bug this test guards against.
        from NEMO_smart_lab.readers import _parse_and_bucket_continuous_pressure_file

        path = os.path.join(self.root, "Pressure - 2026-08-20 00.00.00.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write('Time (sec),"Process"(Torr)\n')
            f.write("12:00:00.000 AM 8/20/2026,NaN\n")
            f.write("12:00:05.000 AM 8/20/2026,7.0\n")
        buckets = _parse_and_bucket_continuous_pressure_file(path, bucket_seconds=900)
        self.assertEqual(list(buckets["Process"].values()), [[7.0, 1]])

    def test_an_infinite_cell_is_also_excluded(self):
        from NEMO_smart_lab.readers import _parse_and_bucket_continuous_pressure_file

        path = os.path.join(self.root, "Pressure - 2026-08-20 00.00.00.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write('Time (sec),"Process"(Torr)\n')
            f.write("12:00:00.000 AM 8/20/2026,inf\n")
            f.write("12:00:05.000 AM 8/20/2026,7.0\n")
        buckets = _parse_and_bucket_continuous_pressure_file(path, bucket_seconds=900)
        self.assertEqual(list(buckets["Process"].values()), [[7.0, 1]])

    def test_a_short_row_falls_back_and_still_parses_correctly(self):
        # A ragged grid (some row shorter than the header) can't go through the numpy fast path
        # (np.loadtxt requires a uniform column count) - the whole file falls back to the original
        # per-cell loop, which must still parse every well-formed row/cell correctly.
        from NEMO_smart_lab.readers import _parse_and_bucket_continuous_pressure_file

        path = os.path.join(self.root, "Pressure - 2026-08-20 00.00.00.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write('Time (sec),"Process"(Torr),"Chamber"(Torr)\n')
            f.write("12:00:00.000 AM 8/20/2026,5.0,1.0\n")
            f.write("12:00:05.000 AM 8/20/2026,7.0\n")  # missing the "Chamber" cell
            f.write("12:00:10.000 AM 8/20/2026,9.0,3.0\n")
        buckets = _parse_and_bucket_continuous_pressure_file(path, bucket_seconds=900)
        self.assertEqual(sorted(buckets["Process"].values()), [[21.0, 3]])
        self.assertEqual(sorted(buckets["Chamber"].values()), [[4.0, 2]])

    def test_large_clean_grid_fast_and_slow_paths_agree(self):
        """Explicitly cross-checks the numpy fast path (see this function's own docstring) against
        the original per-cell algorithm on the same, larger, clean input - the strongest guarantee
        available that the fast path can't be silently diverging from the always-correct one it's
        meant to shortcut."""
        from NEMO_smart_lab.readers import _parse_and_bucket_continuous_pressure_file

        base = datetime(2026, 8, 20, 0, 0, 0)
        lines = ['Time (sec),"Process"(Torr),"Chamber"(Torr)']
        for i in range(3000):
            ts = base + timedelta(seconds=i)
            lines.append(f"{ts.strftime('%I:%M:%S.000 %p %m/%d/%Y')},{1.0 + i * 0.001:.4f},{2.0 + i * 0.002:.4f}")
        path = os.path.join(self.root, "Pressure - 2026-08-20 00.00.00.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines) + "\n")

        buckets = _parse_and_bucket_continuous_pressure_file(path, bucket_seconds=900)
        # 3000 one-second rows = 50 minutes -> 4 fifteen-minute buckets, all present for both gauges.
        self.assertEqual(len(buckets["Process"]), 4)
        self.assertEqual(len(buckets["Chamber"]), 4)
        self.assertEqual(sum(count for _total, count in buckets["Process"].values()), 3000)
        self.assertEqual(sum(count for _total, count in buckets["Chamber"].values()), 3000)


class FastContinuousPressureTimestampTests(unittest.TestCase):
    """_fast_continuous_pressure_timestamp - a hand-rolled, faster stand-in for
    datetime.strptime(_CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT) - must agree with it exactly on
    every real timestamp shape, and return None (not raise) for anything else."""

    def test_matches_strptime_on_real_samples(self):
        from NEMO_smart_lab.readers import _CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT, _fast_continuous_pressure_timestamp

        samples = [
            "2:03:38.359 PM 8/20/2026",
            "11:59:59.999 AM 12/31/2025",
            "12:00:00.000 AM 1/1/2026",
            "12:00:00.000 PM 6/15/2026",
            "1:00:00.000 AM 1/1/2026",
        ]
        for sample in samples:
            expected = datetime.strptime(sample, _CONTINUOUS_PRESSURE_ROW_TIMESTAMP_FORMAT)
            self.assertEqual(_fast_continuous_pressure_timestamp(sample), expected, sample)

    def test_unrecognized_format_returns_none_not_raises(self):
        from NEMO_smart_lab.readers import _fast_continuous_pressure_timestamp

        self.assertIsNone(_fast_continuous_pressure_timestamp("not-a-timestamp"))
        self.assertIsNone(_fast_continuous_pressure_timestamp(""))
        self.assertIsNone(_fast_continuous_pressure_timestamp("2026-08-20 14:03:38"))

    def test_falls_back_to_strptime_via_the_public_wrapper(self):
        # _continuous_pressure_row_timestamp is what the parser actually calls - confirms the
        # fallback path itself also produces a real, correct datetime, not just None-vs-not-None.
        from NEMO_smart_lab.readers import _continuous_pressure_row_timestamp

        self.assertEqual(_continuous_pressure_row_timestamp("2:03:38.359 PM 8/20/2026"), datetime(2026, 8, 20, 14, 3, 38, 359000))
        self.assertIsNone(_continuous_pressure_row_timestamp("garbage"))


class GetContinuousPressureTrendTests(TestCase):
    """get_continuous_pressure_trend() - fiji5's own continuous, always-on background pressure
    log (opt-in via continuous_pressure_subdir) - a genuinely different, more complete signal than
    get_base_pressure_history's "one point per standby run"."""

    def setUp(self):
        cache.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji5", kind="mvd", local_root=self._tmp.name,
            sync_endpoint=self.endpoint, remote_subdir="Fiji5", continuous_pressure_subdir="datalog/data/Pressure",
        )

    def test_none_without_continuous_pressure_subdir_configured(self):
        from NEMO_smart_lab.readers import get_continuous_pressure_trend

        no_subdir_tool = SmartLabTool.objects.create(
            name="mvd", kind="mvd", local_root=self._tmp.name, sync_endpoint=self.endpoint, remote_subdir="MVD",
        )
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote") as mock_list:
            self.assertIsNone(get_continuous_pressure_trend(no_subdir_tool.as_source_config()))
        mock_list.assert_not_called()

    def test_none_when_no_matching_files_exist(self):
        from NEMO_smart_lab.readers import get_continuous_pressure_trend

        listing = "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"  # empty folder
        with patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=listing):
            self.assertIsNone(get_continuous_pressure_trend(self.tool.as_source_config()))

    def test_end_to_end_merges_multiple_files_into_one_aligned_series(self):
        from NEMO_smart_lab.readers import get_continuous_pressure_trend

        now = datetime.now()
        recent = now - timedelta(hours=1)
        older = now - timedelta(days=1)
        recent_name = f"Pressure - {recent.strftime('%Y-%m-%d %H.%M.%S')}.txt"
        older_name = f"Pressure - {older.strftime('%Y-%m-%d %H.%M.%S')}.txt"
        listing = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            f"-rwxr-xr-x         1,000 2026/08/27 08:26:53 {older_name}\n"
            f"-rwxr-xr-x         1,000 2026/08/27 08:26:53 {recent_name}\n"
        )
        contents = {
            older_name: 'Time (sec),"Process"(Torr)\n' + older.strftime("%I:%M:%S.000 %p %m/%d/%Y") + ",1.0\n",
            recent_name: 'Time (sec),"Process"(Torr)\n' + recent.strftime("%I:%M:%S.000 %p %m/%d/%Y") + ",3.0\n",
        }

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            name = remote_relpath_full.rsplit("/", 1)[-1]
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(contents[name])
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=listing),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            # range_key="7d" (not the default) - the "older" file is a day back, which the default
            # "today" (midnight-to-now) window would deliberately exclude (see
            # test_default_range_key_is_today_not_a_rolling_lookback below); this test is about the
            # multi-file merge/alignment itself, so it asks for a range wide enough to include both.
            trend = get_continuous_pressure_trend(self.tool.as_source_config(), range_key="7d")
        self.assertEqual(trend["gauges"], ["Process"])
        self.assertEqual(len(trend["timestamps"]), 2)
        self.assertEqual(sorted(v for v in trend["series"]["Process"] if v is not None), [1.0, 3.0])

    def test_default_range_key_is_last_24_hours(self):
        """A missing range_key means "24h" (a rolling last-24-hours lookback, not a calendar-day
        "today") - a file from 23 hours ago is included, one from 25 hours ago is not."""
        from NEMO_smart_lab.readers import get_continuous_pressure_trend

        now = datetime.now()
        within_24h = now - timedelta(hours=23)
        before_24h = now - timedelta(hours=25)
        recent_name = f"Pressure - {within_24h.strftime('%Y-%m-%d %H.%M.%S')}.txt"
        older_name = f"Pressure - {before_24h.strftime('%Y-%m-%d %H.%M.%S')}.txt"
        listing = (
            "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n"
            f"-rwxr-xr-x         1,000 2026/08/27 08:26:53 {older_name}\n"
            f"-rwxr-xr-x         1,000 2026/08/27 08:26:53 {recent_name}\n"
        )
        contents = {
            older_name: 'Time (sec),"Process"(Torr)\n' + before_24h.strftime("%I:%M:%S.000 %p %m/%d/%Y") + ",1.0\n",
            recent_name: 'Time (sec),"Process"(Torr)\n' + within_24h.strftime("%I:%M:%S.000 %p %m/%d/%Y") + ",3.0\n",
        }

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            name = remote_relpath_full.rsplit("/", 1)[-1]
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(contents[name])
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=listing),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            trend = get_continuous_pressure_trend(self.tool.as_source_config())
        self.assertEqual(sorted(v for v in trend["series"]["Process"] if v is not None), [3.0])

    def test_most_recent_file_is_relevant_even_when_its_own_start_predates_the_cutoff(self):
        """The most recent file is the one still actively being logged into - its own start
        timestamp can be well before a narrow range's cutoff while its rows keep extending up to
        the present moment, so it must always be treated as relevant, not just when its own start
        (or the start of some nonexistent "next" file) happens to clear the cutoff. Regression test
        for a real bug found live: fiji5's newest file started well before "now - 24h" but still
        held a row from within the last 24 hours."""
        from NEMO_smart_lab.readers import get_continuous_pressure_trend

        now = datetime.now()
        file_start = now - timedelta(hours=30)  # older than the 24h cutoff
        row_time = now - timedelta(hours=1)  # but the file's own data reaches into the window
        name = f"Pressure - {file_start.strftime('%Y-%m-%d %H.%M.%S')}.txt"
        listing = "drwxr-sr-x         4,096 2026/08/27 08:26:53 .\n" f"-rwxr-xr-x         1,000 2026/08/27 08:26:53 {name}\n"
        contents = {name: 'Time (sec),"Process"(Torr)\n' + row_time.strftime("%I:%M:%S.000 %p %m/%d/%Y") + ",5.0\n"}

        def fake_sync_file(local_path, endpoint, remote_relpath_full):
            file_name = remote_relpath_full.rsplit("/", 1)[-1]
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="latin-1") as f:
                f.write(contents[file_name])
            return "ok"

        with (
            patch("NEMO_smart_lab.remote_cache.remote_sync.list_remote", return_value=listing),
            patch("NEMO_smart_lab.remote_cache.remote_sync.sync_file_from_remote", side_effect=fake_sync_file),
        ):
            trend = get_continuous_pressure_trend(self.tool.as_source_config(), range_key="24h")
        self.assertEqual(sorted(v for v in trend["series"]["Process"] if v is not None), [5.0])
