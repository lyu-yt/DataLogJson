# DataLogJson

A local Python desktop app for mouse task logging, mouse-colony profiles, and
tracking data files on local or mounted storage. Uses plain JSON and local Git
history; no server or database is required.

## Quick start

Install Anaconda/Miniconda and Git, then run from this checkout:

```sh
conda env create -f environment.yml
conda activate datalogjson
python -m experiment_logger
```

In **Settings**, choose the archive folder and enter your operator name. Update an
existing environment with `conda env update -n datalogjson -f environment.yml`. Keep packages
in `datalogjson`, not Conda base. Launchers: `launch.ps1` and `launch.sh`.

## Features

- **Log session:** drop a completed task CSV; infer/review mouse ID and date; enter
  weight, preset/custom task type, training day, imaging-session flag, and notes.
  Save/reopen drafts, validate, and finalize with verified copying or an explicitly
  confirmed move. File checks/transfers show progress and support cancellation.
- **Mouse colony:** create/reopen a mouse profile; import strain, sex, DOB,
  genotype, cage, labels, status, and notes from an XLSX colony sheet. Select and
  confirm the correct row when IDs repeat or have letter prefixes. Add manual
  notes and multiple surgeries with dates, details, and optional surgeon. Save
  editable profiles with revision checks, import provenance, and Git history.
- **Browse / details:** filter by mouse, date, training day, task, and record type.
  Separate `data` and `session` rows show training day. Inspect read-only JSON,
  CSV previews, numeric plots, amendments, and analysis history.
- **Locations:** verify full SHA-256 hashes, find missing files within selected
  roots, and relink verified copies. Rebuild/commit catalogs and rebuild indexes.
  Change a root's configured path when moving an entire archive.
- **History and recovery:** finalized records retain UUIDs, schema versions, and
  checksum manifests. Corrections create amendments; frozen JSON stays unchanged.
  Recover interrupted saves/publications and failed metadata Git commits.
- **CLI and shared API:** access logging, profiles, file verification/relinking,
  recovery, and previewed layout migration. The optional trusted Python analysis
  runner records code snapshots, inputs, parameters, environment, outputs, and logs;
  reruns get separate workspaces. Run `python -m experiment_logger.cli --help`.

## Storage and privacy

Under your configured archive root:

```text
metadata-json/records/mouse_007/
  mouse_info.json
  YYMMDD/task_name_1/
    task.json
    session.json
    manifest.json
robot-csv/mouse_007/YYMMDD/1/file.csv
```

Mouse IDs stay three-digit strings. JSON dates use `YYYY-MM-DD`; training day is a
separate operator-assigned number. Acquisition timezone: America/New_York.

This GitHub repository contains only code, environment/build settings, schemas,
empty templates, and synthetic test/example generators. An allowlist excludes
experimental files, workbooks, finalized records, credentials, local settings, and
outputs. Machine settings/drafts stay in `.logger-local/`; the archive's separate
metadata Git repository stays local. Back up the complete archive and local state;
Git alone is not a data backup, and backups are not automated.

## Checks and limits

```sh
python -m pytest -q
python examples/create_demo.py --destination /absolute/path/to/new-demo-folder
```

Tests generate temporary synthetic data. For MATLAB JSON decoding, set
`LOGGER_JSON_FIXTURE` in MATLAB to a generated synthetic `task.json`, then run
`examples/matlab_json_smoke.m`.

Current scope: one operator and headerless, tab-separated push-task CSVs with
13-14 numeric columns. Sampling rate/units remain unknown where not supplied;
plots show sample index, not inferred timing or task performance. Imaging is a
session flag; imaging-data workflows and MATLAB analysis execution are future
extensions. Linux/NAS locking and recovery need deployment-specific checks.
