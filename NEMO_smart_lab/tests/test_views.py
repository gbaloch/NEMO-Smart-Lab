"""
Tests for NEMO_smart_lab.views' access control (smart_lab_access_required) - staff/superusers
always get in, anyone else needs the "smart_lab.access_smart_lab" permission (grantable to a
specific user or a whole group from the ordinary Django admin, no separate settings toggle).
"""

import json
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from NEMO.models import User
from NEMO_smart_lab.models import RemoteSyncEndpoint, SmartLabTool


def _make_user(username, is_staff=False, is_superuser=False):
    user = User.objects.create_user(username, "Test", "User", f"{username}@example.org")
    user.is_staff = is_staff
    user.is_superuser = is_superuser
    user.save()
    return user


class SmartLabAccessTests(TestCase):
    def setUp(self):
        self.url = reverse("smart_lab_dashboard")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_plain_authenticated_user_is_forbidden(self):
        user = _make_user("alice")
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_staff_user_is_allowed(self):
        user = _make_user("bob", is_staff=True)
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_superuser_is_allowed(self):
        user = _make_user("carol", is_superuser=True)
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_non_staff_user_granted_the_permission_is_allowed(self):
        user = _make_user("dave")
        permission = Permission.objects.get(codename="access_smart_lab", content_type__app_label="smart_lab")
        user.user_permissions.add(permission)
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


class ToolSyncMapTests(TestCase):
    """/smart_lab/api/sync-map.json - the machine-to-machine endpoint the staging scripts fetch
    the tool-name -> Oak-directory mapping from (see staging_api_key_required), instead of each
    script hardcoding its own copy of that list."""

    def setUp(self):
        cache.clear()
        self.url = reverse("smart_lab_tool_sync_map")
        self.endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )

    def test_no_key_configured_on_this_instance_refuses_everything(self):
        # No SMART_LAB_STAGING_API_KEY setting at all (the default in tests) - fails closed rather
        # than exposing the endpoint unauthenticated.
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Token anything")
        self.assertEqual(response.status_code, 403)

    @override_settings(SMART_LAB_STAGING_API_KEY="s3cret")
    def test_missing_authorization_header_is_forbidden(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    @override_settings(SMART_LAB_STAGING_API_KEY="s3cret")
    def test_wrong_key_is_forbidden(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Token wrong")
        self.assertEqual(response.status_code, 403)

    @override_settings(SMART_LAB_STAGING_API_KEY="s3cret")
    def test_correct_key_returns_the_mapping(self):
        SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root="/data/fiji1", sync_endpoint=self.endpoint, remote_subdir="Fiji1"
        )
        SmartLabTool.objects.create(
            name="mvd", kind="mvd", local_root="/data/mvd", sync_endpoint=self.endpoint, remote_subdir=""
        )
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Token s3cret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"fiji1": "Fiji1", "mvd": "mvd"})

    @override_settings(SMART_LAB_STAGING_API_KEY="s3cret")
    def test_tools_without_a_sync_endpoint_are_excluded(self):
        SmartLabTool.objects.create(name="local-only", kind="heater_log", local_root="/data/x")
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Token s3cret")
        self.assertEqual(json.loads(response.content), {})

    @override_settings(SMART_LAB_STAGING_API_KEY="s3cret")
    def test_disabled_tools_are_excluded(self):
        SmartLabTool.objects.create(
            name="fiji1",
            kind="heater_log",
            local_root="/data/fiji1",
            sync_endpoint=self.endpoint,
            remote_subdir="Fiji1",
            enabled=False,
        )
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Token s3cret")
        self.assertEqual(json.loads(response.content), {})


