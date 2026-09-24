/**
 * Shared chart state and small DOM helpers: series colors, the chart registry, range-URL helpers, error/zoom-hint/download-link visibility.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
var SMART_LAB_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
];

var SMART_LAB_FIXED_SERIES_COLORS = [
    {match: /optkita/i, color: "#5cb85c"},
    {match: /reactor/i, color: "#337ab7"},
];

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
