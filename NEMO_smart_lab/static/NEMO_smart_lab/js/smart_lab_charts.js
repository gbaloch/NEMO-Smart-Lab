/**
 * Renders the JSON a Smart Lab chart.json/stream.json endpoint returns (see charts.py's
 * get_chart_json()/get_stream_chart_json()) onto a <canvas> using the vendored Chart.js build,
 * instead of the older static chart.png/stream.png <img>. One render function per "chart_type"
 * the backend can send: "line" (heater_log/mvd/waferlog/stream), "gantt" (cobra_job step
 * timing), "scatter" (eventlog event timeline), "error" (the run/tool couldn't be read).
 *
 * Interactivity (chartjs-plugin-zoom, vendored alongside Chart.js itself): mouse wheel zooms,
 * click-and-drag draws a selection box to zoom into that interval (handy for comparing two
 * sections of a long run), and Ctrl+drag pans once zoomed in. A "Reset zoom" button (see
 * smartLabResetZoom()) snaps back to the full view - Chart.js has no built-in double-click
 * shortcut for that, so the button is the reliable way back out.
 */

if (typeof Chart !== "undefined" && typeof ChartZoom !== "undefined") {
    Chart.register(ChartZoom);
}

var SMART_LAB_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
];

// Shared across chart types: wheel/drag-to-select zoom, Ctrl+drag to pan once zoomed in. A plain
// drag is reserved for the zoom selection box (the "compare an interval" gesture) rather than
// panning, since panning only matters once already zoomed in, at which point Ctrl+drag is
// discoverable enough via the on-page hint text next to the "Reset zoom" button.
var SMART_LAB_ZOOM_OPTIONS = {
    pan: {enabled: true, mode: "x", modifierKey: "ctrl"},
    zoom: {
        wheel: {enabled: true},
        drag: {enabled: true, backgroundColor: "rgba(51, 122, 183, 0.2)"},
        mode: "x",
    },
};

var SMART_LAB_CHARTS = {};

function smartLabRenderChart(canvasId, errorBoxId, url) {
    var canvas = document.getElementById(canvasId);
    var errorBox = document.getElementById(errorBoxId);
    var note = document.getElementById(canvasId + "-note");

    fetch(url, {credentials: "same-origin"})
        .then(function (response) {
            return response.json();
        })
        .then(function (data) {
            if (data.chart_type === "error") {
                canvas.style.display = "none";
                errorBox.style.display = "block";
                errorBox.textContent = data.message;
                return;
            }
            var config = smartLabChartConfig(data);
            if (!config) {
                canvas.style.display = "none";
                errorBox.style.display = "block";
                errorBox.textContent = "No data to plot for this run.";
                return;
            }
            if (SMART_LAB_CHARTS[canvasId]) {
                SMART_LAB_CHARTS[canvasId].destroy();
            }
            SMART_LAB_CHARTS[canvasId] = new Chart(canvas.getContext("2d"), config);
            if (note) {
                note.style.display = data.downsampled ? "block" : "none";
            }
        })
        .catch(function (err) {
            canvas.style.display = "none";
            errorBox.style.display = "block";
            errorBox.textContent = "Could not load chart data (" + err + ").";
        });
}

function smartLabResetZoom(canvasId) {
    var chart = SMART_LAB_CHARTS[canvasId];
    if (chart && typeof chart.resetZoom === "function") {
        chart.resetZoom();
    }
}

function smartLabChartConfig(data) {
    if (data.chart_type === "line") {
        return smartLabLineConfig(data);
    }
    if (data.chart_type === "gantt") {
        return smartLabGanttConfig(data);
    }
    if (data.chart_type === "scatter") {
        return smartLabScatterConfig(data);
    }
    return null;
}

function smartLabLineConfig(data) {
    if (!data.series || !data.series.length) {
        return null;
    }
    var datasets = data.series.map(function (s, i) {
        var color = SMART_LAB_CHART_COLORS[i % SMART_LAB_CHART_COLORS.length];
        var points = s.x.map(function (x, j) {
            return {x: x, y: s.y[j]};
        });
        return {
            label: s.name,
            data: points,
            borderColor: color,
            backgroundColor: color,
            borderWidth: 1.5,
            pointRadius: 1,
            tension: 0.1,
        };
    });
    return {
        type: "line",
        data: {datasets: datasets},
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            parsing: false,
            interaction: {mode: "nearest", axis: "x", intersect: false},
            plugins: {
                title: {display: true, text: data.title},
                legend: {position: "bottom"},
                zoom: SMART_LAB_ZOOM_OPTIONS,
            },
            scales: {
                x: {type: "linear", title: {display: true, text: data.x_label}},
                y: {title: {display: true, text: data.y_label}},
            },
        },
    };
}

function smartLabGanttConfig(data) {
    if (!data.bars || !data.bars.length) {
        return null;
    }
    var labels = data.bars.map(function (b, i) {
        return "Step " + i + ": " + b.label;
    });
    var points = data.bars.map(function (b) {
        return [b.start, b.start + b.duration];
    });
    return {
        type: "bar",
        data: {
            labels: labels,
            datasets: [
                {
                    data: points,
                    backgroundColor: labels.map(function (_, i) {
                        return SMART_LAB_CHART_COLORS[i % SMART_LAB_CHART_COLORS.length];
                    }),
                },
            ],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                title: {display: true, text: data.title},
                legend: {display: false},
                zoom: SMART_LAB_ZOOM_OPTIONS,
            },
            scales: {x: {title: {display: true, text: data.x_label}}},
        },
    };
}

function smartLabScatterConfig(data) {
    if (!data.points || !data.points.length) {
        return null;
    }
    var normalPoints = data.points.filter(function (p) { return !p.fault; });
    var faultPoints = data.points.filter(function (p) { return p.fault; });
    function toXY(p) {
        return {x: p.x, y: p.module, label: p.event};
    }
    return {
        type: "scatter",
        data: {
            datasets: [
                {label: "Event", data: normalPoints.map(toXY), backgroundColor: "#337ab7"},
                {label: "Fault/Alarm", data: faultPoints.map(toXY), backgroundColor: "#d9534f"},
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            plugins: {
                title: {display: true, text: data.title},
                legend: {position: "bottom"},
                tooltip: {callbacks: {label: function (ctx) { return ctx.raw.label; }}},
                zoom: SMART_LAB_ZOOM_OPTIONS,
            },
            scales: {
                x: {type: "linear", title: {display: true, text: data.x_label}},
                y: {type: "category", labels: data.modules},
            },
        },
    };
}
