"""
Tests for NEMO_smart_lab.status - local in-use detection runs against real (if minimal)
NEMO.models rows in the test database; the remote fallback is tested purely against a mocked
`requests.get` (no test here is allowed to make a real network call).
"""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from NEMO.models import Account, Project, Tool, UsageEvent, User
from NEMO_smart_lab.models import NemoApiSource, SmartLabTool
from NEMO_smart_lab.status import get_tool_status


def _make_user(username):
    return User.objects.create_user(username, "Test", "User", f"{username}@example.org")


def _make_project():
    account = Account.objects.create(name="Test account")
    return Project.objects.create(name="Test project", application_identifier="TEST", account=account)


def _make_slt(**overrides):
    defaults = {"name": "fiji1", "kind": "heater_log", "local_root": "/tmp/does-not-matter"}
    defaults.update(overrides)
    return SmartLabTool.objects.create(**defaults)


class InUseDetectionTests(TestCase):
    def setUp(self):
        # _operational defaults to False (NEMO's own default for a freshly created Tool) - these
        # tests are about in-use detection specifically, not the separate operational-flag
        # behavior (see NonOperationalStatusTests below), so mark it operational explicitly.
        self.tool = Tool.objects.create(name="fiji1", visible=True, _operational=True)
        self.user = _make_user("alice")
        self.project = _make_project()

    def test_open_usage_event_wins_regardless_of_recipe(self):
        UsageEvent.objects.create(
            tool=self.tool, user=self.user, operator=self.user, project=self.project, start=timezone.now(), end=None
        )
        slt = _make_slt(shutdown_recipe_keywords="standby run")
        status = get_tool_status("fiji1", {"recipe": "standby run"}, slt)
        self.assertEqual(status["code"], "in_use")
        self.assertEqual(status["label"], "In use")
        self.assertEqual(status["username"], "alice")

    def test_closed_usage_event_is_not_in_use(self):
        UsageEvent.objects.create(
            tool=self.tool,
            user=self.user,
            operator=self.user,
            project=self.project,
            start=timezone.now() - timedelta(hours=2),
            end=timezone.now() - timedelta(hours=1),
        )
        slt = _make_slt()
        status = get_tool_status("fiji1", {"recipe": "Some Recipe"}, slt)
        self.assertEqual(status["code"], "ready")

    def test_no_usage_event_at_all_is_not_in_use(self):
        slt = _make_slt()
        status = get_tool_status("fiji1", {"recipe": "Some Recipe"}, slt)
        self.assertEqual(status["code"], "ready")

    def test_existing_local_tool_never_falls_back_to_remote_for_in_use(self):
        # A real local Tool row exists (just not in use) - even with a usage_reference_source
        # configured, this must be treated as an authoritative "not in use", regardless of what a
        # remote lookup would say (see NonOperationalStatusTests.RemoteOperationalCheckAlsoRunsWithLocalToolTests
        # below for the *operational* flag, which - unlike this in-use check - IS also checked
        # remotely even when a local Tool row exists).
        api_source = NemoApiSource.objects.create(name="Fake remote", api_root="https://example.invalid/api", token="x")
        slt = _make_slt(usage_reference_source=api_source, real_id=42)
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            # If this were consulted for "in use", this response would incorrectly report someone
            # actively using it - the assertion below confirms local truth wins regardless.
            mock_get.return_value.json.return_value = [{"user": {"username": "bob", "first_name": "Bob", "last_name": "B"}}]
            mock_get.return_value.raise_for_status.return_value = None
            status = get_tool_status("fiji1", {"recipe": "Some Recipe"}, slt)
        self.assertEqual(status["code"], "ready")


class NonOperationalStatusTests(TestCase):
    """NEMO.models.Tool._operational (its "operational" property) - False means the tool has been
    marked non-operational on NEMO itself (a Task with force_shutdown=True, or a staff member
    directly), independent of any recipe having been run."""

    def test_non_operational_tool_shows_shut_down_with_no_matching_recipe(self):
        Tool.objects.create(name="fiji1", visible=True, _operational=False)
        slt = _make_slt()
        # "Al2O3 STANDARD" matches none of the default keyword lists - without the operational
        # check, this would fall through to "ready", which would be wrong: NEMO itself says the
        # tool is down.
        status = get_tool_status("fiji1", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "shutdown")

    def test_operational_tool_is_not_forced_to_shutdown(self):
        Tool.objects.create(name="fiji1", visible=True, _operational=True)
        slt = _make_slt()
        status = get_tool_status("fiji1", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "ready")

    def test_in_use_still_wins_over_non_operational(self):
        tool = Tool.objects.create(name="fiji1", visible=True, _operational=False)
        user = _make_user("alice")
        project = _make_project()
        UsageEvent.objects.create(tool=tool, user=user, operator=user, project=project, start=timezone.now(), end=None)
        slt = _make_slt()
        status = get_tool_status("fiji1", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "in_use")


