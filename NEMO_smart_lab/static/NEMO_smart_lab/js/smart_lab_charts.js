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

var SMART_LAB_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
];

var SMART_LAB_FIXED_SERIES_COLORS = [
    {match: /optkita/i, color: "#5cb85c"},
    {match: /reactor/i, color: "#337ab7"},
];

/**
 * Formats a plain-seconds elapsed-time value - collapsed to whatever units are actually needed
 * ("52.0s", "2m 17.3s", "4h 5m 53.7s", never a leading "0h 0m") - used for both the "seconds since
 * run start" x-axis on a long run's own chart (raw seconds alone gets unreadable past a couple
 * thousand) and each event's own elapsed-time offset in the Events list, so a many-hour run reads
 * in hours/minutes in both places instead of a long, hard-to-parse raw second count.
 *
 * Unlike the server's own smart_lab_filters.smart_duration (which rounds to whole seconds once a
 * value reaches a full minute - fine for a single "run duration" summary line), the seconds
 * component here ALWAYS keeps one decimal place, even past 60 seconds - real event offsets in the
 * Events list are only ever a fraction of a second apart from each other at that point in a run
 * (confirmed live), and rounding away that decimal made two, three, or more genuinely-different
 * events all display as the exact same "46m 2s", collapsing them together into what looked like
 * one indistinguishable timestamp.
 */
function smartLabFormatElapsed(seconds) {
    if (seconds == null || isNaN(seconds)) {
        return "-";
    }
    if (seconds < 0) {
        return "-";
    }
    if (seconds < 60) {
        return seconds.toFixed(1) + "s";
    }
    var totalWhole = Math.floor(seconds);
    var fractional = seconds - totalWhole;
    var days = Math.floor(totalWhole / 86400);
    totalWhole -= days * 86400;
    var hours = Math.floor(totalWhole / 3600);
    totalWhole -= hours * 3600;
    var minutes = Math.floor(totalWhole / 60);
    var secs = totalWhole - minutes * 60 + fractional;

    var parts = [];
    if (days) {
        parts.push(days + "d");
    }
    if (hours || parts.length) {
        parts.push(hours + "h");
    }
    if (minutes || parts.length) {
        parts.push(minutes + "m");
    }
    parts.push(secs.toFixed(1) + "s");
    return parts.join(" ");
}

/**
 * uPlot x-axis "values" formatter for a plain seconds-since-run-start axis (see smartLabRenderUplot)
 * - picks ONE unit (seconds/minutes/hours) for the whole axis, from its current visible max, so
 * every tick is consistent (never mixing "45s" next to "2h" on the same axis) rather than
 * reformatting each tick independently.
 */
function smartLabElapsedAxisValues(u, splits, axisIdx, foundIncr) {
    var maxVal = u.scales.x.max || 0;
    // uPlot passes the actual spacing between ticks as `foundIncr` - below 1 second, a zoomed-in
    // view needs a decimal on the seconds part to actually distinguish adjacent ticks (e.g. "1:00.0"
    // vs "1:00.5"), otherwise plain whole seconds is all that's ever meaningfully shown.
    var showDecimal = !!(foundIncr && foundIncr < 1);

    function formatSecondsPart(secs) {
        if (showDecimal) {
            var pieces = secs.toFixed(1).split(".");
            return pieces[0].padStart(2, "0") + "." + pieces[1];
        }
        return String(Math.round(secs)).padStart(2, "0");
    }

    if (maxVal < 60) {
        return splits.map(function (v) {
            return (showDecimal ? v.toFixed(1) : String(Math.round(v))) + "s";
        });
    }
    var showHours = maxVal >= 3600;
    return splits.map(function (v) {
        var hours = Math.floor(v / 3600);
        var minutes = Math.floor((v % 3600) / 60);
        var secs = v - hours * 3600 - minutes * 60;
        var secsStr = formatSecondsPart(secs);
        return showHours ? hours + ":" + String(minutes).padStart(2, "0") + ":" + secsStr : minutes + ":" + secsStr;
    });
}

