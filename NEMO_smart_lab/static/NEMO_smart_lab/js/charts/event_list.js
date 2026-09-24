/**
 * The chronological Events list (a plain table, not a chart), with consecutive-event collapsing.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
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
