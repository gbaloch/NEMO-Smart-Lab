"""Tests for NEMO_smart_lab.templatetags.smart_lab_filters."""

import unittest
from datetime import datetime

from django.utils import timezone

from NEMO_smart_lab.templatetags.smart_lab_filters import humanize_key, range_end, range_start, strip_txt


class StripTxtTests(unittest.TestCase):
    def test_single_extension_is_stripped(self):
        self.assertEqual(strip_txt("clear0.txt"), "clear0")

    def test_double_extension_is_fully_stripped(self):
        # A recipe literally named with a ".txt" suffix, then a *second* one appended by the
        # heater log export (see readers.py's module notes) - confirmed live on real Oak data.
        self.assertEqual(strip_txt("Plasma Al2O3 STANDARD.txt.txt"), "Plasma Al2O3 STANDARD")

    def test_no_extension_is_unchanged(self):
        self.assertEqual(strip_txt("20 - STANDBY 200C"), "20 - STANDBY 200C")

    def test_non_string_passes_through_unchanged(self):
        self.assertIsNone(strip_txt(None))
        self.assertEqual(strip_txt(42), 42)

    def test_case_insensitive(self):
        self.assertEqual(strip_txt("recipe.TXT"), "recipe")


class HumanizeKeyTests(unittest.TestCase):
    def test_replaces_underscores_and_capitalizes(self):
        self.assertEqual(humanize_key("cycles_remaining"), "Cycles remaining")


class RangeFiltersTests(unittest.TestCase):
    def _dt(self, *args):
        return timezone.make_aware(datetime(*args))

    def test_range_start_formats_date_and_time(self):
        self.assertEqual(range_start(self._dt(2026, 9, 10, 9, 57)), "09/10/2026 9:57 am")

    def test_range_start_none_passes_through(self):
        self.assertIsNone(range_start(None))

    def test_range_end_same_day_shows_time_only(self):
        start = self._dt(2026, 9, 10, 9, 57)
        end = self._dt(2026, 9, 10, 13, 47)
        self.assertEqual(range_end(end, start), "1:47 pm")

    def test_range_end_different_day_shows_full_date(self):
        start = self._dt(2026, 9, 10, 21, 30)
        end = self._dt(2026, 9, 11, 1, 0)
        self.assertEqual(range_end(end, start), "09/11/2026 1:00 am")

    def test_range_end_none_returns_none_not_a_string(self):
        # So the template's own |default:"in progress"/"now" still applies.
        self.assertIsNone(range_end(None, self._dt(2026, 9, 10, 9, 57)))

    def test_range_end_without_a_start_still_shows_full_date(self):
        self.assertEqual(range_end(self._dt(2026, 9, 10, 13, 47), None), "09/10/2026 1:47 pm")


if __name__ == "__main__":
    unittest.main()
