"""Readable storage labels and versioned bundle manifests, independent of IDs."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re

from .io import LoggerError, digest, loads, read_json, resolve
from .models import valid_task_type

METADATA_DIRECTORY = "metadata-json"
CSV_DIRECTORY = "robot-csv"


def metadata_directory(archive):
    """Prefer the requested layout; continue reading an untouched legacy archive.

    No directory is moved or created by choosing a path. In particular, opening
    the app must not silently fork an existing metadata repository.
    """
    archive = Path(archive)
    current = archive / METADATA_DIRECTORY
    legacy = archive / "metadata"
    if not current.exists() and (legacy / "logger_repository.json").is_file():
        return legacy
    return current


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:48] or "record"


def mouse_folder(mouse_id):
    if not re.fullmatch(r"[0-9]{3}", mouse_id or ""):
        raise LoggerError("mouse_id: enter exactly three digits")
    return f"mouse_{mouse_id}"


def day_folder(session_date):
    value = date.fromisoformat(session_date)
    if not 2000 <= value.year <= 2099:
        raise LoggerError("Storage dates must be between 2000 and 2099 for YYMMDD folders")
    return value.strftime("%y%m%d")


def csv_session_directory(record, layout):
    return f"{CSV_DIRECTORY}/{mouse_folder(record['mouse_id'])}/{day_folder(record['session_date'])}/{layout['number']}"


def context(record):
    if not re.fullmatch(r"[0-9]{3}", record.get("mouse_id") or ""):
        raise LoggerError("mouse_id: three digits are required for a storage label")
    if date.fromisoformat(record.get("session_date") or "").isoformat() != record["session_date"]:
        raise LoggerError("session_date: use YYYY-MM-DD")
    if not valid_task_type(record.get("experiment_type")):
        raise LoggerError("experiment_type: nonblank task text of at most 120 characters is required")
    return {k: record[k] for k in ("mouse_id", "session_date", "experiment_type", "session_id")}


def empty_labels():
    return {"version": 1, "counters": {}, "sessions": {}}


def reserve_label(record, labels, archive, records):
    """Mutate a caller-owned labels dictionary; caller serializes reservation."""
    ctx = context(record)
    prior = labels["sessions"].get(ctx["session_id"])
    same_context = prior and all(prior.get(k) == v for k, v in ctx.items())
    folder = mouse_folder(ctx["mouse_id"])
    day = day_folder(ctx["session_date"])
    if same_context and prior.get("directory_name", "").startswith(f"{folder}/{day}/"):
        return prior
    legacy_key = f"{ctx['mouse_id']}_{ctx['session_date']}"
    key = f"mouse{legacy_key}"
    highest = max(labels["counters"].get(key, 0), labels["counters"].get(legacy_key, 0))
    for prefix in (key, legacy_key):
        for category in (CSV_DIRECTORY, "csv"):
            for existing in (Path(archive) / category / prefix).glob("*"):
                if existing.name.isdecimal():
                    highest = max(highest, int(existing.name))
        for existing in Path(records).glob(prefix + "_*"):
            ending = existing.name.rsplit("_", 1)[-1]
            if ending.isdecimal():
                highest = max(highest, int(ending))
    for category in (CSV_DIRECTORY, "csv"):
        for path in (Path(archive) / category / folder / day).glob("*"):
            if path.name.isdecimal():
                highest = max(highest, int(path.name))
    for path in (Path(records) / folder / day).glob("*"):
        ending = path.name.rsplit("_", 1)[-1]
        if ending.isdecimal():
            highest = max(highest, int(ending))
    number = prior["number"] if same_context else highest + 1
    label = {**ctx, "number": number,
             "directory_name": f"{folder}/{day}/{slug(ctx['experiment_type'])}_{number}"}
    labels["counters"][key] = max(highest, number)
    labels["sessions"][ctx["session_id"]] = label
    return label


def role_filename(record):
    if record.get("data_type") == "expression":
        return "expression.json"
    return {"data": "task.json", "session": "session.json", "amendment": "amendment.json",
            "analysis_run": "analysis_run.json"}[record["record_type"]]


def bundle_entries(folder):
    """Return record paths and seals for either legacy or readable bundles.

    All identities are checked against content, never derived from filenames.
    Each listed filename must be a direct child, and no scientific JSON may be
    silently omitted from a combined manifest.
    """
    folder = Path(folder)
    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("version") != 2 or not isinstance(manifest.get("records"), list) or not manifest["records"]:
            raise LoggerError("Unsupported or empty bundle manifest")
        entries, ids, names = [], set(), set()
        for seal in manifest["records"]:
            name = seal["filename"]
            p = resolve(folder, name)
            if p.parent != folder.resolve() or not name.endswith(".json") or name == "manifest.json":
                raise LoggerError("Manifest record filename must be a direct JSON child")
            if name in names or seal["record_id"] in ids:
                raise LoggerError("Duplicate manifest record or filename")
            names.add(name)
            ids.add(seal["record_id"])
            entries.append((p, seal))
        actual = {p.name for p in folder.glob("*.json")} - {"manifest.json"}
        if actual != names:
            raise LoggerError("Manifest does not match the bundle's JSON files")
        return entries
    return [(p, read_json(p.with_suffix(".seal.json")))
            for p in sorted(folder.glob("*.json")) if not p.name.endswith(".seal.json")]


def check_record(path, seal, schema_sha256):
    raw = Path(path).read_bytes()
    record = loads(raw)
    if record.get("record_state") != "finalized":
        raise LoggerError("Published record is not finalized")
    if digest(raw) != seal["sha256"] or record["record_id"] != seal["record_id"]:
        raise LoggerError("checksum mismatch")
    if seal["schema_sha256"] != schema_sha256:
        raise LoggerError("schema checksum mismatch")
    return record
