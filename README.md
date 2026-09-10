# NEMO Smart Lab

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![License](https://img.shields.io/badge/License-AGPL_v3%2B-blue)](https://github.com/SNF-Root/NEMO-Smart-Lab/blob/main/LICENSE)

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
pip install "NEMO-smart-lab[NEMO]"   # or NEMO-smart-lab[NEMO-CE] for NEMO Community Edition
```

in `settings.py` add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    '...',
    'NEMO_smart_lab',
    '...'
]
```

Run `python manage.py migrate` to create this plugin's tables, then configure your tools from
the Django admin under **Tool Data > Smart Lab tools**. There is one row per Tool, matching the exact
`Tool.name` NEMO already uses for it. 

Then visit `/smart_lab/`, or the new "Tool Data" tile on the landing page.

### Optional: pull tool data down from a remote SSH fileserver

If a tool's raw data lives on a remote host reachable over SSH, rather
than a network share you can mount directly, `sync_remote_data` will mirror it down into that
tool's `local_root`.

First add a **Remote sync endpoint** in the admin (Tool Data > Remote sync endpoints) describing
the remote host:

| Field               | Example (Oak)              |
| ------------------- | -------------------------- |
| `name`              | `Oak`                      |
| `host`              | `dtn.oak.stanford.edu`     |
| `username`          | account name               |
| `ssh_key_path`      | `/etc/nemo/oak_id_ed25519` |
| `base_path`         | `/oak/stanford/orgs/nano`  |
| `extra_ssh_options` | optional                   |

Then, on each `SmartLabTool` that should sync from it, set **Remote sync > sync_endpoint** to
that endpoint, and optionally **remote_subdir** if the directory name on the remote host doesn't
match the tool's own name exactly (e.g. the tool lives at
`/oak/stanford/orgs/nano/fiji-1-data` but is configured here as `"fiji1"`).

```bash
python manage.py sync_remote_data              # sync every tool with a sync_endpoint set
python manage.py sync_remote_data fiji1        # just one
python manage.py sync_remote_data --dry-run    # rsync -n, if rsync is installed
```

Run it on a schedule (cron/Task Scheduler) to keep `local_root` up to date.

### Optional: seed a demo/dev database

```bash
python manage.py seed_smart_lab_demo
```

Creates/renames a `Tool` for every `SmartLabTool` row (at its `real_id`, if set) and the
"Tool Data" landing page tile. By default it also deletes every _other_ `Tool` (and whatever
cascades from it - reservations, usage events, etc.) so a demo database seeded from NEMO's
splash-pad fixture ends up with just the Smart Lab tools; pass `--keep-other-tools` to skip that.

## Usage

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

This project was built at the Stanford Nanofabrication Facility, and has been released under the GNU Affero General Public License v3.0. Please see [LICENSE.md](LICENSE.md) for more details.
