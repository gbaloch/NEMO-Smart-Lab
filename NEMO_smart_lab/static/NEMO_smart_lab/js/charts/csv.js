/**
 * Client-side "Download as CSV" for charts and event lists.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
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
