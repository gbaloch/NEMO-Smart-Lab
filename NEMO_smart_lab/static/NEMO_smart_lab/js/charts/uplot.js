/**
 * uPlot line-chart rendering, zoom-triggered range re-fetch, and keeping the visible scale backed by data.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
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
