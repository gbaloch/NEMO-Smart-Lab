"""Shared chart helpers: line colors (matching the browser charts), title suffixes, and finishing a matplotlib figure into PNG bytes."""

import matplotlib

matplotlib.use("Agg")


import io
import matplotlib.pyplot as plt
import re

from NEMO_smart_lab.readers import get_chart_groups


def _resolve_chart_group(cfg, run_id, group_key):
    """The requested group, or the first (Temperature) one if group_key is None/blank/unknown -
    unknown falls back rather than 404ing, since a stale bookmarked/cached URL for a group that
    no longer exists on a later run shouldn't break instead of just showing the primary chart."""
    groups = get_chart_groups(cfg, run_id)
    if group_key:
        group = next((g for g in groups if g["key"] == group_key), None)
        if group is not None:
            return group
    return groups[0]


# Same palette as static/NEMO_smart_lab/js/charts/'s SMART_LAB_CHART_COLORS, so a
# channel's line color matches between the interactive uPlot canvas and its "download as image"
# PNG counterpart instead of matplotlib's own default color cycle.
LINE_CHART_COLORS = [
    "#337ab7", "#5cb85c", "#d9534f", "#f0ad4e", "#5bc0de",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
]


# Mirrors js/charts's SMART_LAB_FIXED_SERIES_COLORS exactly
_FIXED_SERIES_COLORS = [
    (re.compile("optkita", re.IGNORECASE), "#5cb85c"),
    (re.compile("reactor", re.IGNORECASE), "#337ab7"),
]


def _series_color(name, index):
    for pattern, color in _FIXED_SERIES_COLORS:
        if pattern.search(name or ""):
            return color
    return LINE_CHART_COLORS[index % len(LINE_CHART_COLORS)]


def _with_run_suffix(title, username=None, timestamp=None):
    """"<recipe title> - <username> - <run timestamp>" with whichever of username/timestamp are
    actually known appended (either, both, or neither) - see views.py's tool_detail, which
    resolves the run's username (from the same reservation/usage lookup already shown on the page)
    and its already-formatted "Run ended"/"Last update" display string once, then passes both
    through as query params rather than looking them up again per chart."""
    parts = [p for p in (username, timestamp) if p]
    return f"{title} - {' - '.join(parts)}" if parts else title


def _finish(fig, ax, legend_outside=False):
    ax.grid(True, linestyle=":", linewidth=0.5)
    if legend_outside:
        # Placed outside the axes (to the right) instead of overlaid on the plot, so it never
        # covers data - bbox_inches="tight" on save (below) is what keeps it from being clipped
        # off the edge of the saved image.
        ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=7)
    else:
        fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight" if legend_outside else None)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
