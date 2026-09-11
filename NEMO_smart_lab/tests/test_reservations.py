"""
Tests for NEMO_smart_lab.reservations - the local lookup runs against real (if minimal)
NEMO.models rows in the test database; the remote lookup is tested purely against a mocked
`requests.get` (no test here is allowed to make a real network call).
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from NEMO.models import Account, Project, Reservation, Tool, UsageEvent, User
from NEMO_smart_lab.models import NemoApiSource
from NEMO_smart_lab.reservations import get_local_usage, get_remote_usage, get_run_usage, run_time_window


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

    def test_usage_event_takes_priority_over_overlapping_reservation(self):
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
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "usage_event")


class GetRemoteUsageTests(TestCase):
    """Every requests call is mocked - these tests must never touch the network."""

    def setUp(self):
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
            "user_detail": {"first_name": "Bob", "last_name": "Builder"},
        }
        non_overlapping = {
            "start": (self.now - timedelta(days=2)).isoformat(),
            "end": (self.now - timedelta(days=2) + timedelta(hours=1)).isoformat(),
            "user_detail": {"first_name": "Nope", "last_name": "Skip"},
        }
        with patch("NEMO_smart_lab.reservations.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"results": [overlapping, non_overlapping]}
            mock_get.return_value.raise_for_status.return_value = None
            results = get_remote_usage(
                self.api_source, 9, self.now - timedelta(minutes=5), self.now + timedelta(minutes=5)
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["user"], "Bob Builder")
        self.assertEqual(results[0]["source"], "usage_event")

    def test_request_failure_is_swallowed_and_returns_empty(self):
        import requests

        with patch("NEMO_smart_lab.reservations.requests.get", side_effect=requests.ConnectionError("boom")):
            self.assertEqual(get_remote_usage(self.api_source, 9, self.now, self.now), [])


class GetRunUsageTests(TestCase):
    def setUp(self):
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
                        "user_detail": {"first_name": "Bob", "last_name": "Builder"},
                    }
                ]
            }
            results = get_run_usage("fiji1", 9, {"last_update": self.now, "run_duration_s": 600}, api_source)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["reference_from"], "Fake remote")
