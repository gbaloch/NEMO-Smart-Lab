/**
 * Renders the JSON a Smart Lab chart.json/stream.json endpoint returns (see charts.py's
 * get_chart_json()/get_stream_chart_json()) into a mount <div>, dispatched by "chart_type":
 *   - "line" (heater_log/mvd/waferlog/stream) -> uPlot, a micro-library purpose-built for dense
 *     time-series - much faster than a general-purpose charting library at this specific workload
 *     (long runs with tens of thousands of points), and lighter on mobile. See smartLabRenderUplot.
 *     Always the real, full-resolution data - no server-side downsampling.
 *   - "list" (heater_log's Events tab) -> a plain chronological list, not a chart - a scatter plot
 *     of sparse, irregularly-timed text events doesn't read well as a chart.
 *   - "gantt" (cobra_job step timing) / "scatter" (eventlog event timeline) -> not currently
 *     configured for any tool; no interactive JS renderer for these (see smartLabDispatchChartData) -
 *     only the PNG "download as image" endpoint (still matplotlib-rendered, unaffected) covers them.
 *   - "error" -> the run/tool couldn't be read; shown as plain text.
 *
 * Zoom fidelity for line charts is deliberately NOT "stretch a fixed pre-downsampled buffer":
 * every zoom/pan settle (drag-select release, wheel stop, pinch end) re-fetches real data scoped
 * to the new visible x-range (chart.json's start/end params - see charts.py's _line_series_json)
 * and swaps it in via uPlot.setData(), so zooming in actually reveals more real detail.
 */

var SMART_LAB_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
];

// Keyed by mount element id, one entry per active uPlot instance (line charts are the only
// interactive chart type - see the file header).
var SMART_LAB_CHARTS = {};

// uPlot's own legend markup (a <table class="u-legend"> under the plot, one <tr class="u-series">
// per series) defaults to loose spacing that can look scattered with many series (fiji5's Power
// group alone has 5) - injected once, tightens it into a compact, centered, wrapping list without
// fighting its native table layout.
(function injectUplotLegendStyle() {
    var style = document.createElement("style");
    style.textContent =
        ".u-legend { font-size: 11px; margin: 6px auto 0; border-spacing: 0 2px; }" +
        ".u-legend tr.u-series > th { padding: 1px 10px 1px 0; white-space: nowrap; font-weight: normal; }" +
        ".u-legend .u-marker { margin-right: 4px; }";
    document.head.appendChild(style);
})();

function smartLabStripRangeParams(url) {
    var parsed = new URL(url, window.location.origin);
    parsed.searchParams.delete("start");
    parsed.searchParams.delete("end");
    return parsed.toString();
}

function smartLabBuildRangeUrl(baseUrl, start, end) {
    var parsed = new URL(baseUrl, window.location.origin);
    parsed.searchParams.set("start", start);
    parsed.searchParams.set("end", end);
    return parsed.toString();
}

function smartLabDestroyChart(mountId) {
    var existing = SMART_LAB_CHARTS[mountId];
    if (!existing) {
        return;
    }
    existing.instance.destroy();
    clearTimeout(existing.debounceTimer);
    delete SMART_LAB_CHARTS[mountId];
}

function smartLabShowError(mountId, errorBoxId, message) {
    var mount = document.getElementById(mountId);
    var errorBox = document.getElementById(errorBoxId);
    smartLabDestroyChart(mountId);
    mount.innerHTML = "";
    mount.style.display = "none";
    errorBox.style.display = "block";
    errorBox.textContent = message;
}

/**
 * Given an already-fetched chart.json/stream.json response, renders it into the `mountId` <div>
 * (created empty by the template - uPlot and the plain event list both render straight into it).
 * Shared by smartLabRenderChart() (a single fetch-and-render, no caching - the stream telemetry
 * chart and any single-group tool) and smartLabInitTabbedChart() (per-group caching + tab
 * switching, for tools with more than one chart group).
 */
