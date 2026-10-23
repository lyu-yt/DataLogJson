"""Schema validation plus domain rules shared by the desktop and CLI."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import csv
import math
import re
from uuid import UUID

from jsonschema import Draft7Validator, FormatChecker

from .io import LoggerError, encode, now, read_json, signature, uid

TASK_TYPES = ("water linear push", "water two-angle push")
COLUMNS = ("pos_x", "pos_y", "cur1", "cur2", "mvtyp", "mvangl", "frame_right",
           "frame_left", "lick", "sol", "states", "laser", "camfr", "laserCounter")
RESOURCE = Path(__file__).parent
SCHEMA_VERSION = "1.3.0"
SCHEMA_FILES = {SCHEMA_VERSION: "record.json"}


def schema_filename(version):
    if version not in SCHEMA_FILES:
        raise LoggerError(f"schema_version: unsupported version {version!r}")
    return SCHEMA_FILES[version]


def valid_task_type(value):
    return (isinstance(value, str) and 1 <= len(value.strip()) <= 120
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def envelope(kind):
    return {"schema_version": SCHEMA_VERSION, "record_type": kind, "record_id": uid(),
            "record_state": "draft", "created_at": now(), "operator": None,
            "finalization": {"finalized_at": None, "finalized_by": None}}


def new_task(source=None):
    r = read_json(RESOURCE / "templates" / "task_data.json")
    r.pop("template_version", None)
    r.update(envelope("data"))
    r["session_id"] = uid()
    r["files"] = []
    if source:
        p = Path(source)
        match = re.fullmatch(r"(?:mouse_?)?([0-9]{3})", p.parent.name, re.IGNORECASE)
        if match:
            r["mouse_id"] = match.group(1)
        try:
            # Explicit 2000-2099 convention, avoiding Python's two-digit pivot.
            r["session_date"] = datetime.strptime("20" + p.parent.parent.name, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
        # Registered files place the numeric session folder between file and day.
        if not match and p.parent.name.isdecimal() and re.fullmatch(r"[0-9]{6}", p.parent.parent.name):
            mouse = re.fullmatch(r"mouse_([0-9]{3})", p.parent.parent.parent.name, re.IGNORECASE)
            if mouse:
                r["mouse_id"] = mouse.group(1)
    return r


def inspect_csv(path, cancel=None):
    """Read all rows to verify the positional mapping; keep a bounded preview.

    No physical headers are fabricated: source_index is one-based and
    source_name is null. semantic_name comes from the supplied MATLAB reader.
    """
    before = signature(path)
    preview = []
    width = None
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.reader(f, delimiter="\t"), 1):
            if i % 4096 == 0 and cancel and cancel():
                from .io import Cancelled
                raise Cancelled("CSV inspection cancelled")
            if width is None:
                width = len(row)
            if width not in (13, 14) or len(row) != width:
                raise LoggerError(f"csv.row[{i}]: expected 13 or 14 consistent tab-separated columns")
            try:
                if not all(math.isfinite(float(x)) for x in row):
                    raise ValueError()
            except ValueError as exc:
                raise LoggerError(f"csv.row[{i}]: blank, nonnumeric, or nonfinite value; mapping needs review") from exc
            if len(preview) < 12:
                preview.append(row)
            count += 1
    if not count:
        raise LoggerError("csv: empty file")
    if before != signature(path):
        raise LoggerError("CSV changed during inspection; retry once acquisition has finished")
    return {"encoding": "utf-8-sig", "delimiter": "\t", "has_header": False,
            "header_row": None, "row_unit": "sample", "row_count": count,
            "mapping_source": "readRobotFn_260908.m positional mapping; units not supplied",
            "columns": [{"source_index": i + 1, "source_name": None,
                         "semantic_name": name, "description": f"MATLAB reader field {name}; physical unit unconfirmed",
                         "value_type": "number", "unit": None, "missing_values": []}
                        for i, name in enumerate(COLUMNS[:width])],
            "time_reference": {"kind": "sample_index", "first_sample_index": 0,
                               "sample_interval_s": None, "status": "unknown",
                               "clock_id": None, "zero_definition": "first stored sample; absolute time unknown"}}, preview


def validate(record, final=False, schema=None):
    errors = []
    try:
        encode(record)
    except (ValueError, TypeError) as exc:
        return [f"json: {exc}"]
    if schema is None:
        try:
            schema = read_json(RESOURCE / "schemas" / schema_filename(record.get("schema_version")))
        except LoggerError as exc:
            return [str(exc)]
    for e in Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(record):
        errors.append(f"{'.'.join(map(str, e.absolute_path)) or 'record'}: {e.message}")
    if errors:
        return errors
    for key in ("record_id", "session_id"):
        if key in record:
            try:
                if str(UUID(record[key])) != record[key]:
                    raise ValueError()
            except (ValueError, TypeError):
                errors.append(f"{key}: must be a full canonical UUID")
    is_final = final or record["record_state"] == "finalized"
    if is_final:
        for key in ("operator",):
            if not record.get(key) or not record[key].strip():
                errors.append(f"{key}: required")
        if record["record_type"] in ("data", "session"):
            for key in ("mouse_id", "session_date", "experiment_type"):
                if record.get(key) is None:
                    errors.append(f"{key}: required")
    if record["record_type"] == "data" and record.get("data_type") == "task":
        m = record["metadata"]
        if is_final:
            for field in ("mouse_weight_g", "training_day", "is_imaging_session"):
                if m.get(field) is None:
                    errors.append(f"metadata.{field}: required")
            if not valid_task_type(record.get("experiment_type")):
                errors.append("experiment_type: select a task type or enter custom text (1–120 characters)")
            if len(record["files"]) != 1:
                errors.append("files: exactly one registered task CSV is required")
        for i, f in enumerate(record["files"]):
            csvmeta = f.get("csv", {})
            expected = list(COLUMNS[:len(csvmeta.get("columns", []))])
            if (len(expected) not in (13, 14)
                or [c.get("semantic_name") for c in csvmeta.get("columns", [])] != expected
                or [c.get("source_index") for c in csvmeta.get("columns", [])] != list(range(1, len(expected) + 1))):
                errors.append(f"files.{i}.csv.columns: unresolved positional mapping")
            if csvmeta.get("delimiter") != "\t" or csvmeta.get("has_header") is not False or csvmeta.get("row_unit") != "sample":
                errors.append(f"files.{i}.csv: unsupported mapping")
    if record["record_type"] == "data":
        if record.get("data_type") == "expression":
            from .expression import record_errors
            errors.extend(record_errors(record, is_final))
        ids = [f.get("file_id") for f in record["files"]]
        if len(ids) != len(set(ids)):
            errors.append("files: duplicate file IDs")
        acq = record["acquisition"]
        if acq.get("started_at") and acq.get("ended_at"):
            if datetime.fromisoformat(acq["ended_at"]) < datetime.fromisoformat(acq["started_at"]):
                errors.append("acquisition.ended_at: precedes started_at")
    return errors


def require_valid(record, final=False, schema=None):
    errors = validate(record, final, schema)
    if errors:
        raise LoggerError("\n".join(errors))


def seal_record(record, author):
    r = deepcopy(record)
    r["record_state"] = "finalized"
    r["finalization"] = {"finalized_at": now(), "finalized_by": author}
    require_valid(r, True)
    return r


def session_for(data, checksum):
    s = envelope("session")
    s["record_id"] = data["session_id"]
    for key in ("mouse_id", "session_id", "session_date", "experiment_type", "operator"):
        s[key] = data[key]
    s["acquisition"] = deepcopy(data["acquisition"])
    s["training_day"] = data["metadata"].get("training_day")
    s["datasets"] = [{"record_id": data["record_id"], "sha256": checksum}]
    return seal_record(s, data["finalization"]["finalized_by"])
