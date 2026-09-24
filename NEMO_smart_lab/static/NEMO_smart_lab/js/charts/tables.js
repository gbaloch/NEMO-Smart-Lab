/**
 * Client-side pagination for the weekly trend tables.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
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
