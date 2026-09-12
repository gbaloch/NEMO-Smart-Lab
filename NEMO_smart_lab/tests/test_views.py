"""
Tests for NEMO_smart_lab.views' access control (smart_lab_access_required) - staff/superusers
always get in, anyone else needs the "smart_lab.access_smart_lab" permission (grantable to a
specific user or a whole group from the ordinary Django admin, no separate settings toggle).
"""

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from NEMO.models import User


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
