"""Optional PTIQ live telemetry (StreamedData) for Cobra tools."""

import os
import struct

from datetime import datetime

from NEMO_smart_lab.readers.common import ToolDataError


#
# Separate from Jobs.db - this is the tool's raw, high-frequency process telemetry
# (PTIQ/Databases/StreamedData/<module>/<year>/<month>/<day>/<hour>/<minute>_PD.stream), a
# sequence of msgpack objects: a header, a table of ~300 channel id->name definitions, then a
# long alternating sequence of either ("m", elapsed_ms) time markers or (channel_id, value)
# updates. Optional because it needs the "msgpack" package and a "stream_root" pointing at a
# synced copy of PTIQ/Databases/StreamedData - most sites won't have that set up.
#
# Reverse-engineered quirk: PTIQ writes its float64 channel values in *little-endian* byte
# order, which is the opposite of what the msgpack spec requires (big-endian). A spec-correct
# msgpack decoder (like the "msgpack" package used here) still "succeeds" at decoding them -
# it just produces nonsense subnormal floats near zero, which is why a first pass at this data
# looked like it was full of empty/garbage values. Byte-swapping each decoded float (undo the
# decoder's big-endian read, redo it little-endian) turns them back into real, plausible
# process values (e.g. a chamber pressure channel decoded this way holds steady around 0.7,
# matching a real idle-chamber Torr reading, instead of ~1e-314).
#
# This only ever looks at whatever the single most recently modified minute-file is - it's a
# live "what's happening right now" snapshot, not a historical trace across many files (there
# can be thousands of these for a long-lived tool, and each one has to be fully decoded to be
# read at all, so scanning a long history of them isn't practical to do on every request).
_STREAM_INTERESTING_CHANNEL_SUFFIXES = (
    "RFGen1.ForwardPower.Value",
    "RFGen1.ReflectedPower.Value",
    "RFGen1.DCBias",
    "RFGen2.ForwardPower.Value",
    "RFGen2.ReflectedPower.Value",
    "RFGen2.DCBias",
    "APC.Pressure.Value",
    "ProcessGauge.Pressure.Value",
    "HIVACGauge.Pressure.Value",
)


def _latest_stream_file(stream_root, module):
    path = os.path.join(stream_root, module)
    if not os.path.isdir(path):
        raise ToolDataError(f"StreamedData module folder not found: {path}")
    # Walk year -> month -> day -> hour, taking the lexicographically-latest (i.e. most
    # recent, since these are zero-padded numeric names) entry at each level.
    for _ in range(4):
        entries = [e for e in os.listdir(path) if os.path.isdir(os.path.join(path, e))]
        if not entries:
            raise ToolDataError(f"No dated StreamedData folders found under: {path}")
        path = os.path.join(path, max(entries))
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".stream")]
    if not files:
        raise ToolDataError(f"No .stream files found in: {path}")
    return max(files, key=os.path.basename)


def _fix_stream_float(value):
    """Undoes msgpack's spec-correct big-endian float64 read and redoes it little-endian,
    matching how PTIQ actually wrote it. See the module-level comment above for how this was
    figured out. Non-float values (small integer state/enum codes) are passed through as-is -
    only float64 payloads exhibit this byte-order quirk."""
    if isinstance(value, float):
        return struct.unpack("<d", struct.pack(">d", value))[0]
    return value


def _parse_stream_file(path):
    try:
        import msgpack
    except ImportError as e:
        raise ToolDataError("The 'msgpack' package is required to read StreamedData files") from e

    with open(path, "rb") as f:
        data = f.read()
    unpacker = msgpack.Unpacker(raw=False)
    unpacker.feed(data)
    items = list(unpacker)
    markers, payloads = items[0::2], items[1::2]

    module_name = None
    channel_names = {}
    for marker, payload in zip(markers, payloads):
        if marker == "h" and isinstance(payload, dict):
            module_name = payload.get("m")
        elif marker == "d" and isinstance(payload, list) and len(payload) == 4 and isinstance(payload[1], str):
            channel_names[payload[0]] = payload[1]

    series = {}  # channel_id -> ([elapsed_ms, ...], [value, ...])
    elapsed_ms = 0
    for marker, payload in zip(markers, payloads):
        if marker == "m":
            elapsed_ms = payload
        elif isinstance(marker, int) and marker in channel_names:
            times, values = series.setdefault(marker, ([], []))
            times.append(elapsed_ms)
            values.append(_fix_stream_float(payload))

    return {
        "path": path,
        "run_id": os.path.basename(path),
        "mtime": os.path.getmtime(path),
        "module_name": module_name,
        "channel_names": channel_names,
        "series": series,
    }


def _interesting_stream_channels(data):
    for channel_id, name in data["channel_names"].items():
        if any(name.endswith(suffix) for suffix in _STREAM_INTERESTING_CHANNEL_SUFFIXES):
            yield channel_id, name


def get_stream_summary(cfg):
    """Returns a small dict describing the latest StreamedData snapshot, or None if this tool
    isn't configured for it (no "stream_root") or none could be read."""
    stream_root = cfg.get("stream_root")
    if not stream_root:
        return None
    try:
        data = _parse_stream_file(_latest_stream_file(stream_root, cfg.get("stream_module", "PMC1")))
    except ToolDataError:
        return None

    channels = []
    for channel_id, name in _interesting_stream_channels(data):
        _, values = data["series"].get(channel_id, ([], []))
        channels.append({"name": name, "latest_value": values[-1] if values else None})
    channels.sort(key=lambda c: c["name"])

    return {
        "run_id": data["run_id"],
        "module_name": data["module_name"],
        "timestamp": datetime.fromtimestamp(data["mtime"]),
        "channels": channels,
    }


def get_stream_chart_data(cfg):
    stream_root = cfg.get("stream_root")
    if not stream_root:
        raise ToolDataError("No stream_root configured for this tool")
    data = _parse_stream_file(_latest_stream_file(stream_root, cfg.get("stream_module", "PMC1")))
    series = {}
    for channel_id, name in _interesting_stream_channels(data):
        times, values = data["series"].get(channel_id, ([], []))
        if times:
            series[name] = ([t / 1000.0 for t in times], values)
    title = f"Live telemetry - {data['module_name'] or ''} ({data['run_id']})"
    return title, "Time (s)", "", series