class RemoteOperationalCheckAlsoRunsWithLocalToolTests(TestCase):
    """A tool's own "operational" flag - unlike the "in use" check (see
    InUseDetectionTests.test_existing_local_tool_never_falls_back_to_remote_for_in_use) - is
    checked on BOTH a local Tool row (if one exists) AND a configured usage_reference_source, since
    a local row's own operational flag isn't necessarily kept live-synced with a separate remote
    source of truth. Regression coverage for a real bug: a tool with a stale, locally-present
    operational=True Tool row stayed "Ready" even though it was genuinely shut down on its
    configured remote reference source."""

    def setUp(self):
        cache.clear()
        self.api_source = NemoApiSource.objects.create(name="Fake remote", api_root="https://example.invalid/api", token="x")

    def test_remote_non_operational_overrides_a_stale_locally_operational_tool(self):
        Tool.objects.create(name="savannah", visible=True, _operational=True)
        slt = _make_slt(name="savannah", usage_reference_source=self.api_source, real_id=8)
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"operational": False}
            mock_get.return_value.raise_for_status.return_value = None
            status = get_tool_status("savannah", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "shutdown")

    def test_falls_back_to_underscore_operational_field(self):
        # Confirmed live against the real production NEMO API this is actually pointed at: its
        # ToolSerializer response carries "_operational" (the raw stored field), not the computed
        # "operational" property - without this fallback, the check would silently never fire
        # against real data at all.
        Tool.objects.create(name="savannah", visible=True, _operational=True)
        slt = _make_slt(name="savannah", usage_reference_source=self.api_source, real_id=8)
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"id": 8, "name": "savannah", "_operational": False}
            mock_get.return_value.raise_for_status.return_value = None
            status = get_tool_status("savannah", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "shutdown")

    def test_remote_operational_true_does_not_override_local_non_operational(self):
        # Local already says non-operational - the remote check is skipped entirely (not just
        # ignored) in this case, since local truth is already conclusive.
        Tool.objects.create(name="fiji1", visible=True, _operational=False)
        slt = _make_slt(usage_reference_source=self.api_source, real_id=42)
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            status = get_tool_status("fiji1", {"recipe": "Al2O3 STANDARD"}, slt)
        mock_get.assert_not_called()
        self.assertEqual(status["code"], "shutdown")

    def test_remote_lookup_failure_does_not_force_shutdown(self):
        import requests

        Tool.objects.create(name="savannah", visible=True, _operational=True)
        slt = _make_slt(name="savannah", usage_reference_source=self.api_source, real_id=8)
        with patch("NEMO_smart_lab.status.requests.get", side_effect=requests.RequestException("boom")):
            status = get_tool_status("savannah", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "ready")

    def test_no_usage_reference_source_never_checks_remote(self):
        Tool.objects.create(name="fiji1", visible=True, _operational=True)
        slt = _make_slt()  # no usage_reference_source configured
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            status = get_tool_status("fiji1", {"recipe": "Al2O3 STANDARD"}, slt)
        mock_get.assert_not_called()
        self.assertEqual(status["code"], "ready")


