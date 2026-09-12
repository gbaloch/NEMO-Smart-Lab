"""
Tests for NEMO_smart_lab.reservations - the local lookup runs against real (if minimal)
NEMO.models rows in the test database; the remote lookup is tested purely against a mocked
`requests.get` (no test here is allowed to make a real network call).
"""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from NEMO.models import Account, Project, Reservation, Tool, UsageEvent, User
from NEMO_smart_lab.models import NemoApiSource
from NEMO_smart_lab.reservations import (
    annotate_run_usage,
    get_local_usage,
    get_remote_usage,
    get_run_usage,
    run_time_window,
)


def _make_user(username):
    return User.objects.create_user(username, "Test", "User", f"{username}@example.org")


def _make_project():
    account = Account.objects.create(name="Test account")
    return Project.objects.create(name="Test project", application_identifier="TEST", account=account)


class RunTimeWindowTests(TestCase):
    def test_none_when_no_last_update(self):
        self.assertEqual(run_time_window({}), (None, None))

    def test_naive_last_update_is_made_aware_and_padded(self):
        # readers.py builds "last_update" with datetime.fromtimestamp() - always naive.
        naive_end = timezone.datetime(2026, 1, 1, 12, 0, 0)
        self.assertTrue(timezone.is_naive(naive_end))
        start, end = run_time_window({"last_update": naive_end, "run_duration_s": 600})
        self.assertTrue(timezone.is_aware(start))
        self.assertTrue(timezone.is_aware(end))
        # 600s (10min) run duration + a 5 minute pad on *each* side of that = 20 total minutes.
        self.assertEqual((end - start), timedelta(minutes=20))


