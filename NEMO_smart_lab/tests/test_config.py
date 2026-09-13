from django.core.cache import cache
from django.test import TestCase

from NEMO_smart_lab.config import get_tool_sources
from NEMO_smart_lab.models import SmartLabTool, SmartLabToolChannel


class GetToolSourcesTests(TestCase):
    def setUp(self):
        # get_tool_sources() is cached for TOOL_SOURCES_TTL seconds (see config.py) - each test
        # needs a clean slate rather than whatever the previous test's DB state cached.
        cache.clear()

    def test_empty_when_no_tools_configured(self):
        self.assertEqual(get_tool_sources(), {})

    def test_builds_expected_shape(self):
        tool = SmartLabTool.objects.create(
            name="fiji1",
            kind="heater_log",
            local_root=r"C:\data\fiji1",
            on_threshold_c=35.0,
        )
        self.assertEqual(
            get_tool_sources(),
            {"fiji1": {"id": tool.pk, "kind": "heater_log", "root": r"C:\data\fiji1", "on_threshold_c": 35.0}},
        )

    def test_disabled_tools_are_excluded(self):
        SmartLabTool.objects.create(name="fiji1", kind="heater_log", local_root=r"C:\data\fiji1", enabled=False)
        self.assertEqual(get_tool_sources(), {})

    def test_stream_fields_included_only_when_stream_root_set(self):
        SmartLabTool.objects.create(
            name="Ox-ALE",
            kind="cobra_job",
            local_root=r"C:\data\ox-ale",
            stream_root=r"C:\data\ox-ale\stream",
            stream_module="PMC1",
        )
        SmartLabTool.objects.create(name="Ox-gen", kind="cobra_job", local_root=r"C:\data\ox-gen")

        sources = get_tool_sources()
        self.assertEqual(sources["Ox-ALE"]["stream_root"], r"C:\data\ox-ale\stream")
        self.assertEqual(sources["Ox-ALE"]["stream_module"], "PMC1")
        self.assertNotIn("stream_root", sources["Ox-gen"])

    def test_channel_labels_included_only_when_present(self):
        tool = SmartLabTool.objects.create(name="fiji1", kind="heater_log", local_root=r"C:\data\fiji1")
        no_labels = SmartLabTool.objects.create(name="fiji2", kind="heater_log", local_root=r"C:\data\fiji2")
        SmartLabToolChannel.objects.create(
            tool=tool, channel_key="Heater 10", display_name="Source chuck", role="chuck"
        )

        sources = get_tool_sources()
        self.assertEqual(sources["fiji1"]["channel_labels"], {"Heater 10": ("Source chuck", "chuck", False, None)})
        self.assertNotIn("channel_labels", sources["fiji2"])

    def test_result_is_cached_until_cleared(self):
        self.assertEqual(get_tool_sources(), {})
        # A new tool created after the first call doesn't show up until the cache is cleared -
        # this is TOOL_SOURCES_TTL's whole point (see config.py's docstring).
        SmartLabTool.objects.create(name="fiji1", kind="heater_log", local_root=r"C:\data\fiji1")
        self.assertEqual(get_tool_sources(), {})
        cache.clear()
        self.assertIn("fiji1", get_tool_sources())
