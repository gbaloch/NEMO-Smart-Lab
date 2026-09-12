from django import template

from NEMO_smart_lab.models import SmartLabToolChannel

register = template.Library()

_ROLE_LABELS = dict(SmartLabToolChannel.ROLE_CHOICES)


@register.filter
def role_label(role):
    """A channel dict's "role" is the raw SmartLabToolChannel.ROLE_CHOICES code (e.g.
    "source_valve") - plain dicts don't get Django's usual get_FOO_display() model helper, so this
    does the same lookup for template use."""
    return _ROLE_LABELS.get(role, role)


@register.filter
def humanize_key(key):
    """A tool's "extra" dict (readers.py, e.g. "cycles_remaining", "lot_id", "machine_id") is
    keyed by internal snake_case field names meant for code, not display - this turns
    "cycles_remaining" into "Cycles remaining" for the tool detail page's extra-fields table."""
    if not isinstance(key, str):
        return key
    return key.replace("_", " ").capitalize()


@register.filter
def smart_duration(seconds):
    """
    Formats a duration in seconds, collapsed to whatever units are actually needed: a
    52-second run reads "52.0s", a 137-second run reads "2m 17s", a 14753-second run reads
    "4h 5m 53s" - never a leading "0h 0m" for something that only lasted a few seconds.
    """
    if seconds is None:
        return "-"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"

    if seconds < 60:
        return f"{seconds:.1f}s"

    total = int(round(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _range_date(dt):
    return dt.strftime("%m/%d/%Y")


def _range_time(dt):
    return dt.strftime("%-I:%M %p").lower()


@register.filter
def range_start(start):
    """The first half of a reservation/usage "start - end" display: always date + time (e.g.
    "09/10/2026 9:57 am") - use alongside range_end, which collapses away its own date whenever
    it's the same calendar day as this one."""
    if start is None:
        return start
    return f"{_range_date(start)} {_range_time(start)}"


@register.filter
def range_end(end, start):
    """The second half of a "start - end" display - just the time (e.g. "1:47 pm") when `end`
    falls on the same calendar day as `start`, or "date time" when the reservation/usage genuinely
    spans multiple days. Returns None (not a string) when `end` is None, so the template's own
    `|default:"in progress"`/`|default:"now"` still applies exactly as it did before this filter."""
    if end is None:
        return None
    if start is not None and end.date() == start.date():
        return _range_time(end)
    return f"{_range_date(end)} {_range_time(end)}"


@register.filter
def strip_txt(value):
    """
    Strips a trailing ".txt" extension for display purposes only - e.g. a run_id/source_file
    like "clear0.txt" reads as "clear0". Never apply this to a value being used to build a
    ?run= link - the underlying run_id needs its real extension to resolve to a file.
    """
    if isinstance(value, str) and value.lower().endswith(".txt"):
        return value[: -len(".txt")]
    return value