class GetLocalUsageTests(TestCase):
    def setUp(self):
        self.tool = Tool.objects.create(name="fiji1", visible=True)
        self.user = _make_user("alice")
        self.project = _make_project()
        self.now = timezone.now()

    def test_no_tool_returns_empty(self):
        self.assertEqual(get_local_usage("does-not-exist", self.now, self.now), [])

    def test_overlapping_usage_event_is_found(self):
        UsageEvent.objects.create(
            tool=self.tool,
            user=self.user,
            operator=self.user,
            project=self.project,
            start=self.now - timedelta(hours=1),
            end=self.now + timedelta(hours=1),
        )
        results = get_local_usage("fiji1", self.now - timedelta(minutes=5), self.now + timedelta(minutes=5))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "usage_event")
        self.assertEqual(results[0]["user"], "Test User")

    def test_non_overlapping_usage_event_is_not_found(self):
        UsageEvent.objects.create(
            tool=self.tool,
            user=self.user,
            operator=self.user,
            project=self.project,
            start=self.now - timedelta(days=1),
            end=self.now - timedelta(days=1) + timedelta(hours=1),
        )
        self.assertEqual(get_local_usage("fiji1", self.now, self.now + timedelta(minutes=1)), [])

    def test_still_running_usage_event_with_null_end_overlaps_anything_after_start(self):
        UsageEvent.objects.create(
            tool=self.tool, user=self.user, operator=self.user, project=self.project, start=self.now, end=None
        )
        results = get_local_usage("fiji1", self.now + timedelta(hours=2), self.now + timedelta(hours=3))
        self.assertEqual(len(results), 1)

    def test_falls_back_to_reservation_only_when_no_usage_event(self):
        Reservation.objects.create(
            tool=self.tool,
            user=self.user,
            creator=self.user,
            project=self.project,
            short_notice=False,
            start=self.now - timedelta(hours=1),
            end=self.now + timedelta(hours=1),
            cancelled=False,
        )
        results = get_local_usage("fiji1", self.now - timedelta(minutes=5), self.now + timedelta(minutes=5))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "reservation")

    def test_cancelled_reservation_is_ignored(self):
        Reservation.objects.create(
            tool=self.tool,
            user=self.user,
            creator=self.user,
            project=self.project,
            short_notice=False,
            start=self.now - timedelta(hours=1),
            end=self.now + timedelta(hours=1),
            cancelled=True,
        )
        self.assertEqual(get_local_usage("fiji1", self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)), [])

    def test_shortened_reservation_is_excluded_in_favor_of_its_descendant(self):
        # Regression, confirmed against real prod data: ending tool usage early doesn't cancel the
        # original reservation (cancelled stays False) - NEMO instead marks it shortened=True and
        # creates a new "descendant" reservation for the actual (shorter) time. Without excluding
        # shortened=True, both the original calendar booking and its descendant show up side by
        # side as if they were two unrelated reservations for the same user/day.
        original = Reservation.objects.create(
            tool=self.tool, user=self.user, creator=self.user, project=self.project, short_notice=False,
            start=self.now - timedelta(hours=1), end=self.now + timedelta(hours=1),
            cancelled=False, shortened=True,
        )
        Reservation.objects.create(
            tool=self.tool, user=self.user, creator=self.user, project=self.project, short_notice=False,
            start=self.now - timedelta(hours=1), end=self.now - timedelta(minutes=10),
            cancelled=False, shortened=False, descendant=None,
        )
        results = get_local_usage("fiji1", self.now - timedelta(minutes=15), self.now)
        self.assertEqual(len(results), 1)
        self.assertNotEqual(results[0]["end"], original.end)

    def test_missed_reservation_is_excluded(self):
        # Regression, confirmed against real prod data: a reservation nobody showed up for
        # (missed=True) is a real, independent Reservation row - not an ancestor/descendant pair
        # like the shortened case above - but showing it alongside a later, actually-used
        # reservation for the same window is still misleading, since it was never honored.
        Reservation.objects.create(
            tool=self.tool, user=self.user, creator=self.user, project=self.project, short_notice=False,
            start=self.now - timedelta(hours=3), end=self.now,
            cancelled=False, shortened=False, missed=True,
        )
        Reservation.objects.create(
            tool=self.tool, user=self.user, creator=self.user, project=self.project, short_notice=False,
            start=self.now - timedelta(minutes=30), end=self.now,
            cancelled=False, shortened=False, missed=False,
        )
        results = get_local_usage("fiji1", self.now - timedelta(minutes=15), self.now)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start"], self.now - timedelta(minutes=30))

    def test_both_usage_event_and_reservation_are_returned_when_both_overlap(self):
        # A UsageEvent (actual logged usage) and a Reservation (calendar intent) are different
        # signals shown separately - one must not suppress the other.
        Reservation.objects.create(
            tool=self.tool,
            user=self.user,
            creator=self.user,
            project=self.project,
            short_notice=False,
            start=self.now - timedelta(hours=1),
            end=self.now + timedelta(hours=1),
            cancelled=False,
        )
        UsageEvent.objects.create(
            tool=self.tool,
            user=self.user,
            operator=self.user,
            project=self.project,
            start=self.now - timedelta(hours=1),
            end=self.now + timedelta(hours=1),
        )
        results = get_local_usage("fiji1", self.now - timedelta(minutes=5), self.now + timedelta(minutes=5))
        self.assertEqual(len(results), 2)
        sources = {r["source"] for r in results}
        self.assertEqual(sources, {"usage_event", "reservation"})
        # usage_event listed first (the stronger signal, shown at the top).
        self.assertEqual(results[0]["source"], "usage_event")