function smartLabSeriesColor(name, index) {
    for (var i = 0; i < SMART_LAB_FIXED_SERIES_COLORS.length; i++) {
        if (SMART_LAB_FIXED_SERIES_COLORS[i].match.test(name || "")) {
            return SMART_LAB_FIXED_SERIES_COLORS[i].color;
        }
    }
    return SMART_LAB_CHART_COLORS[index % SMART_LAB_CHART_COLORS.length];
}

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
 * Builds a CSV file from `rows` (array of arrays, first row = header) and triggers a normal
 * browser download for it via a temporary, invisible <a download> element - entirely client-side,
 * from whatever chart.json data is already loaded in the page, rather than needing a dedicated
 * server export endpoint for every chart (the way the older, separate base-pressure CSV endpoint
 * does - that one's kept as-is; this covers every OTHER chart, which never had a CSV export at
 * all).
 */
function smartLabDownloadCsvRows(filename, rows) {
    var csv = rows
        .map(function (row) {
            return row
                .map(function (cell) {
                    var value = cell === null || cell === undefined ? "" : String(cell);
                    // Quote (doubling any embedded quotes) only when actually needed - a comma,
                    // quote, or newline inside the value would otherwise corrupt the row structure.
                    return /[",\r\n]/.test(value) ? '"' + value.replace(/"/g, '""') + '"' : value;
                })
                .join(",");
        })
        .join("\r\n");
    var blob = new Blob([csv], {type: "text/csv;charset=utf-8;"});
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

/**
 * Wires up (or hides) a chart's own "Download as CSV" link (mountId + "-csv-link" in the template)
 * from whatever chart.json data is currently displayed - "line" charts export one column per
 * series (plus the shared x column), "list" (Events) exports one row per event. Deliberately
 * independent of smartLabSetDownloadLinkVisible's own length-based visibility for the image link:
 * a short events list is exactly the case a CSV export is most useful for (a handful of rows is
 * still worth taking off-page into a spreadsheet), so the CSV link stays visible whenever there's
 * any real tabular data to export, regardless of length.
 *
 * A no-op for a link that already has its own real (non-"#") href - the chamber base-pressure
 * chart's own CSV link points at a dedicated, range-aware server endpoint
 * (smart_lab_tool_base_pressure_csv) that already does the right thing on its own; this function
 * only ever takes over a plain "#"-href placeholder link (the convention every other chart's own
 * template uses for a JS-managed download link, matching smart-lab-chart-mount-download-link's
 * own image-link placeholder).
 *
 * `tabLabel` (the currently active tab's own visible label, e.g. "Temperature (°C)"/"MFC Flow
 * (sccm)") is used to prefix the downloaded filename, since data.title alone is often the same
 * across every tab of a tabbed chart (e.g. every heater_log/mvd group shares the run's plain
 * "Recipe: <name>" title) - without it, every tab's CSV would download under an identical,
 * unhelpful name ("Recipe_...csv") no matter which tab it actually came from.
 */
function smartLabCsvNamePrefix(tabLabel) {
    if (!tabLabel) {
        return "";
    }
    // Drop a trailing unit annotation like " (°C)"/" (sccm)"/" (Torr)" - not meaningful in a
    // filename - then turn the rest into a plain Word_Word slug ("MFC Flow" -> "MFC_Flow").
    return tabLabel
        .replace(/\s*\([^)]*\)\s*$/, "")
        .trim()
        .replace(/\s+/g, "_");
}

function smartLabCsvFilename(title, tabLabel) {
    var base = title || "chart";
    var prefix = smartLabCsvNamePrefix(tabLabel);
    if (!prefix || base.toLowerCase().indexOf(prefix.toLowerCase()) === 0) {
        return base;
    }
    return prefix + "_" + base;
}

function smartLabSetCsvDownload(mountId, data, tabLabel) {
    var link = document.getElementById(mountId + "-csv-link");
    if (!link) {
        return;
    }
    var href = link.getAttribute("href");
    if (href && href !== "#") {
        return;
    }
    if (data.chart_type === "line" && data.x && data.x.length) {
        link.hidden = false;
        link.onclick = function (e) {
            e.preventDefault();
            var header = [data.x_label || "x"].concat(data.series.map(function (s) { return s.name; }));
            var rows = [header].concat(
                data.x.map(function (xVal, i) {
                    return [xVal].concat(data.series.map(function (s) { return s.y[i]; }));
                })
            );
            smartLabDownloadCsvRows(smartLabCsvFilename(data.title, tabLabel) + ".csv", rows);
        };
        return;
    }
    if (data.chart_type === "list" && data.items && data.items.length) {
        link.hidden = false;
        link.onclick = function (e) {
            e.preventDefault();
            var rows = [["Offset (s)", "Category", "Text", "Fault"]].concat(
                data.items.map(function (item) {
                    return [item.offset, item.category, item.text, item.fault ? "yes" : ""];
                })
            );
            smartLabDownloadCsvRows(smartLabCsvFilename(data.title || "events", tabLabel) + ".csv", rows);
        };
        return;
    }
    link.hidden = true;
    link.onclick = null;
}

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
        offsetTd.textContent = smartLabFormatElapsed(item.offset);
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
                offsetTd.textContent = smartLabFormatElapsed(groupItem.offset);
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
        var filterKey = query + "\0" + category;
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
                stroke: smartLabSeriesColor(s.name, i),
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
        // Almost every chart here uses plain seconds-since-run-start floats, not unix timestamps -
        // "time: false" is required for those or uPlot's default time-scale formatting misreads
        // them as dates. The one exception (data.time_x - currently just the chamber base-pressure
        // history chart, one point per historical run) genuinely does use real unix-second
        // timestamps and wants uPlot's built-in date-aware axis formatting.
        scales: {x: {time: !!data.time_x}},
        // data.time_x charts (real wall-clock timestamps) keep uPlot's own default date-aware tick
        // formatting; every other chart's x-axis is plain seconds-since-run-start, which reads as
        // an unwieldy raw number for a run running into the thousands of seconds (nearly an hour)
        // or more - smartLabElapsedAxisValues reformats those ticks into a consistent h/m/s scale
        // instead (matching smartLabFormatElapsed's own units), chosen once from the axis' own
        // current max so every tick on it uses the same unit rather than mixing "45s" with "2h".
        axes: [{label: data.x_label, values: data.time_x ? null : smartLabElapsedAxisValues}, {label: data.y_label}],
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

    // Chamber base-pressure chart only (data.point_recipes, parallel to "x" - see
    // charts.get_base_pressure_chart_json) - a tool can configure more than one standby recipe, so
    // a bare pressure number alone doesn't say which one produced it. Shown as a small label under
    // the chart (mountId + "-point-info" in the template), updated to the nearest point's own
    // recipe name as the cursor moves - cleared when the cursor leaves the chart (u.cursor.idx is
    // null/undefined there) rather than left showing a stale recipe name.
    if (data.point_recipes) {
        opts.hooks.setCursor = [
            function (u) {
                var infoEl = document.getElementById(mountId + "-point-info");
                if (!infoEl) {
                    return;
                }
                var idx = u.cursor.idx;
                infoEl.textContent = idx != null && data.point_recipes[idx] ? "Recipe: " + data.point_recipes[idx] : "";
            },
        ];
    }

    var instance = new uPlot(opts, smartLabUplotAlignedData(data), mount);
    // fetchSeq: see smartLabFetchUplotRange - guards against an in-flight zoom-refinement request
    // resolving *after* a newer one and clobbering it with stale, narrower-range data.
    var entry = {type: "uplot", instance: instance, baseUrl: baseUrl, mountId: mountId, errorBoxId: errorBoxId, debounceTimer: null, fetchSeq: 0};
    SMART_LAB_CHARTS[mountId] = entry;

    smartLabAttachUplotZoom(instance);
    if (data.point_run_ids && data.run_link_base) {
        smartLabAttachUplotPointLinks(instance, data.point_run_ids, data.run_link_base);
    }

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
    // A real, confirmed bug: rapid zooming (e.g. zoom in a lot, then back out) can fire more than
    // one of these before the first one's response comes back, and network timing gives no
    // guarantee they resolve in the order they were sent - a *wider*-range request (more raw data
    // to filter/serialize server-side, so genuinely slower) issued *before* a narrower one can
    // easily still be in flight when the narrower one's response already landed and updated the
    // chart. Applying that late, stale, narrower dataset on top of a scale that has since moved on
    // (resetScales=false deliberately keeps whatever scale the user is currently looking at) drew
    // real data only across its own narrow x-range while the rest of the now-wider visible scale
    // was left with nothing to plot at all - the reported "zoom out and the rest of the chart
    // disappears", and separately, hovering in that data-less region found no point for the cursor
    // to report even though a line was visible nearby ("zooming in/out seems to fix it" - the next
    // zoom action's own fresh fetch/setData cycle happened to paper over the mismatch). Tagging
    // each request with a sequence number and only ever applying the *latest* one - discarding any
    // response that's no longer current by the time it arrives, however long it took - fixes both
    // regardless of how requests happen to resolve.
    var seq = ++entry.fetchSeq;
    fetch(url, {credentials: "same-origin"})
        .then(function (response) { return response.json(); })
        .then(function (data) {
            // Also bail if this mountId has since moved on to a whole different chart/tab
            // (smartLabRenderUplot already destroyed `entry.instance` and replaced it in
            // SMART_LAB_CHARTS by then) - calling setData on an already-destroyed uPlot instance
            // is its own, separate way to end up applying a stale response.
            if (entry.fetchSeq !== seq || SMART_LAB_CHARTS[entry.mountId] !== entry) {
                return; // A newer request (or a whole new chart) has taken over - this is stale.
            }
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
 * Before optimistically widening/panning the visible x-scale (wheel/pinch - see
 * smartLabAttachUplotZoom), makes sure `u.data` already covers the new [min, max] range - even if
 * only with null-valued placeholder points at the edges - before `u.setScale` actually moves the
 * visible domain there.
 *
 * This is the fix for a persistent, real "hover point doesn't match the mouse" bug: uPlot's cursor
 * finds the data index NEAREST the hovered pixel by searching the WHOLE loaded `u.data` array, not
 * just whatever's currently visible. Zooming out (or panning) moves the visible scale instantly,
 * but the debounced re-fetch that supplies real data for the newly-exposed region takes measurable
 * time (up to several seconds on a cold cache - see remote_cache's own docstrings for why). Without
 * this, hovering anywhere in that not-yet-fetched region - during that whole window - found no real
 * point out there and incorrectly snapped to and reported the OLD boundary point instead, however
 * far the mouse actually was from it: exactly the reported "point doesn't respond to the mouse,
 * depending on zoom" symptom, worse (more noticeable/reproducible) the wider or slower the zoom.
 *
 * A null placeholder renders as a real, honest gap - the same "no data here" signal this codebase
 * already relies on for genuine data gaps elsewhere - so hovering there now correctly shows nothing
 * instead of a misleading stale value, until the real fetch lands and overwrites these placeholders
 * with genuine data (smartLabFetchUplotRange's own setData call does that automatically - no extra
 * wiring needed here).
 *
 * No-op when `min`/`max` are already within the currently-loaded data's own extent - the
 * overwhelmingly common case (a zoom-IN, or a zoom-out still inside what's already loaded) needs no
 * padding at all. Drag-to-select-zoom (uPlot's own built-in `cursor.drag`) never needs this either -
 * a drag selection is always a sub-region of the already-rendered (already-loaded) chart area, so
 * it can only ever narrow, never reach outside currently-loaded data.
 *
 * Builds a plain new array via slice()/concat() rather than mutating `u.data` in place with
 * push()/unshift() - uPlot may store a series internally as a typed array (Float64Array), which
 * has neither method; slice() safely copies either representation into an ordinary, mutable Array.
 */
function smartLabExtendDataToScale(u, min, max) {
    var xs = u.data[0];
    if (!xs || !xs.length) {
        return;
    }
    var dataMin = xs[0];
    var dataMax = xs[xs.length - 1];
    if (min >= dataMin && max <= dataMax) {
        return;
    }
    var prefix = min < dataMin ? [min] : [];
    var suffix = max > dataMax ? [max] : [];
    var newXs = prefix.concat(Array.prototype.slice.call(xs), suffix);
    var newSeries = u.data.slice(1).map(function (s) {
        var nullPrefix = prefix.length ? [null] : [];
        var nullSuffix = suffix.length ? [null] : [];
        return nullPrefix.concat(Array.prototype.slice.call(s), nullSuffix);
    });
    // resetScales=false - this only ever extends the DATA to cover the scale that's about to be
    // set; the scale change itself is the caller's own, separate u.setScale call right after this.
    u.setData([newXs].concat(newSeries), false);
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

/**
 * Client-side pagination for any table carrying the "smart-lab-paginated-table" class (currently
 * the Trends tab's own weekly tables - see _tool_health_trends.html) - the underlying data is
 * already fully rendered server-side into the table's <tbody> (a tool with years of history can
 * mean many dozens of weekly rows across several tables at once), this just hides/shows a page's
 * worth of <tr> elements at a time instead of re-fetching anything.
 *
 * Rows are assumed newest-first (every one of these tables renders "{% for entry in trend
 * reversed %}") - page 1 is therefore the most recent weeks, which is what a viewer opening this
 * tab actually wants to see first, not the oldest history buried at the bottom.
 *
 * Call this again (e.g. after replacing a table's own rows, or - as tool_detail.html's own Trends
 * tab does - right after injecting freshly-fetched fragment HTML via innerHTML, since a <script>
 * tag inside HTML set that way never executes on its own) to (re)build pagination controls for
 * every matching table found under `root` (defaults to the whole document).
 */
function smartLabPaginateTables(root, pageSize) {
    root = root || document;
    pageSize = pageSize || 12;
    Array.prototype.forEach.call(root.querySelectorAll(".smart-lab-paginated-table"), function (table) {
        // Already paginated (e.g. a repeat call after the same fragment reloaded) - tear down the
        // old controls first rather than stacking a second set underneath the table.
        if (table.smartLabPaginationControls) {
            table.smartLabPaginationControls.remove();
            delete table.smartLabPaginationControls;
        }

        var tbody = table.querySelector("tbody");
        if (!tbody) {
            return;
        }
        var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
        if (rows.length <= pageSize) {
            return; // Fits on one page already - no controls needed.
        }

        var page = 0;
        var totalPages = Math.ceil(rows.length / pageSize);

        var controls = document.createElement("div");
        controls.style.cssText = "display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin: 4px 0 12px";

        var prevBtn = document.createElement("button");
        prevBtn.type = "button";
        prevBtn.className = "btn btn-default btn-xs";
        prevBtn.textContent = "‹ Newer";

        var pageLabel = document.createElement("span");
        pageLabel.className = "text-muted small";

        var nextBtn = document.createElement("button");
        nextBtn.type = "button";
        nextBtn.className = "btn btn-default btn-xs";
        nextBtn.textContent = "Older ›";

        function render() {
            rows.forEach(function (row, i) {
                row.hidden = !(i >= page * pageSize && i < (page + 1) * pageSize);
            });
            pageLabel.textContent = "Weeks " + (page * pageSize + 1) + "–" + Math.min((page + 1) * pageSize, rows.length) +
                " of " + rows.length + " (page " + (page + 1) + " of " + totalPages + ")";
            prevBtn.disabled = page === 0;
            nextBtn.disabled = page === totalPages - 1;
        }

        prevBtn.addEventListener("click", function () {
            if (page > 0) {
                page -= 1;
                render();
            }
        });
        nextBtn.addEventListener("click", function () {
            if (page < totalPages - 1) {
                page += 1;
                render();
            }
        });

        controls.appendChild(prevBtn);
        controls.appendChild(pageLabel);
        controls.appendChild(nextBtn);
        table.parentElement.insertBefore(controls, table.nextSibling);
        table.smartLabPaginationControls = controls;
        render();
    });
}
