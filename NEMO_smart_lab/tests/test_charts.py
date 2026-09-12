import unittest

from NEMO_smart_lab.charts import LINE_CHART_MAX_POINTS, _line_series_json, _lttb_downsample


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


class LineSeriesJsonTests(unittest.TestCase):
    def test_small_series_is_not_downsampled(self):
        series = {"Heater 1": (list(range(50)), [float(i) for i in range(50)])}
        result = _line_series_json(series)
        self.assertEqual(len(result), 1)
        self.assertNotIn("original_point_count", result[0])
        self.assertEqual(len(result[0]["x"]), 50)

    def test_large_series_is_downsampled_and_flagged(self):
        n = LINE_CHART_MAX_POINTS + 5000
        series = {"Heater 1": (list(range(n)), [float(i % 11) for i in range(n)])}
        result = _line_series_json(series)
        self.assertEqual(result[0]["original_point_count"], n)
        self.assertEqual(len(result[0]["x"]), LINE_CHART_MAX_POINTS)

    def test_none_values_are_dropped_before_downsampling(self):
        n = 20
        y_values = [float(i) if i % 2 == 0 else None for i in range(n)]
        series = {"Heater 1": (list(range(n)), y_values)}
        result = _line_series_json(series)
        self.assertEqual(len(result[0]["y"]), 10)
        self.assertNotIn(None, result[0]["y"])


if __name__ == "__main__":
    unittest.main()