class GetRemoteUsageTests(TestCase):
    """Every requests call is mocked - these tests must never touch the network."""

    def setUp(self):
        cache.clear()
        self.api_source = NemoApiSource.objects.create(
            name="Fake remote", api_root="https://example.invalid/api", token="fake-token"
        )
        self.now = timezone.now()

    def test_no_source_or_no_real_id_returns_empty_without_any_request(self):
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            self.assertEqual(get_remote_usage(None, 9, self.now, self.now), [])
            self.assertEqual(get_remote_usage(self.api_source, None, self.now, self.now), [])
        mock_get.assert_not_called()

    def test_only_get_requests_are_ever_issued(self):
        with (
            patch("NEMO_smart_lab.reservations.requests.get") as mock_get,
            patch("requests.post") as mock_post,
            patch("requests.put") as mock_put,
            patch("requests.patch") as mock_patch,
            patch("requests.delete") as mock_delete,
        ):
            mock_get.return_value.json.return_value = []
            mock_get.return_value.raise_for_status.return_value = None
            get_remote_usage(self.api_source, 9, self.now - timedelta(hours=1), self.now)
        mock_get.assert_called()
        mock_post.assert_not_called()
        mock_put.assert_not_called()
        mock_patch.assert_not_called()
        mock_delete.assert_not_called()

    def test_parses_paginated_response_and_filters_by_overlap(self):
        overlapping = {
            "start": (self.now - timedelta(hours=1)).isoformat(),
            "end": (self.now + timedelta(hours=1)).isoformat(),
            "user": {"first_name": "Bob", "last_name": "Builder"},
        }
        non_overlapping = {
            "start": (self.now - timedelta(days=2)).isoformat(),
            "end": (self.now - timedelta(days=2) + timedelta(hours=1)).isoformat(),
            "user": {"first_name": "Nope", "last_name": "Skip"},
        }
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"results": [overlapping, non_overlapping]}
            mock_get.return_value.raise_for_status.return_value = None
            results = get_remote_usage(
                self.api_source, 9, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)
            )
        # Same mocked response is returned for both the usage_events and reservations endpoint
        # calls, so the one overlapping row is combined from each - the non-overlapping row is
        # filtered out of both.
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["user"] == "Bob Builder" for r in results))
        self.assertEqual({r["source"] for r in results}, {"usage_event", "reservation"})

    def test_server_side_start__gte_is_padded_back_not_the_windows_own_start(self):
        # Regression: a reservation covering several runs back to back has its *own* start well
        # before any one specific run's window - a flat start__gte=<window start> would exclude it
        # even though it genuinely overlaps (confirmed live on fiji2: a run at 12:29-13:09 sits
        # entirely inside a 10:00-14:00 reservation; get_run_usage's single-run lookup - a narrow
        # window - found nothing, while annotate_run_usage's whole-page lookup - whose wider range
        # happened to already reach back past 10:00 - found it, for the exact same run).
        window_start = self.now
        window_end = self.now + timedelta(minutes=40)
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.json.return_value = []
            mock_get.return_value.raise_for_status.return_value = None
            get_remote_usage(self.api_source, 9, window_start, window_end)
        sent_start_gte = mock_get.call_args.kwargs["params"]["start__gte"]
        self.assertLess(sent_start_gte, window_start.isoformat())

    def test_reservation_starting_well_before_the_window_is_still_found(self):
        long_reservation = {
            "start": (self.now - timedelta(hours=2, minutes=29)).isoformat(),
            "end": (self.now + timedelta(hours=1)).isoformat(),
            "user": {"first_name": "Adrian", "last_name": "Magallon", "username": "adrianmn"},
        }
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.json.return_value = [long_reservation]
            mock_get.return_value.raise_for_status.return_value = None
            results = get_remote_usage(self.api_source, 9, self.now, self.now + timedelta(minutes=40))
        self.assertEqual(len(results), 2)  # combined from both the usage_events and reservations endpoints
        self.assertTrue(all(r["username"] == "adrianmn" for r in results))

    def test_combines_distinct_usage_event_and_reservation_endpoints(self):
        usage_event_row = {
            "start": (self.now - timedelta(hours=1)).isoformat(),
            "end": (self.now + timedelta(hours=1)).isoformat(),
            "user": {"first_name": "Bob", "last_name": "Builder", "username": "bbuilder"},
        }
        reservation_row = {
            "start": (self.now - timedelta(hours=2)).isoformat(),
            "end": (self.now + timedelta(hours=2)).isoformat(),
            "user": {"first_name": "Alice", "last_name": "Smith", "username": "asmith"},
        }
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.side_effect = [[usage_event_row], [reservation_row]]
            results = get_remote_usage(
                self.api_source, 9, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)
            )
        self.assertEqual(len(results), 2)
        by_source = {r["source"]: r for r in results}
        self.assertEqual(by_source["usage_event"]["username"], "bbuilder")
        self.assertEqual(by_source["reservation"]["username"], "asmith")

    def test_shortened_reservation_row_is_excluded_in_favor_of_its_descendant(self):
        # Same regression as GetLocalUsageTests' version, against the remote API path - confirmed
        # against real prod data that a shortened original stays cancelled=False.
        original = {
            "start": (self.now - timedelta(hours=1)).isoformat(),
            "end": (self.now + timedelta(hours=1)).isoformat(),
            "cancelled": False,
            "shortened": True,
            "user": {"first_name": "Bob", "last_name": "Builder", "username": "bbuilder"},
        }
        descendant = {
            "start": (self.now - timedelta(hours=1)).isoformat(),
            "end": (self.now - timedelta(minutes=10)).isoformat(),
            "cancelled": False,
            "shortened": False,
            "user": {"first_name": "Bob", "last_name": "Builder", "username": "bbuilder"},
        }
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = [original, descendant]
            results = get_remote_usage(self.api_source, 9, self.now - timedelta(hours=2), self.now + timedelta(hours=2))
        reservations = [r for r in results if r["source"] == "reservation"]
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["end"].isoformat(), descendant["end"])

    def test_missed_reservation_row_is_excluded(self):
        missed = {
            "start": (self.now - timedelta(hours=3)).isoformat(),
            "end": self.now.isoformat(),
            "cancelled": False,
            "shortened": False,
            "missed": True,
            "user": {"first_name": "Bob", "last_name": "Builder", "username": "bbuilder"},
        }
        actual = {
            "start": (self.now - timedelta(minutes=30)).isoformat(),
            "end": self.now.isoformat(),
            "cancelled": False,
            "shortened": False,
            "missed": False,
            "user": {"first_name": "Bob", "last_name": "Builder", "username": "bbuilder"},
        }
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = [missed, actual]
            results = get_remote_usage(self.api_source, 9, self.now - timedelta(hours=4), self.now + timedelta(hours=1))
        reservations = [r for r in results if r["source"] == "reservation"]
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["start"].isoformat(), actual["start"])

    def test_request_failure_is_swallowed_and_returns_empty(self):
        import requests

        with patch("NEMO_smart_lab.reservations.requests.get", side_effect=requests.ConnectionError("boom")):
            self.assertEqual(get_remote_usage(self.api_source, 9, self.now, self.now), [])

    def test_extracts_username_alongside_display_name(self):
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = [
                {
                    "start": (self.now - timedelta(minutes=10)).isoformat(),
                    "end": self.now.isoformat(),
                    "user": {"first_name": "Bob", "last_name": "Builder", "username": "bbuilder"},
                }
            ]
            results = get_remote_usage(self.api_source, 9, self.now - timedelta(minutes=15), self.now)
        self.assertEqual(results[0]["user"], "Bob Builder")
        self.assertEqual(results[0]["username"], "bbuilder")

    def test_second_call_within_ttl_does_not_re_fetch(self):
        start, end = self.now - timedelta(minutes=15), self.now
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = []
            get_remote_usage(self.api_source, 9, start, end)
            get_remote_usage(self.api_source, 9, start, end)
        self.assertEqual(mock_get.call_count, 2)  # usage_events + reservations, both empty -> cached together

        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get_again:
            get_remote_usage(self.api_source, 9, start, end)
        mock_get_again.assert_not_called()  # third call, same window - fully served from cache

    def test_empty_result_is_also_cached(self):
        start, end = self.now - timedelta(minutes=15), self.now
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = []
            first = get_remote_usage(self.api_source, 9, start, end)
        self.assertEqual(first, [])
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get_again:
            second = get_remote_usage(self.api_source, 9, start, end)
        mock_get_again.assert_not_called()
        self.assertEqual(second, [])


class GetRunUsageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tool = Tool.objects.create(name="fiji1", visible=True)
        self.user = _make_user("alice")
        self.project = _make_project()
        self.now = timezone.now()

    def test_empty_when_summary_has_no_last_update(self):
        self.assertEqual(get_run_usage("fiji1", 9, {}), [])

    def test_local_result_wins_and_remote_is_never_called(self):
        UsageEvent.objects.create(
            tool=self.tool,
            user=self.user,
            operator=self.user,
            project=self.project,
            start=self.now - timedelta(minutes=10),
            end=self.now,
        )
        api_source = NemoApiSource.objects.create(name="Fake", api_root="https://example.invalid/api", token="x")
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            results = get_run_usage("fiji1", 9, {"last_update": self.now, "run_duration_s": 600}, api_source)
        mock_get.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertNotIn("reference_from", results[0])

    def test_falls_back_to_remote_and_tags_reference_source_when_local_is_empty(self):
        api_source = NemoApiSource.objects.create(name="Fake remote", api_root="https://example.invalid/api", token="x")
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.return_value = None
            mock_get.return_value.json.return_value = {
                "results": [
                    {
                        "start": (self.now - timedelta(minutes=10)).isoformat(),
                        "end": self.now.isoformat(),
                        "user": {"first_name": "Bob", "last_name": "Builder"},
                    }
                ]
            }
            results = get_run_usage("fiji1", 9, {"last_update": self.now, "run_duration_s": 600}, api_source)
        # Same mocked response used for both the usage_events and reservations endpoint calls, so
        # the one overlapping row is combined from each (see get_remote_usage - it no longer stops
        # at the first non-empty endpoint).
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["reference_from"] == "Fake remote" for r in results))


