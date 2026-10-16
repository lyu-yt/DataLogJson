# DataLogJson

A local Python desktop logger for mouse task CSVs, session metadata, file
locations, and mouse-colony profiles. Supports Windows or Linux with local or
mounted storage.

## Install and run

Install Anaconda or Miniconda and Git, then run from this checkout:

```sh
conda env create -f environment.yml
conda activate datalogjson
python -m experiment_logger
```

For an existing environment, use `conda env update -n datalogjson -f environment.yml`.
All dependencies belong in the dedicated `datalogjson` environment, not Conda base.
Launchers are provided as `launch.ps1` and `launch.sh`.

## Repository contents and privacy

This repository contains application source, a JSON Schema and empty draft
template, environment/build settings, launchers, tests, and example-generator code.
It contains no acquisition data, colony spreadsheets, finalized metadata records,
local configuration, credentials, screenshots, or analysis outputs.

The root `.gitignore` uses an allowlist: everything is ignored unless explicitly
approved as source/configuration. Test workbooks, CSVs, and records are generated
from synthetic values in temporary directories. Example generators create files
locally; generated artifacts are excluded from Git. Do not use `git add -f` to
override these protections for experimental files.

The app creates a separate metadata Git repository inside the selected archive
root. This source repository does not configure a remote for that metadata repo.
Machine paths stay in private `.logger-local/config.json`.

## Log a session

1. In **Settings**, select an archive root and enter an operator name. Use the
   mounted path appropriate to your operating system, then initialize storage.
2. Drop a completed CSV onto **Log session**. Mouse ID and date can be inferred
   from `YYMMDD/007/file.csv` and reviewed before finalization.
3. Enter weight in grams, task type, training day, imaging status, and optional
   notes. Choose either push-task preset or enter a custom task type.
4. Save a draft, validate, and finalize after acquisition finishes. Finalization
   shows source and destination for confirmation. Copy is the default; Move
   removes the source only after verified copying and a successful metadata commit.

New files use this layout under the configured archive root:

```text
metadata-json/records/mouse_007/
  mouse_info.json
  YYMMDD/task_name_1/
    task.json
    session.json
    manifest.json
robot-csv/mouse_007/YYMMDD/1/file.csv
```

Mouse IDs are three-digit strings; folder years mean 2000-2099. JSON dates use
`YYYY-MM-DD`. Training day is an operator-assigned number, separate from calendar
date. Acquisition timezone is America/New_York; audit timestamps have explicit UTC
offsets. Internal UUID identities do not depend on paths.

## Browse, profiles, and locations

**Browse / details** filters by mouse/date/training day/task/record type. Separate
columns distinguish `data` and `session`: each acquisition has a task record with
its CSV registration and a session record linking that task. Both display training
day. Details include read-only metadata, CSV preview, and a bounded numeric plot.
Corrections create amendments instead of editing finalized records.

**Mouse colony** opens an editable profile for a local ID. Choose an XLSX export
and search matching IDs. All matching rows are presented without automatic
selection. Select and confirm a row before importing; prefixed and repeated IDs
require the operator's decision. Local and exact colony IDs remain separate.
Supported headers include Mouse ID, Strain, Sex, DOB, Genotype, Cage ID, Use,
Labels, Notes, and SystemId. DOB accepts Excel dates, ISO dates, or US
month/day/year text. Add manual notes and multiple surgeries with date, details,
and optional surgeon. Imports retain source provenance and do not infer surgeries
from notes. Save explicitly; revisions and Git history preserve profile edits.

**Locations** resolves stable file IDs through a mutable catalog and per-machine
roots. Locate searches only selected roots and fully verifies SHA-256 before
relinking. Whole-root moves need a configuration change; individual file moves
change the catalog instead of frozen metadata. Commit catalog changes explicitly.

The current scope is one operator. Native Linux behavior, SMB locking, and NAS
power-loss recovery require deployment testing before concurrent use.

## CSV and JSON conventions

The implemented push-task format is headerless, tab-separated numeric data with
13 or 14 consistent columns. Positional meanings follow the MATLAB reader mapping
documented in the source. Inspection never modifies original files.

No timestamp column is available in this mapping. Sampling interval, physical
units, and exact acquisition start/end stay unknown where not supplied. The UI
does not invent trial outcomes, reward rates, or elapsed-time plots. Confirm timing
and scientific definitions before adding performance analyses.

Strict UTF-8 JSON rejects duplicate keys and NaN/Infinity. Unknown values are null.
The app ships one current draft-07 schema and empty task template. Finalized
records retain `schema_version`, exact byte checksums, and the original archived
schema. Current version: 1.2.0. Historical records are not rewritten. Editable
profiles validate against the schema's `definitions.mouse_profile`.

## CLI and API

```sh
python -m experiment_logger.cli --help
python -m experiment_logger.cli browse
python -m experiment_logger.cli mouse-profile 007
python -m experiment_logger.cli colony-search /path/to/colony.xlsx 007
python -m experiment_logger.cli recover
```

Use `--config /path/to/private/config.json` before the subcommand for another local
configuration. GUI and CLI share validation, draft revisions, registration,
publication, amendment, profile, and location services.

The optional trusted Python analysis wrapper runs a clean Git code snapshot in a
new workspace, recording input hashes, parameters, environment, outputs, and logs.
Viewing a record never executes analysis commands. MATLAB scientific methods are
not rewritten by this application.

## Recovery and backups

Finalized locally, Git committed, and backed up are separate statuses. Commit
failures preserve local records and recoverable pending operations. **Browse ->
Recover** retries prepared publication/profile saves and Git commits. Reopen a
profile after recovery. Recovery refuses conflicting bytes and never automatically
deletes source files. Never remove a writer lock while any logger process is active.

Back up the full archive root, including metadata `.git`, records, schemas,
catalog history, hidden runtime journals, CSVs, and analysis workspaces. Back up
private local settings/drafts separately. Test restoration to a new location and
fully verify hashes. Git alone is not a raw-data backup. Checksums and Git do not
provide WORM retention or independently prove creation time.

## Tests and generated examples

```sh
conda activate datalogjson
python -m pytest -q
python examples/create_demo.py --destination /absolute/path/to/new-demo-folder
```

Tests generate synthetic inputs and do not require experimental data. The demo
refuses an existing destination. For a MATLAB decoding check, set MATLAB's
`LOGGER_JSON_FIXTURE` environment variable to the generated synthetic `task.json`
path and run `examples/matlab_json_smoke.m`.
