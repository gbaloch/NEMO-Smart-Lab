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
 *
 * Because that re-fetch is asynchronous (and can take real time - up to several seconds on a cold
 * cache), wheel/pinch zoom-out and panning keep the visible x-scale always backed by SOME data -
 * real or a null-valued placeholder - the instant the scale moves, rather than only after the
 * fetch resolves; see smartLabExtendDataToScale's own docstring for the "hover point doesn't match
 * the mouse" bug this fixes.
 */

/**
 * Given an already-fetched chart.json/stream.json response, renders it into the `mountId` <div>
 * (created empty by the template - uPlot and the plain event list both render straight into it).
 * Shared by smartLabRenderChart() (a single fetch-and-render, no caching - the stream telemetry
 * chart and any single-group tool) and smartLabInitTabbedChart() (per-group caching + tab
 * switching, for tools with more than one chart group).
 */
function smartLabDispatchChartData(mountId, errorBoxId, data, url, tabLabel) {
    var mount = document.getElementById(mountId);
    var errorBox = document.getElementById(errorBoxId);

    // Independent of the chart_type branching below - see smartLabSetCsvDownload's own docstring
    // for why the CSV link's visibility deliberately doesn't follow the "Download as image" link's
    // own length-based hiding.
    smartLabSetCsvDownload(mountId, data, tabLabel);

    if (data.chart_type === "error") {
        smartLabShowError(mountId, errorBoxId, data.message);
        smartLabSetZoomHintVisible(mountId, false);
        smartLabSetDownloadLinkVisible(mountId, false);
        return;
    }
    mount.style.display = "block";
    errorBox.style.display = "none";

    if (data.chart_type === "line") {
        smartLabSetZoomHintVisible(mountId, true);
        smartLabSetDownloadLinkVisible(mountId, true);
        smartLabRenderUplot(mountId, errorBoxId, data, url);
        return;
    }
    smartLabSetZoomHintVisible(mountId, false);
    if (data.chart_type === "list") {
        smartLabSetDownloadLinkVisible(mountId, data.items && data.items.length <= 10);
        smartLabRenderList(mountId, errorBoxId, data);
        return;
    }
    smartLabSetDownloadLinkVisible(mountId, true);

    // "gantt"/"scatter" (cobra_job/eventlog) have no interactive JS renderer - not currently
    // configured for any tool, and not worth an interactive chart implementation with nothing
    // live to verify it against. The PNG "download as image" link still covers them.
    smartLabShowError(mountId, errorBoxId, "This chart isn't available as an interactive view - use \"Download as image\" below.");
}

/**
 * Chamber base-pressure-over-time chart (tool_detail.html overview page) - like
 * smartLabRenderChart below, but with an optional "Past 1 year / 5 years / 10 years / All time"
 * range picker (the "<mountId>-range"/"<mountId>-range-select" elements in the template), shown
 * only once the data's own "full_range_days" (see charts.get_base_pressure_chart_json) says
 * there's actually more than a year of history to narrow down - a short history has nothing
 * meaningful to filter. Picking a range re-fetches (real server-side filtering, not just a
 * client-side zoom of the same points) and keeps the "download as image"/"download as CSV" links
 * pointed at whatever range is currently selected, same reasoning as
 * smartLabInitTabbedChart's own download-link syncing.
 */
function smartLabInitBasePressureChart(mountId, errorBoxId, jsonUrl, pngUrl, csvUrl, runLinkBase) {
    var rangeWrapper = document.getElementById(mountId + "-range");
    var rangeSelect = document.getElementById(mountId + "-range-select");
    var imageLink = document.getElementById(mountId + "-download-link");
    var csvLink = document.getElementById(mountId + "-csv-link");

    function urlWithRange(base, range) {
        if (!range || range === "all") {
            return base;
        }
        var url = new URL(base, window.location.origin);
        url.searchParams.set("range", range);
        return url.toString();
    }

    function load(range) {
        if (imageLink) {
            imageLink.href = urlWithRange(pngUrl, range);
        }
        if (csvLink) {
            csvLink.href = urlWithRange(csvUrl, range);
        }
        var url = urlWithRange(jsonUrl, range);
        fetch(url, {credentials: "same-origin"})
            .then(function (response) { return response.json(); })
            .then(function (data) {
                // Not part of the server's own response shape - stapled on here so
                // smartLabRenderUplot can wire up "click a point to jump to that run" only for
                // this particular chart (every other line chart has no such per-point run_id).
                if (runLinkBase) {
                    data.run_link_base = runLinkBase;
                }
                smartLabDispatchChartData(mountId, errorBoxId, data, url);
                if (rangeWrapper) {
                    rangeWrapper.hidden = !(data.full_range_days && data.full_range_days > 365);
                }
            })
            .catch(function (err) {
                smartLabShowError(mountId, errorBoxId, "Could not load chart data (" + err + ").");
            });
    }

    if (rangeSelect) {
        rangeSelect.addEventListener("change", function () {
            load(rangeSelect.value);
        });
    }

    load(rangeSelect ? rangeSelect.value : null);
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
    var currentKey = null;
    var downloadLink = document.getElementById(mountId + "-download-link");

    // A channel unchecked on the interactive uPlot legend is left out of the PNG too, rather than
    // "Download as image" silently including channels the user just explicitly turned off -
    // computed fresh on each click (not when the tab activates) since the user can keep toggling
    // series after a tab first loads. No-op for a tab that isn't a line chart (nothing to hide).
    if (downloadLink) {
        downloadLink.addEventListener("click", function () {
            var entry = SMART_LAB_CHARTS[mountId];
            var baseUrl = pngUrlsByKey[currentKey];
            if (!entry || entry.type !== "uplot" || !baseUrl) {
                downloadLink.href = baseUrl || "#";
                return;
            }
            var url = new URL(baseUrl, window.location.origin);
            url.searchParams.delete("hide");
            // series[0] is uPlot's own x-axis pseudo-series - real channels start at index 1.
            entry.instance.series.slice(1).forEach(function (s) {
                if (s.show === false) {
                    url.searchParams.append("hide", s.label);
                }
            });
            downloadLink.href = url.toString();
        });
    }

    function activate(key) {
        currentKey = key;
        if (downloadLink) {
            downloadLink.href = pngUrlsByKey[key];
        }
        // The tab's own visible text (e.g. "Temperature (°C)") - used by smartLabSetCsvDownload to
        // prefix the CSV filename, since data.title alone is often identical across every tab of a
        // tabbed chart (see smartLabCsvNamePrefix's docstring).
        var tabEl = document.querySelector("#" + mountId + '-tabs [data-group-key="' + key + '"]');
        var tabLabel = tabEl ? tabEl.textContent.trim() : null;
        if (cache[key]) {
            smartLabDispatchChartData(mountId, errorBoxId, cache[key], jsonUrlsByKey[key], tabLabel);
            return;
        }
        fetch(jsonUrlsByKey[key], {credentials: "same-origin"})
            .then(function (response) { return response.json(); })
            .then(function (data) {
                cache[key] = data;
                smartLabDispatchChartData(mountId, errorBoxId, data, jsonUrlsByKey[key], tabLabel);
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
    // resetScales=true here (unlike the zoom-refinement path below) - the whole point of "reset
    // zoom" is to snap the visible x-range back to the full run, not just refresh the data
    // underneath whatever range was already zoomed into.
    smartLabFetchUplotRange(entry, smartLabStripRangeParams(entry.baseUrl), true);
}

// ==================== uPlot (line charts) ====================
