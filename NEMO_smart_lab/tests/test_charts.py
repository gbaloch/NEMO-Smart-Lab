import os
import tempfile
import unittest

from NEMO_smart_lab.charts import LINE_CHART_MAX_POINTS, get_chart_json, render_chart_png, _align_series, _line_series_json, _lttb_downsample
from NEMO_smart_lab.tests.test_readers import HeaterLogTests, _write_heater_log


class LttbDownsampleTests(unittest.TestCase):
    def test_noop_below_threshold(self):
        xs, ys = list(range(10)), [float(i) for i in range(10)]
        out_x, out_y = _lttb_downsample(xs, ys, 20)
        self.assertEqual(out_x, xs)
        self.assertEqual(out_y, ys)

    def test_reduces_to_requested_point_count(self):
        xs = list(range(10_000))
        ys = [float(i % 7) for i in xs]
        out_x, out_y = _lttb_downsample(xs, ys, 500)
        self.assertEqual(len(out_x), 500)
        self.assertEqual(len(out_y), 500)

    def test_preserves_first_and_last_point(self):
        xs = list(range(5000))
        ys = [float(i) for i in xs]
        out_x, out_y = _lttb_downsample(xs, ys, 100)
        self.assertEqual((out_x[0], out_y[0]), (xs[0], ys[0]))
        self.assertEqual((out_x[-1], out_y[-1]), (xs[-1], ys[-1]))

    def test_preserves_a_sharp_spike_a_naive_stride_would_miss(self):
        # A single-sample spike sitting between otherwise-flat readings, positioned so that
        # picking every Nth point (a naive stride) would step right over it.
        n = 3000
        xs = list(range(n))
        ys = [0.0] * n
        spike_index = 1234
        ys[spike_index] = 1000.0
        _out_x, out_y = _lttb_downsample(xs, ys, 300)
        self.assertIn(1000.0, out_y)


