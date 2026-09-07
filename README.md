# NEMO Smart Lab

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/NEMO-smart-lab?label=python)](https://www.python.org/downloads/release/python-3110/)
[![PyPI](https://img.shields.io/pypi/v/nemo-smart-lab?label=pypi%20version)](https://pypi.org/project/NEMO-smart-lab/)
[![Changelog](https://img.shields.io/github/v/release/SNF-Root/NEMO-Smart-Lab/nemo-smart-lab?include_prereleases&label=changelog)](https://github.com/SNF-Root/NEMO-Smart-Lab/releases)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/SNF-Root/NEMO-Smart-Lab/blob/main/LICENSE)

> [!IMPORTANT]
> This project is an active work in project, and many features listed are not fully functional or reliable. Please refrain from production use for now.

Plugin for [NEMO](https://github.com/usnistgov/NEMO) to process and display instrument data (telemetry data, past run logs, recipes, etc.) collected from tools connected to NEMO in an easy-to-use manner, allowing staff to reliably diagnose problems outside of the cleanroom.

This is part of a broader project built at [Stanford University's nanolabs](https://nanolabs.stanford.edu/) to network cleanroom tools through our [Secure Research Lab Network](https://uit.stanford.edu/service/secure-research-lab-network) and make the data available on our High-Performance Computing storage solution, [Oak](https://uit.stanford.edu/service/oak-storage), which this plugin (and a separate staging machine) uploads and accesses the collected data through a SSH-based Data Transfer Node. However, this system has been designed to be modular and easily extensible, allowing a wide range of deployment across different universities and other environments regardless of their IT configuration.

This repository houses the code for the NEMO plugin, as well as the shell scripts the staging machine runs. Actual processing of non-flatfile data is currently performed in the plugin, but in the future will be done prior to a cloud upload during staging (either on the staging machine itself, or another VM).

## Features (WIP)

- Automated collection of data over mapped and mounted SMB network drives before uploading the data over a DTN
- Improves management and monitoring of instruments by making telemetry information remotely available
- Access and modify recipes from NEMO, with the ability to restrict visibility or make standardized recipes broadly available
- Historical analysis of tool data over time using a combination of telemetry and run log information to allow monitoring of various gauges (chamber base pressure, etc.)
- Processing of PTIQ (Oxford Instruments) information, as well as other streamed data formats
- Reads from the raw process logs to display other useful diagnostic information (faults/alarms during run, per-heater channel status, planned v. actual step time data, etc.) rendering charts and graphs in a desktop and mobile-friendly UI

Current TODO, besides achieving full cleanroom integration:

- Dockerize staging environment
- Browser rendered dataviz
- More user friendliness in terms of responsiveness (mobile UI improvements)
- Interface with reservations and NEMO db for more intelligent flagging

## Support

| Type         | Tool examples                                 | Reads                                                          |
| ------------ | --------------------------------------------- | -------------------------------------------------------------- |
| `heater_log` | Veeco Fiji / Savannah ALD                     | `<root>/Logfile/Heater Data/*.txt` (one file per run)          |
| `mvd`        | Cambridge Nanotech MVD                        | `<root>/log/data/<timestamp>_<recipe>/*_SUM.txt` + `*_DAT.txt` |
| `waferlog`   | Plasma-Therm VersaLine (e.g. hdpcvd)          | `<root>/WaferLog-Data/*.txt` (one file per wafer run)          |
| `cobra_job`  | Oxford Instruments PlasmaPro 100 Cobra (PTIQ) | `<root>/Databases-Data/Jobs.db` (SQLite, read-only)            |
| `eventlog`   | KLA-DSE (Trikon/SPTS "fxPLPXTMC")             | `<root>/EventLog-Data/CurrentEvents.csv`                       |

## Architecture

TODO

## Installation

```bash
python -m install NEMO-smart-lab
```

in `settings.py` add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    '...',
    'NEMO_smart_lab',
    '...'
]
```

## Usage

Then configure `SMART_LAB_TOOL_SOURCES` in `settings.py`. There is one entry per Tool, keyed by the
exact `Tool.name` that your NEMO instance already uses for it.

```python
SMART_LAB_TOOL_SOURCES = {
    "fiji1": {
        "kind": "heater_log",
        "root": r"\\fileserver\tool-logs\fiji1",
        "on_threshold_c": 35.0,   # a heater channel above this is considered "on"
    },
    "Ox-ALE": {
        "kind": "cobra_job",
        "root": r"\\fileserver\tool-logs\ox-ale",
    },
}
```


- `/smart_lab/` - dashboard of every configured tool, with a live status badge (e.g. "ON" for
  an active heater, "N fault(s)" for a KLA-DSE run with alarms, a Cobra job's own `Status`)
- `/smart_lab/tool/<slug>/` - most recent run's detail: recipe, timing, per-channel table, and
  a chart appropriate to the tool (a line chart of channel values over time for heater/wafer-log
  tools, a Gantt-style step timeline for Cobra jobs, an event scatter timeline for KLA-DSE).
  Add `?run=<run_id>` to view a specific past run instead (get `run_id` values from the history
  page below).
- `/smart_lab/tool/<slug>/history/` - paginated list of past runs; only the runs on the
  requested page are actually parsed, so this stays fast regardless of how many runs a tool has
  on disk (tens of thousands, for a long-lived SQLite-backed tool).

## Tests

To run the tests:

```bash
python run_tests.py
```

Tests are self-contained (each test generates its own small synthetic log files/database in a
temp directory) and don't depend on any real tool data being present.

## License

This project was built at the Stanford Nanofabrication Facility, and has been released under the MIT License. Please see [LICENSE](LICENSE) for more details.