class AnnotateRunUsageTests(TestCase):
    """tool_history.html's rowspan'd "User" column relies on annotate_run_usage() grouping
    consecutive runs that share the same reservation/usage period into one cell."""

    def setUp(self):
        cache.clear()
        self.tool = Tool.objects.create(name="fiji1", visible=True)
        self.user = _make_user("alice")
        self.project = _make_project()
        self.now = timezone.now()

    def _run(self, ended_minutes_ago, duration_s=60):
        return {"timestamp": self.now - timedelta(minutes=ended_minutes_ago), "duration_s": duration_s}

    def test_one_reservation_spans_several_consecutive_runs(self):
        # A single 40-minute reservation; three short runs happened back-to-back inside it.
        UsageEvent.objects.create(
            tool=self.tool,
            user=self.user,
            operator=self.user,
            project=self.project,
            start=self.now - timedelta(minutes=40),
            end=self.now,
        )
        runs = [self._run(5), self._run(20), self._run(35)]  # newest first, as tool_history sorts
        annotate_run_usage(runs, "fiji1", None, None)

        for run in runs:
            self.assertIsNotNone(run["usage_period"])
            self.assertEqual(run["usage_period"]["user"], "Test User")
        # Only the first row of the group renders a cell, spanning all 3.
        self.assertTrue(runs[0]["usage_show_cell"])
        self.assertEqual(runs[0]["usage_rowspan"], 3)
        self.assertFalse(runs[1]["usage_show_cell"])
        self.assertFalse(runs[2]["usage_show_cell"])

    def test_runs_outside_any_period_each_get_their_own_dash_cell(self):
        runs = [self._run(5), self._run(120)]  # nothing reserved either time
        annotate_run_usage(runs, "fiji1", None, None)
        for run in runs:
            self.assertIsNone(run["usage_period"])
            self.assertTrue(run["usage_show_cell"])
            self.assertEqual(run["usage_rowspan"], 1)

    def test_two_separate_reservations_produce_two_separate_groups(self):
        bob = _make_user("bob")
        UsageEvent.objects.create(
            tool=self.tool, user=self.user, operator=self.user, project=self.project,
            start=self.now - timedelta(minutes=20), end=self.now - timedelta(minutes=15),
        )
        UsageEvent.objects.create(
            tool=self.tool, user=bob, operator=bob, project=self.project,
            start=self.now - timedelta(minutes=80), end=self.now - timedelta(minutes=75),
        )
        runs = [self._run(16, duration_s=30), self._run(76, duration_s=30)]
        annotate_run_usage(runs, "fiji1", None, None)

        self.assertTrue(runs[0]["usage_show_cell"])
        self.assertEqual(runs[0]["usage_rowspan"], 1)
        self.assertTrue(runs[1]["usage_show_cell"])
        self.assertEqual(runs[1]["usage_rowspan"], 1)
        # _make_user() gives every test user the same display name ("Test User") regardless of
        # username, so compare usernames (which do differ) to confirm these are two distinct users.
        self.assertNotEqual(runs[0]["usage_period"]["username"], runs[1]["usage_period"]["username"])

    def test_runs_with_no_timestamp_are_skipped_without_error(self):
        runs = [{"timestamp": None, "duration_s": None}]
        annotate_run_usage(runs, "fiji1", None, None)
        self.assertIsNone(runs[0]["usage_period"])
        self.assertTrue(runs[0]["usage_show_cell"])

    def test_run_with_both_usage_event_and_reservation_shows_both(self):
        # A reservation (calendar intent) covering a wider window, and a usage event (actual
        # logged-in time) inside it - both should surface for the run, not just one.
        UsageEvent.objects.create(
            tool=self.tool, user=self.user, operator=self.user, project=self.project,
            start=self.now - timedelta(minutes=10), end=self.now,
        )
        Reservation.objects.create(
            tool=self.tool, user=self.user, creator=self.user, project=self.project, short_notice=False,
            start=self.now - timedelta(minutes=30), end=self.now + timedelta(minutes=30), cancelled=False,
        )
        runs = [self._run(5, duration_s=60)]
        annotate_run_usage(runs, "fiji1", None, None)

        self.assertEqual(len(runs[0]["usage_periods"]), 2)
        self.assertEqual({p["source"] for p in runs[0]["usage_periods"]}, {"usage_event", "reservation"})
        # The usage_event is still the "primary" period used for rowspan grouping identity.
        self.assertEqual(runs[0]["usage_period"]["source"], "usage_event")

    def test_reservation_overlapping_only_the_start_of_a_long_run_still_matches(self):
        # Regression: this run is 20 minutes long (ended 5 minutes ago, so it started 25 minutes
        # ago) but the reservation only covers its first two minutes, ending 21 minutes before the
        # run's own raw end - a real interval overlap (the reservation covers part of the run), but
        # one an earlier, buggy version of this loop missed entirely: it only point-tested a
        # period against the run's raw *end* timestamp (way outside any reasonable pad from a
        # reservation this far from the run's end), instead of properly overlap-testing against
        # the run's whole [start, end] window the way get_run_usage() (the single-run detail page)
        # already correctly does.
        Reservation.objects.create(
            tool=self.tool, user=self.user, creator=self.user, project=self.project, short_notice=False,
            start=self.now - timedelta(minutes=26), end=self.now - timedelta(minutes=24), cancelled=False,
        )
        runs = [self._run(5, duration_s=1200)]
        annotate_run_usage(runs, "fiji1", None, None)

        self.assertEqual(len(runs[0]["usage_periods"]), 1)
        self.assertEqual(runs[0]["usage_period"]["source"], "reservation")