class RecipeTogglePinTests(TestCase):
    """tool_recipe_toggle_pin - writes only to this plugin's own local SmartLabTool row (never to
    Oak or prod NEMO), toggled from the small pin icon next to each folder heading."""

    def setUp(self):
        cache.clear()  # get_tool_sources() is cached - see config.py's TOOL_SOURCES_TTL.
        endpoint = RemoteSyncEndpoint.objects.create(
            name="Oak", host="dtn.oak.stanford.edu", username="gbaloch", ssh_key_path="/k", base_path="/base"
        )
        self.tool = SmartLabTool.objects.create(
            name="fiji1", kind="heater_log", local_root="/tmp/does-not-matter",
            sync_endpoint=endpoint, remote_subdir="Fiji1", recipe_subdir="Recipes",
        )
        self.user = _make_user("bob", is_staff=True)
        self.client.force_login(self.user)
        self.url = reverse("smart_lab_tool_recipe_toggle_pin", args=[self.tool.pk])

    def test_pins_an_unpinned_category(self):
        response = self.client.post(self.url, {"category": "Didem"})
        self.assertRedirects(
            response, reverse("smart_lab_tool_recipes", args=[self.tool.pk]), fetch_redirect_response=False
        )
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.pinned_recipe_categories, ["Didem"])

    def test_unpins_an_already_pinned_category(self):
        self.tool.pinned_recipe_categories = ["Didem"]
        self.tool.save(update_fields=["pinned_recipe_categories"])
        self.client.post(self.url, {"category": "Didem"})
        self.tool.refresh_from_db()
        self.assertEqual(self.tool.pinned_recipe_categories, [])

    def test_takes_effect_immediately_not_after_the_tool_sources_cache_ttl(self):
        # get_tool_sources() is cached (config.py) - a plain toggle-then-redirect must not show a
        # stale, unpinned config for the rest of that cache window.
        from NEMO_smart_lab.config import get_tool_sources

        get_tool_sources()  # warm the cache with the pre-toggle (unpinned) config
        self.client.post(self.url, {"category": "Didem"})
        self.assertEqual(get_tool_sources()["fiji1"]["pinned_recipe_categories"], ["Didem"])

    def test_get_is_not_allowed(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_requires_smart_lab_access(self):
        self.client.logout()
        plain_user = _make_user("carol")
        self.client.force_login(plain_user)
        response = self.client.post(self.url, {"category": "Didem"})
        self.assertEqual(response.status_code, 403)


class ToolHistoryFilterTests(TestCase):
    """tool_history's ?recipe=/?user= query params - wiring only (readers.get_tool_history and
    reservations.find_user_run_windows have their own thorough unit tests for the actual
    filtering logic - see test_readers.HistoryFilterTests/test_reservations.FindUserRunWindowsTests)."""

    FULL_HEADER = ["Heater Time"] + [f"Heater {n}" for n in range(6, 18)] + [
        "Program Time", "MFC 1", "MFC Time", "Cycles Remaining", "Recipe", "Loop"
    ]

    def _write_run(self, filename):
        import os

        row = ["0.0"] + ["1.0"] * 12 + ["0.0", "0.0", "0.0", "0", "irrelevant", ""]
        path = os.path.join(self.tmp.name, "Logfile", "Heater Data", filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\t" + "\t".join(self.FULL_HEADER) + "\n")
            f.write("\t" + "\t".join(row) + "\n")

    def setUp(self):
        import os
        import tempfile

        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "Logfile", "Heater Data"))
        self.tool = SmartLabTool.objects.create(name="fiji1", kind="heater_log", local_root=self.tmp.name)
        self.user = _make_user("dave", is_staff=True)
        self.client.force_login(self.user)
        self.url = reverse("smart_lab_tool_history", args=[self.tool.pk])

    def test_recipe_query_param_filters_the_history_list(self):
        self._write_run("2026_01_02-00-00-00_Standby 200C.txt")
        self._write_run("2026_01_01-00-00-00_Thermal Al2O3.txt")
        response = self.client.get(self.url, {"recipe": "Standby 200C"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["recipe_filter"], ["Standby 200C"])
        self.assertTrue(response.context["is_filtered"])

    def test_multiple_recipe_tags_are_read_as_a_list_and_or_together(self):
        self._write_run("2026_01_03-00-00-00_Standby 200C.txt")
        self._write_run("2026_01_02-00-00-00_Thermal Al2O3.txt")
        self._write_run("2026_01_01-00-00-00_Valve Clean.txt")
        response = self.client.get(self.url, {"recipe": ["Standby 200C", "Thermal Al2O3"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 2)
        self.assertEqual(response.context["recipe_filter"], ["Standby 200C", "Thermal Al2O3"])

    def test_no_filter_params_shows_everything(self):
        self._write_run("2026_01_02-00-00-00_Standby 200C.txt")
        self._write_run("2026_01_01-00-00-00_Thermal Al2O3.txt")
        response = self.client.get(self.url)
        self.assertEqual(response.context["total"], 2)
        self.assertFalse(response.context["is_filtered"])
        self.assertEqual(response.context["recipe_filter"], [])

    def test_user_query_param_with_no_matching_usage_returns_zero_runs(self):
        self._write_run("2026_01_01-00-00-00_Standby 200C.txt")
        response = self.client.get(self.url, {"user": "nobody-has-used-this-tool"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 0)
        self.assertEqual(response.context["user_filter"], ["nobody-has-used-this-tool"])

    def test_recipe_and_user_choices_are_exposed_for_the_tag_inputs_suggestions(self):
        self._write_run("2026_01_01-00-00-00_Standby 200C.txt")
        response = self.client.get(self.url)
        self.assertIn("recipe_choices", response.context)
        self.assertIn("user_choices", response.context)

    def test_filter_query_string_carries_every_tag_forward(self):
        self._write_run("2026_01_01-00-00-00_Standby 200C.txt")
        response = self.client.get(self.url, {"recipe": ["A", "B"], "user": ["carol"]})
        qs = response.context["filter_query_string"]
        self.assertIn("recipe=A", qs)
        self.assertIn("recipe=B", qs)
        self.assertIn("user=carol", qs)
