/**
 * Renders the JSON a Smart Lab chart.json/stream.json endpoint returns (see charts.py's
 * get_chart_json()/get_stream_chart_json()) onto a <canvas> using the vendored Chart.js build,
 * instead of the older static chart.png/stream.png <img>. One render function per "chart_type"
 * the backend can send: "line" (heater_log/mvd/waferlog/stream), "gantt" (cobra_job step
 * timing), "scatter" (eventlog event timeline), "error" (the run/tool couldn't be read).
 */

var SMART_LAB_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
];

function smartLabRenderChart(canvasId, errorBoxId, url) {
    var canvas = document.getElementById(canvasId);
    var errorBox = document.getElementById(errorBoxId);

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
            new Chart(canvas.getContext("2d"), config);
        })
        .catch(function (err) {
            canvas.style.display = "none";
            errorBox.style.display = "block";
            errorBox.textContent = "Could not load chart data (" + err + ").";
        });
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
            plugins: {title: {display: true, text: data.title}, legend: {position: "bottom"}},
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
            plugins: {title: {display: true, text: data.title}, legend: {display: false}},
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
            },
            scales: {
                x: {type: "linear", title: {display: true, text: data.x_label}},
                y: {type: "category", labels: data.modules},
            },
        },
    };
}
