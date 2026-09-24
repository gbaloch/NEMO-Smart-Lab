/**
 * uPlot wheel/drag/pinch zoom and click-a-point-to-open-that-run.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
/**
 * uPlot ships no zoom/pan out of the box (deliberately minimal core) - drag-to-select-zoom comes
 * from the `cursor.drag` option above (uPlot's own built-in behavior, no extra code needed here).
 * This adds the other two gestures: mouse wheel (desktop) and two-finger pinch (touch, the
 * mobile-friendly gesture that - unlike a single-finger drag inside the chart - never conflicts
 * with the page's normal vertical scroll).
 */
function smartLabAttachUplotZoom(u) {
    var zoomFactor = 0.85;

    function zoomAround(dataX, factor) {
        var scale = u.scales.x;
        var range = scale.max - scale.min;
        var newRange = range * factor;
        var ratio = range === 0 ? 0.5 : (dataX - scale.min) / range;
        var newMin = dataX - newRange * ratio;
        var newMax = dataX + newRange * (1 - ratio);
        smartLabExtendDataToScale(u, newMin, newMax);
        u.setScale("x", {min: newMin, max: newMax});
    }

    u.over.addEventListener(
        "wheel",
        function (e) {
            e.preventDefault();
            var rect = u.over.getBoundingClientRect();
            var dataX = u.posToVal(e.clientX - rect.left, "x");
            zoomAround(dataX, e.deltaY < 0 ? zoomFactor : 1 / zoomFactor);
        },
        {passive: false}
    );

    var pinchStartDist = null;
    var pinchStartRange = null;
    var pinchCenterX = null;

    function touchDist(touches) {
        var dx = touches[0].clientX - touches[1].clientX;
        var dy = touches[0].clientY - touches[1].clientY;
        return Math.sqrt(dx * dx + dy * dy);
    }

    u.over.addEventListener(
        "touchstart",
        function (e) {
            if (e.touches.length !== 2) {
                return;
            }
            pinchStartDist = touchDist(e.touches);
            pinchStartRange = u.scales.x.max - u.scales.x.min;
            var rect = u.over.getBoundingClientRect();
            var midX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
            pinchCenterX = u.posToVal(midX, "x");
        },
        {passive: true}
    );

    u.over.addEventListener(
        "touchmove",
        function (e) {
            if (e.touches.length !== 2 || pinchStartDist === null) {
                return;
            }
            e.preventDefault();
            var factor = pinchStartDist / touchDist(e.touches); // pinch-out (fingers apart) zooms in
            var newRange = pinchStartRange * factor;
            var scale = u.scales.x;
            var ratio = pinchStartRange === 0 ? 0.5 : (pinchCenterX - scale.min) / (scale.max - scale.min);
            var newMin = pinchCenterX - newRange * ratio;
            var newMax = pinchCenterX + newRange * (1 - ratio);
            smartLabExtendDataToScale(u, newMin, newMax);
            u.setScale("x", {min: newMin, max: newMax});
        },
        {passive: false}
    );

    u.over.addEventListener("touchend", function () {
        pinchStartDist = null;
    });
}

/**
 * Chamber base-pressure chart only (see smartLabInitBasePressureChart's "run_link_base") - each
 * point is one specific standby run, so clicking a point jumps straight to that run's own full
 * detail page rather than leaving the viewer to go hunt for it. `runIds` is parallel to the
 * chart's own x-array (data.point_run_ids from charts.get_base_pressure_chart_json).
 *
 * Distinguishes an actual click from the end of a drag-to-zoom (uPlot's own cursor.drag, or this
 * file's own wheel/pinch handlers all fire on the same element) by how far the pointer moved
 * between mousedown and this click - a real click barely moves at all, a drag obviously does.
 */
function smartLabAttachUplotPointLinks(u, runIds, runLinkBase) {
    var downX = null;
    var downY = null;

    u.over.addEventListener("mousedown", function (e) {
        downX = e.clientX;
        downY = e.clientY;
    });

    u.over.style.cursor = "pointer";

    u.over.addEventListener("click", function (e) {
        if (downX !== null && (Math.abs(e.clientX - downX) > 5 || Math.abs(e.clientY - downY) > 5)) {
            return; // a drag-to-zoom ending on this element, not a real click on a point
        }
        var xData = u.data[0];
        if (!xData || !xData.length) {
            return;
        }
        var rect = u.over.getBoundingClientRect();
        var clickVal = u.posToVal(e.clientX - rect.left, "x");
        // xData is sorted ascending (oldest run first) - binary search for the closest index.
        var lo = 0, hi = xData.length - 1;
        while (lo < hi) {
            var mid = (lo + hi) >> 1;
            if (xData[mid] < clickVal) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        if (lo > 0 && Math.abs(xData[lo - 1] - clickVal) <= Math.abs(xData[lo] - clickVal)) {
            lo -= 1;
        }
        var runId = runIds[lo];
        if (runId) {
            window.location.href = runLinkBase + "?run=" + encodeURIComponent(runId);
        }
    });
}
