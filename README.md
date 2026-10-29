# DataLogJson

A local Python desktop app for mouse task logging, in-vivo imaging, expression checks, mouse-colony profiles, and
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

- **Behavior:** drop a completed task CSV; infer/review mouse ID and date; enter
  weight, preset/custom task type, training day, imaging-session flag, and notes.
  Save/reopen drafts, validate, and finalize with verified copying or an explicitly
  confirmed move. Moves use direct renaming where supported, or verified copies
  followed by source removal after the metadata Git commit.
- **Mouse colony:** create/reopen a mouse profile; import strain, sex, DOB,
  genotype, cage, labels, status, and notes from an XLSX colony sheet. Select and
  confirm the correct row when IDs repeat or have letter prefixes. Add manual
  notes and multiple surgeries with dates, details, and optional surgeon. Save
  editable profiles with revision checks, import provenance, and Git history.
- **Expression:** drop a mouse folder (or choose a mouse from a date folder).
  Detect left red/green, right, and other TIFFs; review checkboxes and editable
  paths, including multiple files per channel. Add optional notes, save a draft,
  and finalize verified archive copies. Enable **Move sources** for direct moves
  or verified copy/removal after the metadata Git commit; otherwise sources are
  retained. Filename collisions get numbered suffixes. TIFF stacks are not decoded.
- **Imaging:** drop a mouse folder to detect one left/right TIFF each. Review
  red/green presence, enter optional free-text laser power per side, and link a
  logged robot CSV. A unique same-mouse/date match is preselected; multiple matches
  require a choice. Direct moves avoid copying when supported by the filesystem.
  Cross-filesystem transfers use verified copies, then remove sources after the
  metadata Git commit. Checksum work runs in the background; Recover resumes
  interrupted transfers. Browse can open the linked behavior record.
- **Browse / details:** filter by mouse, date, training day, experiment, data type, and record type.
  Separate `data` and `session` rows show training day. Inspect read-only JSON,
  CSV previews, numeric plots, expression file locations, amendments, and analysis history.
- **Locations:** verify full SHA-256 hashes, find missing files within selected
  roots, and relink verified copies. Rebuild/commit catalogs and rebuild indexes.
  Change a root's configured path when moving an entire archive.
- **History and recovery:** finalized records retain UUIDs, schema versions, and
  checksum manifests. Corrections create amendments; frozen JSON stays unchanged.
  Recover interrupted saves/publications and failed metadata Git commits.
- **Transfer progress:** show the current file/step, elapsed time, and an estimated
  time remaining based on measured throughput. Estimates include file verification,
  adjust when the move method changes, and recalculate after storage stalls.
  Metadata/Git work shows elapsed time until the operation finishes.
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
  YYMMDD/expression_check_2/
    expression.json
    session.json
    manifest.json
robot-csv/mouse_007/YYMMDD/1/file.csv
expression/mouse_007/YYMMDD/left/green/green_001.tif
expression/mouse_007/YYMMDD/left/red/red_001.tif
expression/mouse_007/YYMMDD/right/right_001.tif
expression/mouse_007/YYMMDD/other/extra_001.tif
imaging/mouse_007/YYMMDD/left/left_001.tif
imaging/mouse_007/YYMMDD/right/right_001.tif
```

Mouse IDs stay three-digit strings. JSON dates use `YYYY-MM-DD`; training day is a
separate operator-assigned number. Acquisition timezone: America/New_York.

This GitHub repository contains only code, environment/build settings, schemas,
empty templates, and synthetic test/example generators. An allowlist excludes
experimental files, workbooks, finalized records, credentials, local settings, and
outputs. Machine settings/drafts stay in `.logger-local/`; the archive's separate
metadata Git repository stays local. Back up the complete archive and local state;
Git alone is not a data backup, and backups are not automated.

Schema 1.4.0 adds imaging records and fixed behavior links. Older finalized records and their schemas
retain their exact bytes; editable colony profiles update only on explicit save.
After an interrupted TIFF copy, retry Finalize to reuse verified copies. The
private `.logger-local/expression-transfers/` journals preserve planned paths;
include them in local-state backups. Browse Recover handles pending metadata/Git publication.
Include `.logger-local/imaging-transfers/` and `.logger-local/file-transfers/` in
those backups too. A direct move
changes the path immediately; if hashing or publication is interrupted, the file
stays in the archive and Recover resumes registration. Copy-fallback sources stay
until their copies are verified and metadata is committed. No transfer overwrites
an existing file. A Desktop-to-NAS transfer always needs to transfer the bytes;
direct moves still read the full movie once to calculate SHA-256.

## Checks and limits

```sh
python -m pytest -q
python examples/create_demo.py --destination /absolute/path/to/new-demo-folder
```

Tests generate temporary synthetic data. For MATLAB JSON decoding, set
`LOGGER_JSON_FIXTURE` in MATLAB to a generated synthetic task, expression, or imaging JSON, then run
`examples/matlab_json_smoke.m`.

Current scope: one operator and headerless, tab-separated push-task CSVs with
13-14 numeric columns. Sampling rate/units remain unknown where not supplied;
plots show sample index, not inferred timing or task performance. Expression
and imaging logging register TIFF files; image analysis, synchronization, and MATLAB
analysis execution are future extensions. Linux/NAS locking and recovery need deployment-specific checks.

## License

Project source is licensed under the [MIT License](LICENSE). Dependencies retain
their own licenses.
