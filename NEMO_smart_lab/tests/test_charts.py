import os
import tempfile
import unittest

from NEMO_smart_lab.charts import get_chart_json, render_chart_png, _align_series, _line_series_json
from NEMO_smart_lab.tests.test_readers import HeaterLogTests, _write_heater_log


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
    def test_full_resolution_no_downsampling(self):
        # uPlot renders dense series natively - the response always carries every real point,
        # never a decimated subset.
        n = 50_000
        series = {"Heater 1": (list(range(n)), [float(i % 11) for i in range(n)])}
        result = _line_series_json(series)
        self.assertEqual(len(result["x"]), n)
        self.assertEqual(len(result["series"][0]["y"]), n)

    def test_every_series_in_a_group_stays_aligned(self):
        n = 5000
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

    def test_empty_series_returns_empty_shape(self):
        result = _line_series_json({})
        self.assertEqual(result, {"x": [], "series": []})


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
        self.assertEqual(data["y_label"], "Temperature (°C)")

    def test_explicit_group_key_returns_that_group(self):
        data = get_chart_json(self.cfg, group_key="mfc_flow")
        self.assertEqual(data["y_label"], "Flow (sccm)")
        self.assertEqual(data["series"][0]["name"], "MFC 1")

    def test_unknown_group_key_falls_back_to_first_group(self):
        data = get_chart_json(self.cfg, group_key="not-a-real-group")
        self.assertEqual(data["y_label"], "Temperature (°C)")

    def test_png_renders_for_a_named_group_without_error(self):
        png_bytes = render_chart_png(self.cfg, group_key="mfc_flow")
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    def test_hide_excludes_a_series_the_user_unchecked_on_the_interactive_legend(self):
        # A channel unchecked on uPlot's own legend before clicking "Download as image" (see
        # smart_lab_charts.js's download-link click handler) must not show up in the PNG either.
        from unittest.mock import patch

        with patch("NEMO_smart_lab.charts._finish") as mock_finish:
            mock_finish.return_value = b""
            render_chart_png(self.cfg, hide={"Heater 6"})
        fig, ax = mock_finish.call_args[0]
        plotted_labels = {line.get_label() for line in ax.get_lines()}
        self.assertNotIn("Heater 6", plotted_labels)
        self.assertIn("Heater 7", plotted_labels)

    def test_hiding_every_series_shows_the_no_data_placeholder(self):
        from unittest.mock import patch

        all_names = {f"Heater {n}" for n in range(6, 18)}
        with patch("NEMO_smart_lab.charts._finish") as mock_finish:
            mock_finish.return_value = b""
            render_chart_png(self.cfg, hide=all_names)
        fig, ax = mock_finish.call_args[0]
        self.assertEqual(ax.get_lines(), [])
        _legend_outside_kwarg = mock_finish.call_args[1].get("legend_outside")
        self.assertFalse(_legend_outside_kwarg)


class EventsPngRejectionTests(unittest.TestCase):
    """render_chart_png() must refuse to render an Events tab as an image past
    MAX_EVENTS_FOR_PNG - not just hidden client-side (smart_lab_charts.js), but rejected
    server-side too, so a stale/bookmarked/hand-typed chart.png?group=events URL can't force a
    giant, unreadable scatter render either."""

    def setUp(self):
        from unittest.mock import patch

        self.cfg = {"kind": "heater_log", "root": "unused"}
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _points(self, n):
        return [(float(i), "Events", f"event {i}", False) for i in range(n)]

    def _patch(self, target, **kwargs):
        from unittest.mock import patch

        p = patch(target, **kwargs)
        self._patchers.append(p)
        return p.start()

    def test_heater_log_events_over_the_limit_is_rejected(self):
        from NEMO_smart_lab.charts import MAX_EVENTS_FOR_PNG

        self._patch("NEMO_smart_lab.charts.get_heater_log_run_events", return_value=("Run", self._points(MAX_EVENTS_FOR_PNG + 1)))
        png_bytes = render_chart_png(self.cfg, group_key="events")
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))  # still a valid (error-message) image

    def test_heater_log_events_at_the_limit_renders_normally(self):
        from unittest.mock import patch

        from NEMO_smart_lab.charts import MAX_EVENTS_FOR_PNG

        self._patch("NEMO_smart_lab.charts.get_heater_log_run_events", return_value=("Run", self._points(MAX_EVENTS_FOR_PNG)))
        with patch("NEMO_smart_lab.charts._render_scatter_timeline") as mock_render:
            mock_render.return_value = b"\x89PNG"
            render_chart_png(self.cfg, group_key="events")
        mock_render.assert_called_once()

    def test_mvd_events_over_the_limit_is_rejected(self):
        from NEMO_smart_lab.charts import MAX_EVENTS_FOR_PNG

        cfg = {"kind": "mvd", "root": "unused"}
        self._patch("NEMO_smart_lab.charts.get_mvd_run_events", return_value=("Run", self._points(MAX_EVENTS_FOR_PNG + 5)))
        png_bytes = render_chart_png(cfg, group_key="events")
        self.assertTrue(png_bytes.startswith(b"\x89PNG"))

    def test_mvd_events_at_the_limit_renders_normally(self):
        from unittest.mock import patch

        from NEMO_smart_lab.charts import MAX_EVENTS_FOR_PNG

        cfg = {"kind": "mvd", "root": "unused"}
        self._patch("NEMO_smart_lab.charts.get_mvd_run_events", return_value=("Run", self._points(MAX_EVENTS_FOR_PNG)))
        with patch("NEMO_smart_lab.charts._render_scatter_timeline") as mock_render:
            mock_render.return_value = b"\x89PNG"
            render_chart_png(cfg, group_key="events")
        mock_render.assert_called_once()


if __name__ == "__main__":
    unittest.main()
