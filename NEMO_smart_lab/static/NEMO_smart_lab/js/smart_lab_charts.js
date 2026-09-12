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

/** The "scroll to zoom · drag to select · pinch to zoom · reset zoom" hint (mountId + "-zoomhint"
 * in the template) only makes sense for an actual zoomable line chart - meaningless above a plain
 * events list, so it's hidden whenever the active tab isn't chart_type "line". */
function smartLabSetZoomHintVisible(mountId, visible) {
    var hint = document.getElementById(mountId + "-zoomhint");
    if (hint) {
        hint.hidden = !visible;
    }
}

/** The "Download as image" link (mountId + "-download" in the template) isn't worth offering for
 * a short events list - a handful of rows is already fully readable on the page, and a PNG
 * snapshot of a plain text list is a poor substitute for the interactive one anyway. Once the
 * list is long enough to actually benefit from a static copy, it's shown again. */
function smartLabSetDownloadLinkVisible(mountId, visible) {
    var wrapper = document.getElementById(mountId + "-download");
    if (wrapper) {
        wrapper.hidden = !visible;
    }
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

// A run of this many or more *consecutive* events sharing the same category (mvd/fiji5's own
// "STATUS;"/"MFCLOOP;"/"DIGOUT;"/etc. tags - see readers._parse_mvd_evt; heater_log's own events
// have no such tag and are always "Events", so they never collapse) collapses into one summary
// row instead of flooding the list - real recipes can log tens of thousands of events (confirmed
// live: a single fiji5 run logged 13,000+), most of them repetitive valve/MFC/heater-loop chatter.
// Nothing is dropped - the collapsed row expands to the real, full, ungrouped list on click.
var SMART_LAB_EVENT_COLLAPSE_THRESHOLD = 3;

/** A plain chronological list (heater_log's/mvd's Events tab) - not every chart_type is a chart. */
function smartLabRenderList(mountId, errorBoxId, data) {
    var mount = document.getElementById(mountId);
    smartLabDestroyChart(mountId);

    if (!data.items || !data.items.length) {
        smartLabShowError(mountId, errorBoxId, "No events recorded for this run.");
        return;
    }

    var wrapper = document.createElement("div");

    var toolbar = document.createElement("div");
    toolbar.style.cssText = "display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 8px";
    var searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.className = "form-control input-sm";
    searchInput.style.cssText = "flex: 1 1 200px";
    searchInput.placeholder = "Search events…";
    var categorySelect = document.createElement("select");
    categorySelect.className = "form-control input-sm";
    categorySelect.style.cssText = "flex: 0 1 160px; width: auto";
    var allTypesOption = document.createElement("option");
    allTypesOption.value = "";
    allTypesOption.textContent = "All types";
    categorySelect.appendChild(allTypesOption);
    Array.from(new Set(data.items.map(function (item) { return item.category || ""; })))
        .filter(function (category) { return category; })
        .sort()
        .forEach(function (category) {
            var option = document.createElement("option");
            option.value = category;
            option.textContent = category;
            categorySelect.appendChild(option);
        });
    // Only worth showing when a run actually has more than one kind of event - a single-category
    // list has nothing to narrow down.
    categorySelect.hidden = categorySelect.options.length <= 2;
    var controls = document.createElement("div");
    controls.style.cssText = "display: flex; flex-wrap: wrap; gap: 8px; align-items: center";
    var expandAllBtn = document.createElement("button");
    expandAllBtn.type = "button";
    expandAllBtn.className = "btn btn-default btn-sm";
    expandAllBtn.textContent = "Expand all";
    var collapseAllBtn = document.createElement("button");
    collapseAllBtn.type = "button";
    collapseAllBtn.className = "btn btn-default btn-sm";
    collapseAllBtn.textContent = "Collapse all";
    var countLabel = document.createElement("span");
    countLabel.className = "text-muted small";
    controls.appendChild(expandAllBtn);
    controls.appendChild(collapseAllBtn);
    controls.appendChild(countLabel);
    toolbar.appendChild(searchInput);
    toolbar.appendChild(categorySelect);
    toolbar.appendChild(controls);

    var table = document.createElement("table");
    table.className = "table table-condensed table-striped";
    table.style.tableLayout = "fixed";
    table.style.width = "100%";
    // With table-layout:fixed, column widths otherwise come from whichever row happens to be
    // rendered *first* - a real, confirmed bug here: a collapsed group's summary row didn't set
    // per-cell widths the way addRow()'s rows did, so whenever a page happened to start with a
    // summary row, all three columns silently ended up equal-width instead of "note message gets
    // the remaining space". A <colgroup> makes the column widths authoritative and row-order-
    // independent - every row (summary or individual) now gets the same widths no matter what.
    var colgroup = document.createElement("colgroup");
    [70, 110, null].forEach(function (width) {
        var col = document.createElement("col");
        if (width) {
            col.style.width = width + "px";
        }
        colgroup.appendChild(col);
    });
    table.appendChild(colgroup);
    // Two separate <tbody> elements sharing one table/toolbar: `tbody` (browse mode - the
    // grouped/collapsible view built below, paginated on its own) and `searchTbody` (search/type
    // filter mode - a flat, freshly-rebuilt, separately-paginated list of just the matches, since
    // a filtered-down set doesn't have a stable notion of "consecutive collapsed runs" the way the
    // full unfiltered list does). Exactly one of the two is ever visible at a time.
    var tbody = document.createElement("tbody");
    var searchTbody = document.createElement("tbody");
    searchTbody.hidden = true;

    function addRow(item) {
        var tr = document.createElement("tr");
        if (item.fault) {
            tr.className = "danger";
        }
        var offsetTd = document.createElement("td");
        offsetTd.style.whiteSpace = "nowrap";
        offsetTd.textContent = item.offset.toFixed(1) + "s";
        var categoryTd = document.createElement("td");
        categoryTd.style.whiteSpace = "nowrap";
        categoryTd.className = "text-muted small";
        categoryTd.textContent = item.category || "";
        var textTd = document.createElement("td");
        textTd.style.overflowWrap = "break-word";
        textTd.textContent = item.text;
        tr.appendChild(offsetTd);
        tr.appendChild(categoryTd);
        tr.appendChild(textTd);
        return tr;
    }

    function itemMatches(item, query, category) {
        if (category && item.category !== category) {
            return false;
        }
        return !query || (item.category || "").toLowerCase().indexOf(query) !== -1 || item.text.toLowerCase().indexOf(query) !== -1;
    }

    // Each block is either {type: "single", item, row} or {type: "group", items, summaryRow,
    // rows, toggle, setExpanded} - built once up front (see below), then re-visited by
    // applySearch()/"Expand all"/"Collapse all" without ever re-parsing/re-fetching anything.
    var blocks = [];

    // A short list has nothing worth collapsing - the "N similar events" grouping (and the
    // Expand/Collapse-all controls, hidden below) only earns its keep once a run is genuinely
    // long enough to benefit from it.
    var collapsingEnabled = data.items.length >= 50;
    expandAllBtn.hidden = !collapsingEnabled;
    collapseAllBtn.hidden = !collapsingEnabled;

    if (!collapsingEnabled) {
        data.items.forEach(function (item) {
            var row = addRow(item);
            tbody.appendChild(row);
            blocks.push({type: "single", item: item, row: row});
        });
    }

    // Run-length-encode consecutive same-category groups (never merges across a fault - a fault
    // always gets its own visible row, never buried inside a collapsed group).
    var i = 0;
    while (collapsingEnabled && i < data.items.length) {
        var item = data.items[i];
        var j = i + 1;
        if (!item.fault) {
            while (j < data.items.length && data.items[j].category === item.category && !data.items[j].fault) {
                j++;
            }
        }
        var runLength = j - i;
        if (runLength >= SMART_LAB_EVENT_COLLAPSE_THRESHOLD) {
            (function (start, end, groupItem) {
                var count = end - start;
                var groupItems = data.items.slice(start, end);
                var summaryTr = document.createElement("tr");
                summaryTr.className = "active";
                var offsetTd = document.createElement("td");
                offsetTd.style.whiteSpace = "nowrap";
                offsetTd.textContent = groupItem.offset.toFixed(1) + "s";
                var categoryTd = document.createElement("td");
                categoryTd.style.whiteSpace = "nowrap";
                categoryTd.className = "text-muted small";
                categoryTd.textContent = groupItem.category || "";
                var textTd = document.createElement("td");
                var toggle = document.createElement("a");
                toggle.href = "#";
                textTd.appendChild(toggle);
                summaryTr.appendChild(offsetTd);
                summaryTr.appendChild(categoryTd);
                summaryTr.appendChild(textTd);
                tbody.appendChild(summaryTr);

                // Built eagerly (not lazily on first click) - real data is never dropped from the
                // DOM, only visually hidden, so "expand" is instant with no extra fetch/parse.
                var rows = groupItems.map(function (groupItem2) {
                    var row = addRow(groupItem2);
                    row.hidden = true;
                    tbody.appendChild(row);
                    return row;
                });

                var block = {type: "group", items: groupItems, summaryRow: summaryTr, rows: rows, expanded: false};
                block.setExpanded = function (value) {
                    block.expanded = value;
                    summaryTr.hidden = value;
                    rows.forEach(function (row) {
                        row.hidden = !value;
                    });
                    toggle.textContent = value ? "Collapse " + count + " events" : count + " similar events – click to expand";
                };
                block.setExpanded(false);
                toggle.onclick = function (e) {
                    e.preventDefault();
                    block.setExpanded(!block.expanded);
                };
                blocks.push(block);
            })(i, j, item);
        } else {
            for (var k = i; k < j; k++) {
                var row = addRow(data.items[k]);
                tbody.appendChild(row);
                blocks.push({type: "single", item: data.items[k], row: row});
            }
        }
        i = j;
    }

    // A very long, non-repetitive event list (many distinct categories, so collapsing above
    // doesn't shrink it much) can still balloon the page - paged in blocks of PAGE_SIZE *rows*
    // (each collapsed group counts as one, whatever it collapses) once there are enough of them.
    var PAGE_SIZE = 20;
    var browsePaginationEnabled = blocks.length > PAGE_SIZE;
    var browsePage = 0;
    var browseTotalPages = Math.max(1, Math.ceil(blocks.length / PAGE_SIZE));

    function setBlockVisible(block, visible) {
        if (block.type === "single") {
            block.row.hidden = !visible;
        } else if (!visible) {
            block.summaryRow.hidden = true;
            block.rows.forEach(function (row) {
                row.hidden = true;
            });
        } else {
            block.setExpanded(block.expanded);
        }
    }

    function isFiltering() {
        return !!(searchInput.value.trim() || categorySelect.value);
    }

    // Search/type-filter mode is a completely separate, flat (no grouping/collapsing - a filtered
    // subset doesn't have a stable notion of "consecutive runs" the way the full list does),
    // independently-paginated view - rebuilt fresh into searchTbody each time the query, the type
    // filter, or the search page changes, rather than trying to reuse/reshuffle the browse-mode
    // blocks and their DOM.
    var searchPage = 0;
    var lastFilterKey = null;

    function renderSearchPage() {
        var query = searchInput.value.trim().toLowerCase();
        var category = categorySelect.value;
        var filterKey = query + " " + category;
        if (filterKey !== lastFilterKey) {
            searchPage = 0;
            lastFilterKey = filterKey;
        }
        var matches = data.items.filter(function (item) {
            return itemMatches(item, query, category);
        });
        var totalPages = Math.max(1, Math.ceil(matches.length / PAGE_SIZE));
        searchPage = Math.min(searchPage, totalPages - 1);
        var pageItems = matches.slice(searchPage * PAGE_SIZE, (searchPage + 1) * PAGE_SIZE);

        searchTbody.innerHTML = "";
        pageItems.forEach(function (item) {
            searchTbody.appendChild(addRow(item));
        });
        countLabel.textContent = matches.length + " of " + data.items.length + " events match";
        return {page: searchPage, totalPages: totalPages};
    }

    function refresh() {
        var filtering = isFiltering();
        tbody.hidden = filtering;
        searchTbody.hidden = !filtering;
        countLabel.textContent = "";
        if (filtering) {
            var result = renderSearchPage();
            refreshPager(result.page, result.totalPages, true);
        } else {
            blocks.forEach(function (block, index) {
                var onCurrentPage = !browsePaginationEnabled || (index >= browsePage * PAGE_SIZE && index < (browsePage + 1) * PAGE_SIZE);
                setBlockVisible(block, onCurrentPage);
            });
            refreshPager(browsePage, browseTotalPages, browsePaginationEnabled);
        }
    }

    var pager = document.createElement("div");
    pager.className = "text-center";
    pager.style.margin = "8px 0";
    var prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "btn btn-default btn-sm";
    prevBtn.textContent = "‹ Prev";
    var pageLabel = document.createElement("span");
    pageLabel.style.margin = "0 10px";
    var nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "btn btn-default btn-sm";
    nextBtn.textContent = "Next ›";
    pager.appendChild(prevBtn);
    pager.appendChild(pageLabel);
    pager.appendChild(nextBtn);

    function refreshPager(page, totalPages, visible) {
        pager.hidden = !visible;
        prevBtn.disabled = page === 0;
        nextBtn.disabled = page >= totalPages - 1;
        pageLabel.textContent = "Page " + (page + 1) + " of " + totalPages;
    }
    prevBtn.onclick = function () {
        if (isFiltering()) {
            searchPage = Math.max(0, searchPage - 1);
        } else {
            browsePage = Math.max(0, browsePage - 1);
        }
        refresh();
    };
    nextBtn.onclick = function () {
        if (isFiltering()) {
            searchPage++;
        } else {
            browsePage = Math.min(browseTotalPages - 1, browsePage + 1);
        }
        refresh();
    };

    searchInput.addEventListener("input", refresh);
    categorySelect.addEventListener("change", refresh);
    expandAllBtn.onclick = function () {
        blocks.forEach(function (block) {
            if (block.type === "group") {
                block.setExpanded(true);
            }
        });
        refresh();
    };
    collapseAllBtn.onclick = function () {
        blocks.forEach(function (block) {
            if (block.type === "group") {
                block.setExpanded(false);
            }
        });
        refresh();
    };

    refresh();

    table.appendChild(tbody);
    table.appendChild(searchTbody);
    wrapper.appendChild(toolbar);
    wrapper.appendChild(table);
    wrapper.appendChild(pager);
    mount.innerHTML = "";
    mount.appendChild(wrapper);
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
    // resetScales=true here (unlike the zoom-refinement path below) - the whole point of "reset
    // zoom" is to snap the visible x-range back to the full run, not just refresh the data
    // underneath whatever range was already zoomed into.
    smartLabFetchUplotRange(entry, smartLabStripRangeParams(entry.baseUrl), true);
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
    // Only meaningful when a group has several series and says (via default_visible - currently
    // just mvd's own multi-gauge pressure tabs, see readers._mvd_default_visible_pressure_channel)
    // that only one is the "main" one worth showing up front - the rest start unchecked but stay
    // one click away on uPlot's own legend (built-in click-to-toggle, no extra plugin needed).
    var defaultVisible = data.default_visible && data.default_visible.length ? data.default_visible : null;
    var seriesOpts = [{label: data.x_label}].concat(
        data.series.map(function (s, i) {
            return {
                label: s.name,
                stroke: SMART_LAB_CHART_COLORS[i % SMART_LAB_CHART_COLORS.length],
                width: 1.5,
                points: {show: false},
                show: defaultVisible ? defaultVisible.indexOf(s.name) !== -1 : true,
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
        // resetScales=false: this is a re-fetch for the range the user already zoomed/panned to,
        // not a brand new view - letting uPlot auto-range from the new data here would fight the
        // scale the user just set (and would re-trigger the setScale hook, looping).
        smartLabFetchUplotRange(entry, smartLabBuildRangeUrl(entry.baseUrl, start, end), false);
    }, 300);
}

function smartLabFetchUplotRange(entry, url, resetScales) {
    fetch(url, {credentials: "same-origin"})
        .then(function (response) { return response.json(); })
        .then(function (data) {
            if (data.chart_type === "error" || !data.series || !data.series.length) {
                return; // Keep showing whatever's already on screen rather than blanking it.
            }
            entry.instance.setData(smartLabUplotAlignedData(data), resetScales);
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