class RecipeKeywordStatusTests(TestCase):
    """No local Tool row exists for any of these - purely exercising the recipe-keyword fallback,
    which only kicks in once "in use" is ruled out."""

    def test_shutdown_keyword_match(self):
        slt = _make_slt(shutdown_recipe_keywords="shutdown, shut down")
        status = get_tool_status("no-such-tool", {"recipe": "Nightly Shutdown"}, slt)
        self.assertEqual(status["code"], "shutdown")
        self.assertEqual(status["label"], "Shut down")

    def test_standby_keyword_match(self):
        slt = _make_slt(standby_recipe_keywords="standby")
        status = get_tool_status("no-such-tool", {"recipe": "STANDBY.txt"}, slt)
        self.assertEqual(status["code"], "ready_standby")

    def test_valve_clean_keyword_match(self):
        slt = _make_slt(valve_clean_recipe_keywords="valve clean")
        status = get_tool_status("no-such-tool", {"recipe": "Valve Clean Routine"}, slt)
        self.assertEqual(status["code"], "ready_valve_clean")

    def test_no_match_falls_back_to_ready(self):
        slt = _make_slt()
        status = get_tool_status("no-such-tool", {"recipe": "Al2O3 STANDARD"}, slt)
        self.assertEqual(status["code"], "ready")

    def test_shutdown_checked_before_standby(self):
        # A recipe name that happens to match both keyword lists resolves to shutdown, not standby.
        slt = _make_slt(shutdown_recipe_keywords="clean", standby_recipe_keywords="clean")
        status = get_tool_status("no-such-tool", {"recipe": "Deep Clean"}, slt)
        self.assertEqual(status["code"], "shutdown")

    def test_blank_keywords_disable_that_state(self):
        slt = _make_slt(standby_recipe_keywords="")
        status = get_tool_status("no-such-tool", {"recipe": "Standby"}, slt)
        self.assertEqual(status["code"], "ready")

    def test_no_slt_falls_back_to_ready(self):
        status = get_tool_status("no-such-tool", {"recipe": "Standby"}, None)
        self.assertEqual(status["code"], "ready")

    def test_error_summary_never_matches_a_recipe_keyword(self):
        slt = _make_slt(standby_recipe_keywords="standby")
        status = get_tool_status("no-such-tool", {"error": "boom", "recipe": "Standby"}, slt)
        self.assertEqual(status["code"], "ready")


class RemoteInUseFallbackTests(TestCase):
    """Only reached when there's no local Tool row for this name at all - see
    test_existing_local_tool_never_falls_back_to_remote above for the other branch."""

    def setUp(self):
        cache.clear()
        self.api_source = NemoApiSource.objects.create(name="Fake remote", api_root="https://example.invalid/api", token="x")

    def test_remote_open_usage_event_reports_in_use(self):
        slt = _make_slt(usage_reference_source=self.api_source, real_id=42)
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            mock_get.return_value.json.return_value = [{"user": {"username": "bob", "first_name": "Bob", "last_name": "Builder"}}]
            mock_get.return_value.raise_for_status.return_value = None
            status = get_tool_status("no-such-tool", {"recipe": "Anything"}, slt)
        self.assertEqual(status["code"], "in_use")
        self.assertEqual(status["username"], "bob")
        self.assertEqual(status["user"], "Bob Builder")

    def test_remote_no_open_usage_event_falls_through_to_recipe_matching(self):
        slt = _make_slt(usage_reference_source=self.api_source, real_id=42, shutdown_recipe_keywords="shutdown")
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            mock_get.return_value.json.return_value = []
            mock_get.return_value.raise_for_status.return_value = None
            status = get_tool_status("no-such-tool", {"recipe": "Shutdown"}, slt)
        self.assertEqual(status["code"], "shutdown")

    def test_remote_result_is_cached_within_ttl(self):
        slt = _make_slt(usage_reference_source=self.api_source, real_id=42)
        with patch("NEMO_smart_lab.status.requests.get") as mock_get:
            mock_get.return_value.json.return_value = []
            mock_get.return_value.raise_for_status.return_value = None
            get_tool_status("no-such-tool", {"recipe": "A"}, slt)
            get_tool_status("no-such-tool", {"recipe": "A"}, slt)
        # Two distinct remote checks per call (in-use via _remote_active_user, operational via
        # _remote_tool_operational - see get_tool_status), each independently cached - so the first
        # get_tool_status() call makes exactly one request per check (2 total), and the second call
        # adds none, both already cached.
        self.assertEqual(mock_get.call_count, 2)

    def test_remote_failure_falls_back_to_recipe_matching_without_raising(self):
        import requests

        slt = _make_slt(usage_reference_source=self.api_source, real_id=42, standby_recipe_keywords="standby")
        with patch("NEMO_smart_lab.status.requests.get", side_effect=requests.RequestException("boom")):
            status = get_tool_status("no-such-tool", {"recipe": "Standby"}, slt)
        self.assertEqual(status["code"], "ready_standby")