class AlignSeriesTests(unittest.TestCase):
    """uPlot requires one shared x-array with every series' y-array the same length, aligned to
    it (None marks a gap) - a real difference from the old per-series-independent Chart.js shape."""

    def test_series_sharing_one_x_array_stay_that_way(self):
        # heater_log/mvd's actual shape: every series already comes from the same time_s array.
        x = list(range(5))
        series = {"A": (x, [1.0, 2.0, 3.0, 4.0, 5.0]), "B": (x, [10.0, 20.0, 30.0, 40.0, 50.0])}
        shared_x, aligned = _align_series(series)
        self.assertEqual(shared_x, x)
        self.assertEqual(aligned["A"], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(aligned["B"], [10.0, 20.0, 30.0, 40.0, 50.0])

    def test_series_with_different_x_arrays_are_unioned_with_gaps(self):
        # e.g. waferlog/the live PTIQ stream - each channel has its own independent timestamps.
        series = {"A": ([0, 1, 2], [1.0, 2.0, 3.0]), "B": ([1, 3], [20.0, 40.0])}
        shared_x, aligned = _align_series(series)
        self.assertEqual(shared_x, [0, 1, 2, 3])
        self.assertEqual(aligned["A"], [1.0, 2.0, 3.0, None])
        self.assertEqual(aligned["B"], [None, 20.0, None, 40.0])

    def test_entirely_empty_series_is_dropped(self):
        series = {"A": ([0, 1], [1.0, 2.0]), "Empty": ([], [])}
        shared_x, aligned = _align_series(series)
        self.assertNotIn("Empty", aligned)
        self.assertEqual(shared_x, [0, 1])

    def test_all_null_series_is_dropped(self):
        series = {"A": ([0, 1], [1.0, 2.0]), "AllNull": ([0, 1], [None, None])}
        _shared_x, aligned = _align_series(series)
        self.assertNotIn("AllNull", aligned)

    def test_no_series_at_all(self):
        self.assertEqual(_align_series({}), ([], {}))


class LineSeriesJsonTests(unittest.TestCase):
    def test_small_series_is_not_downsampled(self):
        series = {"Heater 1": (list(range(50)), [float(i) for i in range(50)])}
        result = _line_series_json(series)
        self.assertFalse(result["downsampled"])
        self.assertEqual(len(result["x"]), 50)
        self.assertEqual(len(result["series"]), 1)
        self.assertEqual(len(result["series"][0]["y"]), 50)

    def test_large_series_is_downsampled_and_flagged(self):
        n = LINE_CHART_MAX_POINTS + 5000
        series = {"Heater 1": (list(range(n)), [float(i % 11) for i in range(n)])}
        result = _line_series_json(series)
        self.assertTrue(result["downsampled"])
        self.assertEqual(len(result["x"]), LINE_CHART_MAX_POINTS)
        self.assertEqual(len(result["series"][0]["y"]), LINE_CHART_MAX_POINTS)

    def test_every_series_in_a_group_stays_aligned_after_downsampling(self):
        # Two series with different shapes must still come out the exact same (decimated) length,
        # sharing the same x - required for uPlot, unlike the old per-series-independent shape.
        n = LINE_CHART_MAX_POINTS + 1000
        series = {
            "A": (list(range(n)), [float(i % 7) for i in range(n)]),
            "B": (list(range(n)), [float(i % 13) for i in range(n)]),
        }
        result = _line_series_json(series)
        lengths = {len(s["y"]) for s in result["series"]}
        self.assertEqual(lengths, {len(result["x"])})

    def test_none_values_are_kept_in_place_not_dropped(self):
        # Unlike the old Chart.js-era shape, nulls stay in place (as gaps) rather than being
        # stripped, since stripping per-series independently is what used to break x-alignment.
        n = 20
        y_values = [float(i) if i % 2 == 0 else None for i in range(n)]
        series = {"Heater 1": (list(range(n)), y_values)}
        result = _line_series_json(series)
        self.assertEqual(len(result["x"]), n)
        self.assertEqual(result["series"][0]["y"], y_values)

    def test_start_end_filters_to_that_range(self):
        x = list(range(100))
        series = {"Heater 1": (x, [float(v) for v in x])}
        result = _line_series_json(series, start=10, end=20)
        self.assertEqual(result["x"], list(range(10, 21)))
        self.assertEqual(result["series"][0]["y"], [float(v) for v in range(10, 21)])

    def test_narrow_start_end_avoids_downsampling_that_the_full_range_would_trigger(self):
        # The actual "zooming reveals real detail" fix: the same underlying series that gets
        # decimated over its full range should come back undecimated once scoped to a narrow
        # enough start/end window.
        n = LINE_CHART_MAX_POINTS + 5000
        x = list(range(n))
        series = {"Heater 1": (x, [float(i % 11) for i in range(n)])}
        full = _line_series_json(series)
        self.assertTrue(full["downsampled"])
        zoomed = _line_series_json(series, start=0, end=500)
        self.assertFalse(zoomed["downsampled"])
        self.assertEqual(len(zoomed["x"]), 501)

    def test_empty_series_returns_empty_shape(self):
        result = _line_series_json({})
        self.assertEqual(result, {"x": [], "series": [], "downsampled": False})


class ChartGroupKeyTests(unittest.TestCase):
    """get_chart_json()/render_chart_png()'s group_key param - omitted/unknown must behave
    exactly like before chart groups existed (the Temperature group), an explicit known key
    returns that group instead."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "Logfile", "Heater Data"))
        row = ["0.8"] + ["200.0"] * 12 + ["1210475.4", "19.9", "1.5", "0", "My Recipe", ""]
        _write_heater_log(os.path.join(self.root, "Logfile", "Heater Data", "run1.txt"), HeaterLogTests.FULL_HEADER, [row])
        self.cfg = {"kind": "heater_log", "root": self.root, "on_threshold_c": 35.0}

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_group_key_returns_temperature_group_unchanged(self):
        data = get_chart_json(self.cfg)
        self.assertEqual(data["y_label"], "Temperature (C)")

    def test_explicit_group_key_returns_that_group(self):
        data = get_chart_json(self.cfg, group_key="mfc_flow")
        self.assertEqual(data["y_label"], "Flow (sccm)")
        self.assertEqual(data["series"][0]["name"], "MFC 1")

    def test_unknown_group_key_falls_back_to_first_group(self):
        data = get_chart_json(self.cfg, group_key="not-a-real-group")
        self.assertEqual(data["y_label"], "Temperature (C)")

    def test_png_renders_for_a_named_group_without_error(self):
        png_bytes = render_chart_png(self.cfg, group_key="mfc_flow")
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