function smartLabDispatchChartData(mountId, errorBoxId, data, url) {
    var mount = document.getElementById(mountId);
    var errorBox = document.getElementById(errorBoxId);

    if (data.chart_type === "error") {
        smartLabShowError(mountId, errorBoxId, data.message);
        return;
    }
    mount.style.display = "block";
    errorBox.style.display = "none";

    if (data.chart_type === "line") {
        smartLabRenderUplot(mountId, errorBoxId, data, url);
        return;
    }
    if (data.chart_type === "list") {
        smartLabRenderList(mountId, errorBoxId, data);
        return;
    }

    // "gantt"/"scatter" (cobra_job/eventlog) have no interactive JS renderer - not currently
    // configured for any tool, and not worth an interactive chart implementation with nothing
    // live to verify it against. The PNG "download as image" link still covers them.
    smartLabShowError(mountId, errorBoxId, "This chart isn't available as an interactive view - use \"Download as image\" below.");
}

/** A plain chronological list (heater_log's Events tab) - not every chart_type is a chart. */
function smartLabRenderList(mountId, errorBoxId, data) {
    var mount = document.getElementById(mountId);
    smartLabDestroyChart(mountId);

    if (!data.items || !data.items.length) {
        smartLabShowError(mountId, errorBoxId, "No events recorded for this run.");
        return;
    }

    var table = document.createElement("table");
    table.className = "table table-condensed table-striped";
    var tbody = document.createElement("tbody");
    data.items.forEach(function (item) {
        var tr = document.createElement("tr");
        if (item.fault) {
            tr.className = "danger";
        }
        var offsetTd = document.createElement("td");
        offsetTd.style.whiteSpace = "nowrap";
        offsetTd.style.width = "1%";
        offsetTd.textContent = item.offset.toFixed(1) + "s";
        var textTd = document.createElement("td");
        textTd.textContent = item.text;
        tr.appendChild(offsetTd);
        tr.appendChild(textTd);
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    mount.innerHTML = "";
    mount.appendChild(table);
}

/** Main entry point for a chart with only one possible view (no tabs) - fetches `url` once. */
function smartLabRenderChart(mountId, errorBoxId, url) {
    fetch(url, {credentials: "same-origin"})
        .then(function (response) { return response.json(); })
        .then(function (data) { smartLabDispatchChartData(mountId, errorBoxId, data, url); })
        .catch(function (err) {
            smartLabShowError(mountId, errorBoxId, "Could not load chart data (" + err + ").");
        });
}

/**
 * For a tool with multiple chart groups (e.g. Temperature/Flow/Power/...): renders one at a time
 * into `mountId`, switched via a Bootstrap nav-tabs strip at "#<mountId>-tabs" (one <li
 * data-group-key="..."> per group, built by the template). Only ever fetches a given group's data
 * once per page view - switching back to an already-visited tab re-renders from the cached
 * response instead of hitting the network again. jsonUrlsByKey/pngUrlsByKey are {key: url} maps
 * for chart.json and the "download as image" chart.png link respectively.
 */
function smartLabInitTabbedChart(mountId, errorBoxId, jsonUrlsByKey, pngUrlsByKey, defaultKey) {
    var cache = {};

    function activate(key) {
        var downloadLink = document.getElementById(mountId + "-download-link");
        if (downloadLink) {
            downloadLink.href = pngUrlsByKey[key];
        }
        if (cache[key]) {
            smartLabDispatchChartData(mountId, errorBoxId, cache[key], jsonUrlsByKey[key]);
            return;
        }
        fetch(jsonUrlsByKey[key], {credentials: "same-origin"})
            .then(function (response) { return response.json(); })
            .then(function (data) {
                cache[key] = data;
                smartLabDispatchChartData(mountId, errorBoxId, data, jsonUrlsByKey[key]);
            })
            .catch(function (err) {
                smartLabShowError(mountId, errorBoxId, "Could not load chart data (" + err + ").");
            });
    }

    document.querySelectorAll("#" + mountId + "-tabs [data-group-key]").forEach(function (link) {
        link.addEventListener("click", function (e) {
            e.preventDefault();
            document.querySelectorAll("#" + mountId + "-tabs li").forEach(function (li) {
                li.classList.remove("active");
            });
            link.parentElement.classList.add("active");
            activate(link.getAttribute("data-group-key"));
        });
    });

    activate(defaultKey);
}

function smartLabResetZoom(mountId) {
    var entry = SMART_LAB_CHARTS[mountId];
    if (!entry) {
        return;
    }
    smartLabFetchUplotRange(entry, smartLabStripRangeParams(entry.baseUrl));
}

// ==================== uPlot (line charts) ====================

function smartLabUplotAlignedData(data) {
    return [data.x].concat(data.series.map(function (s) { return s.y; }));
}

function smartLabRenderUplot(mountId, errorBoxId, data, baseUrl) {
    var mount = document.getElementById(mountId);
    if (!data.series || !data.series.length) {
        smartLabShowError(mountId, errorBoxId, "No data to plot for this run.");
        return;
    }

    smartLabDestroyChart(mountId);
    mount.innerHTML = "";

    // series[0] is uPlot's own x-axis pseudo-series - labeling it with the real x_label (e.g.
    // "Time (s)") instead of leaving it blank is what shows up in the legend for it; unlabeled,
    // uPlot's legend shows a generic placeholder there instead, which read as an unexplained
    // "Value" entry with no indication it meant the x-axis.
    var seriesOpts = [{label: data.x_label}].concat(
        data.series.map(function (s, i) {
            return {
                label: s.name,
                stroke: SMART_LAB_CHART_COLORS[i % SMART_LAB_CHART_COLORS.length],
                width: 1.5,
                points: {show: false},
            };
        })
    );

    var firstRender = true;
    var opts = {
        // No `title` - the group's own label is already shown via the tab/heading above this
        // chart, and uPlot reserving extra vertical space for a second, duplicate title inside a
        // fixed-height container was what pushed the legend down far enough to get visually
        // covered/clipped.
        width: mount.clientWidth || mount.parentElement.clientWidth || 600,
        height: 400,
        series: seriesOpts,
        // Our x-values are plain seconds-since-start floats, not unix timestamps - "time: false"
        // is required or uPlot's default time-scale formatting misreads them as dates.
        scales: {x: {time: false}},
        axes: [{label: data.x_label}, {label: data.y_label}],
        legend: {show: true},
        cursor: {drag: {x: true, y: false}},
        hooks: {
            // Fires for every kind of x-range change - drag-select release (uPlot's own built-in
            // behavior), and our own wheel/pinch handlers below, which both just call
            // u.setScale("x", ...) directly rather than each needing their own re-fetch logic.
            setScale: [
                function (u, scaleKey) {
                    if (scaleKey !== "x" || firstRender) {
                        firstRender = false;
                        return;
                    }
                    var entry = SMART_LAB_CHARTS[mountId];
                    if (entry) {
                        smartLabDebouncedRangeFetch(entry, u.scales.x.min, u.scales.x.max);
                    }
                },
            ],
        },
    };

    var instance = new uPlot(opts, smartLabUplotAlignedData(data), mount);
    var entry = {type: "uplot", instance: instance, baseUrl: baseUrl, mountId: mountId, errorBoxId: errorBoxId, debounceTimer: null};
    SMART_LAB_CHARTS[mountId] = entry;

    smartLabAttachUplotZoom(instance);

    if (typeof ResizeObserver !== "undefined") {
        var observer = new ResizeObserver(function () {
            if (mount.clientWidth) {
                instance.setSize({width: mount.clientWidth, height: 400});
            }
        });
        observer.observe(mount);
    }
}

function smartLabDebouncedRangeFetch(entry, start, end) {
    clearTimeout(entry.debounceTimer);
    entry.debounceTimer = setTimeout(function () {
        smartLabFetchUplotRange(entry, smartLabBuildRangeUrl(entry.baseUrl, start, end));
    }, 300);
}

function smartLabFetchUplotRange(entry, url) {
    fetch(url, {credentials: "same-origin"})
        .then(function (response) { return response.json(); })
        .then(function (data) {
            if (data.chart_type === "error" || !data.series || !data.series.length) {
                return; // Keep showing whatever's already on screen rather than blanking it.
            }
            // resetScales=false: this is a re-fetch for the range the user already zoomed/panned
            // to, not a brand new view - letting uPlot auto-range from the new data here would
            // fight the scale the user just set (and would re-trigger the setScale hook, looping).
            entry.instance.setData(smartLabUplotAlignedData(data), false);
        })
        .catch(function () {
            // A failed background refinement fetch leaves the current (still-valid, just less
            // detailed) view in place rather than surfacing an error for what looks like an
            // otherwise-working chart.
        });
}

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
        u.setScale("x", {min: dataX - newRange * ratio, max: dataX + newRange * (1 - ratio)});
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
            u.setScale("x", {min: pinchCenterX - newRange * ratio, max: pinchCenterX + newRange * (1 - ratio)});
        },
        {passive: false}
    );

    u.over.addEventListener("touchend", function () {
        pinchStartDist = null;
    });
}
