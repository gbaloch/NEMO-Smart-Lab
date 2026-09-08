import unittest

from NEMO_smart_lab.templatetags.smart_lab_filters import smart_duration


class SmartDurationTests(unittest.TestCase):
    def test_none_is_dash(self):
        self.assertEqual(smart_duration(None), "-")

    def test_negative_is_dash(self):
        self.assertEqual(smart_duration(-5), "-")

    def test_non_numeric_is_dash(self):
        self.assertEqual(smart_duration("not a number"), "-")

    def test_sub_minute_keeps_one_decimal(self):
        self.assertEqual(smart_duration(2.9), "2.9s")
        self.assertEqual(smart_duration(50.8), "50.8s")
        self.assertEqual(smart_duration(0), "0.0s")

    def test_minutes_drop_hours(self):
        self.assertEqual(smart_duration(137), "2m 17s")

    def test_hours_include_all_smaller_units(self):
        self.assertEqual(smart_duration(14753.4), "4h 5m 53s")

    def test_exact_hour_still_shows_zero_minutes(self):
        # Once we're into hours, minutes/seconds are shown even when zero - only units
        # *larger* than the largest nonzero one are dropped.
        self.assertEqual(smart_duration(3600), "1h 0m 0s")

    def test_days(self):
        self.assertEqual(smart_duration(90000), "1d 1h 0m 0s")


if __name__ == "__main__":
    unittest.main()
