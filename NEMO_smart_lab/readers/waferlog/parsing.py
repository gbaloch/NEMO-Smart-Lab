"""Parsing one WaferLog file."""

import os
import re

from NEMO_smart_lab.readers.common import ToolDataError


#
# Same idea as the Fiji/Savannah heater logs, but each wafer run is its own file (no
# "most recently modified file in a folder" ambiguity beyond picking the newest by mtime),
# and the interesting per-run channels are the endpoint-detector traces recorded during the
# run plus each step's RF power - "on" here means plasma was actually struck, not that a
# heater is above ambient.
_GAS_KEYS = ("Ar", "CH4", "N2a", "N2b", "N2c", "N2d", "O2", "SF6", "SiH4")


def _parse_waferlog(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if not lines:
        raise ToolDataError(f"Wafer log file is empty: {path}")

    machine_id = recipe = start_time = end_time = ""
    duration_s = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("machineID:"):
            for field in line.rstrip("\n").split("\t"):
                field = field.strip()
                if field.startswith("machineID:"):
                    machine_id = field.split(":", 1)[1].strip()
                elif field.startswith("Recipe:"):
                    recipe = field.split(":", 1)[1].strip()
        elif stripped.startswith("Start:"):
            m = re.search(r"Start:\(([^)]+)\)\((\d+)\)\s*End:\(([^)]+)\)\s*\((\d+)\)", stripped)
            if m:
                start_time, end_time = m.group(1).strip(), m.group(3).strip()
                duration_s = (int(m.group(4)) - int(m.group(2))) / 1000.0

    # Step table (used only to find the last step's RF power, for the "was plasma on" flag)
    header_idx = None
    for i, line in enumerate(lines):
        if line.split("\t", 1)[0].strip() == "Step":
            header_idx = i
            break
    steps = []
    if header_idx is not None and header_idx + 1 < len(lines):
        header_fields = [h.strip() for h in lines[header_idx].rstrip("\n").split("\t")]
        unit_fields = [u.strip() for u in lines[header_idx + 1].rstrip("\n").split("\t")]
        colnames, seen = [], {}
        for h, u in zip(header_fields, unit_fields):
            col = "Step" if h == "Step" else (h if u in ("", "()", "(none)", "(Units)") else f"{h} {u}")
            seen[col] = seen.get(col, 0) + 1
            colnames.append(col if seen[col] == 1 else f"{col} #{seen[col]}")
        i = header_idx + 2
        while i < len(lines):
            raw = lines[i].rstrip("\n")
            if raw.strip() == "":
                i += 1
                continue
            if raw.strip().startswith("HistoricalData:"):
                break
            steps.append({name: val.strip() for name, val in zip(colnames, raw.split("\t"))})
            i += 1

    rf_on = False
    for step in steps:
        for key, val in step.items():
            if key.startswith("RFICPGeneratorP") or (key.startswith("RFBiasGenerator") and "(W)" in key):
                try:
                    if float(val) != 0.0:
                        rf_on = True
                except ValueError:
                    pass

    # Endpoint-detector channel history
    hd_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("HistoricalData:"):
            hd_idx = i
            break
    channel_names, channel_units, channel_time, channel_data = [], [], [], []
    if hd_idx is not None and hd_idx + 2 < len(lines):
        name_fields = lines[hd_idx + 1].rstrip("\n").split("\t")
        unit_fields = lines[hd_idx + 2].rstrip("\n").split("\t")
        num_channels = min(len(name_fields), len(unit_fields)) // 2
        channel_names = [name_fields[2 * c].strip() for c in range(num_channels)]
        channel_units = [unit_fields[2 * c + 1].strip() for c in range(num_channels)]
        channel_time = [[] for _ in range(num_channels)]
        channel_data = [[] for _ in range(num_channels)]
        i = hd_idx + 3
        while i < len(lines):
            raw = lines[i].rstrip("\n")
            if raw.strip() == "":
                i += 1
                continue
            if raw.strip().startswith("Process sequence summary"):
                break
            values = raw.split("\t")
            for c in range(num_channels):
                tcol, vcol = 2 * c, 2 * c + 1
                if vcol >= len(values):
                    continue
                traw, vraw = values[tcol].strip(), values[vcol].strip()
                if traw in ("", "---") or vraw in ("", "---", "nil"):
                    continue
                try:
                    channel_time[c].append(float(traw))
                    channel_data[c].append(float(vraw))
                except ValueError:
                    pass
            i += 1

    return {
        "path": path,
        "run_id": os.path.basename(path),
        "machine_id": machine_id,
        "recipe": recipe,
        "mtime": os.path.getmtime(path),
        "duration_s": duration_s,
        "rf_on": rf_on,
        "channel_names": channel_names,
        "channel_units": channel_units,
        "channel_time": channel_time,
        "channel_data": channel_data,
    }
