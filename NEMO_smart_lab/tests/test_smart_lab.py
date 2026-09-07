from django.apps import apps
from django.test import TestCase


class SmartLabTest(TestCase):

    def test_plugin_is_installed(self):
        assert apps.is_installed("NEMO_smart_lab")
