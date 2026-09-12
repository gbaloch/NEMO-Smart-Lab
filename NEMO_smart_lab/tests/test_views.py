"""
Tests for NEMO_smart_lab.views' access control (smart_lab_access_required) - staff/superusers
always get in, anyone else needs the "smart_lab.access_smart_lab" permission (grantable to a
specific user or a whole group from the ordinary Django admin, no separate settings toggle).
"""

from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
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
        self.url = reverse("smart_lab_tool_recipe_toggle_pin", args=["fiji1"])

    def test_pins_an_unpinned_category(self):
        response = self.client.post(self.url, {"category": "Didem"})
        self.assertRedirects(
            response, reverse("smart_lab_tool_recipes", args=["fiji1"]), fetch_redirect_response=False
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
