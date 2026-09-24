/**
 * Elapsed-time formatting for run charts and the events list.
 *
 * Part of the Smart Lab chart frontend - plain global scripts loaded in order by
 * templates/NEMO_smart_lab/_chart_scripts.html (core, format, csv, event_list, uplot, uplot_interactions,
 * tables, init). Functions are called by name across files, only once every file has loaded.
 */
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
