from django import template

register = template.Library()


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
