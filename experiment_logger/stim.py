"""Stimulation drafts, file routing, and plans for the shared transfer engine."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import math
import os
import re

from .expression import inspect_tiff
from .io import (Cancelled, LoggerError, atomic_json, digest, encode, lock, now,
                 read_json, resolve, signature, uid)
from .layout import day_folder, mouse_folder
from .models import envelope, new_task, require_valid
from .stim_parameters import extract_stim_parameters, mouse_number
from .transfers import finish_transfers

BUCKETS = ("imaging", "stimPara", "other")


def new_stim():
    record = {**envelope("data"), "data_type": "stim", "experiment_type": "stimulation",
        "session_id": uid(), "mouse_id": None, "session_date": None,
        "acquisition": deepcopy(new_task()["acquisition"]), "files": [],
        "related_records": [], "notes": None,
        "metadata": {"extraction": None, "parameter_overrides": [], "reviewed": False,
                     "behavior_session": False, "behavior": None}}
    return {"revision": 0, "record": record, "inputs": [], "parameter_choices": {
        "parameters_csv": None, "parameters_mat": None}, "extraction_signatures": {},
        "inferred_mouse_id": None, "inferred_date": None,
        "preferred_behavior_csv_sha256": None, "pending_behavior_source": None}


def refresh_behavior(store, draft):
    """Select a unique same-mouse/date logged CSV; ambiguity stays explicit."""
    d = deepcopy(draft)
    r, m = d["record"], d["record"]["metadata"]
    choices = []
    if m["behavior_session"] and r["mouse_id"] and r["session_date"]:
        choices = store.behavior_candidates(r["mouse_id"], r["session_date"])
        preferred = d.get("preferred_behavior_csv_sha256")
        if preferred:
            choices = [c for c in choices if c["reference"]["csv_sha256"] == preferred]
    references = [c["reference"] for c in choices]
    if m["behavior"] not in references:
        m["behavior"] = references[0] if len(references) == 1 else None
    r["related_records"] = ([{k: m["behavior"][k] for k in ("record_id", "sha256")}]
                            if m["behavior"] else [])
    d["behavior_choices"] = choices
    m["reviewed"] = False
    return d


def choose_behavior_csv(store, draft, path, *, progress=None, cancel=None):
    """Match an explicitly selected CSV by full content as well as mouse/date."""
    from .io import hash_file
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".csv":
        raise LoggerError("Choose a robot CSV")
    hashes = hash_file(source, progress, cancel)
    d = deepcopy(draft)
    d["preferred_behavior_csv_sha256"] = hashes["sha256"]
    d["pending_behavior_source"] = str(source)
    d["record"]["metadata"]["behavior_session"] = True
    return refresh_behavior(store, d)


def _role(path):
    if path.name.lower() == "stim_parameters.csv":
        return "parameters_csv", "csv", "text/csv"
    if re.fullmatch(r"stimpara.*\.mat", path.name, re.I):
        return "parameters_mat", "mat", "application/x-matlab-data"
    if path.suffix.lower() in (".tif", ".tiff"):
        return "imaging_tiff", "tiff", "image/tiff"
    if path.suffix.lower() == ".dat":
        return "stim_recording", "dat", "application/octet-stream"
    return "other", path.suffix.lower().lstrip(".") or "binary", "application/octet-stream"


def add_inputs(draft, paths, area, *, cancel=None):
    """Expand only explicitly dropped files/folders; never follow directory links."""
    if area not in ("imaging", "stimpara"):
        raise LoggerError("Choose the Imaging or Stim parameters drop area")
    d = deepcopy(draft)
    seen = {os.path.normcase(i["source"]) for i in d["inputs"]}
    for supplied in paths:
        root = Path(supplied).expanduser()
        if not root.is_absolute() or root.is_symlink() or root.is_junction() or not root.exists():
            raise LoggerError("Drop existing absolute file/folder paths; links are not followed")
        root = root.resolve()
        if root.is_dir():
            candidates = []
            for parent, directories, filenames in os.walk(root, followlinks=False):
                if cancel and cancel():
                    raise Cancelled("File discovery cancelled")
                directories[:] = sorted(n for n in directories if not (Path(parent)/n).is_symlink()
                                        and not (Path(parent)/n).is_junction())
                candidates.extend(Path(parent)/n for n in sorted(filenames))
        else:
            candidates = [root]
        for path in candidates:
            if cancel and cancel():
                raise Cancelled("File discovery cancelled")
            if path.is_symlink() or not path.is_file():
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            role, format_, media_type = _role(path)
            bucket = "imaging" if area == "imaging" else "stimPara" if role.startswith("parameters_") else "other"
            # A mask TIFF dropped in area 2 is an ancillary file, not a movie.
            if bucket == "other" and role == "imaging_tiff":
                role = "other"
            relative = path.relative_to(root).as_posix() if root.is_dir() else path.name
            if bucket == "stimPara":
                relative = path.name
            d["inputs"].append(dict(source=str(path), relative_name=relative, bucket=bucket,
                role=role, format=format_, media_type=media_type, file_id=uid()))
    d["record"]["metadata"]["reviewed"] = False
    return refresh_parameters(d, cancel=cancel)


def folder_identity(inputs):
    mice, dates = set(), set()
    for item in inputs:
        parents = list(Path(item["source"]).parents)[:8]
        for parent in parents:
            if mouse_number(parent.name):
                mice.add(mouse_number(parent.name))
            if re.fullmatch(r"[0-9]{6}", parent.name):
                try:
                    dates.add(datetime.strptime("20"+parent.name, "%Y%m%d").date().isoformat())
                except ValueError:
                    pass
    return (next(iter(mice)) if len(mice) == 1 else None,
            next(iter(dates)) if len(dates) == 1 else None)


def refresh_parameters(draft, *, cancel=None):
    d = deepcopy(draft)
    selected = {}
    for role in ("parameters_csv", "parameters_mat"):
        items = [i for i in d["inputs"] if i["role"] == role and i["bucket"] == "stimPara"]
        ids = [i["file_id"] for i in items]
        choice = d["parameter_choices"].get(role)
        if choice not in ids:
            choice = ids[0] if len(ids) == 1 else None
        d["parameter_choices"][role] = choice
        selected[role] = next((i for i in items if i["file_id"] == choice), None)
    extracted = extract_stim_parameters(
        selected["parameters_csv"]["source"] if selected["parameters_csv"] else None,
        selected["parameters_mat"]["source"] if selected["parameters_mat"] else None, cancel=cancel)
    report = extracted["report"]
    for source in report["sources"]:
        source["file_id"] = selected[source["role"]]["file_id"]
    inferred_mouse, inferred_date = folder_identity(d["inputs"])
    candidates = {i["mouse_id"] for i in report["identity_candidates"] if i["mouse_id"]}
    if len(candidates) > 1:
        inferred_mouse = None
    elif candidates:
        parameter_mouse = next(iter(candidates))
        if inferred_mouse and inferred_mouse != parameter_mouse:
            report["warnings"].append(f"Folder mouse {inferred_mouse} differs from parameter mouse {parameter_mouse}; review the identity.")
        inferred_mouse = parameter_mouse
    r = d["record"]
    for field, inferred, previous in (("mouse_id", inferred_mouse, "inferred_mouse_id"),
                                      ("session_date", inferred_date, "inferred_date")):
        if r[field] is None or r[field] == d[previous]:
            r[field] = inferred
        d[previous] = inferred
    if r["metadata"]["extraction"] != report:
        r["metadata"]["parameter_overrides"] = []
    r["metadata"].update(extraction=report, reviewed=False)
    d["extraction_signatures"] = extracted["signatures"]
    return d


def record_errors(record, final):
    m, errors = record["metadata"], []
    report = m["extraction"]
    ref = m["behavior"]
    if m["behavior_session"]:
        if final and not ref:
            errors.append("metadata.behavior: choose a logged robot CSV for this mouse/date")
        if ref and record["related_records"] != [{k: ref[k] for k in ("record_id", "sha256")}]:
            errors.append("metadata.behavior: inconsistent linked behavior identity")
    elif ref or record["related_records"]:
        errors.append("metadata.behavior: clear the link when Behavior session is unchecked")
    if final:
        if not m["reviewed"]:
            errors.append("metadata.reviewed: review mouse/date and extracted settings")
        roles = {f["role"] for f in record["files"]}
        if not {"imaging_tiff", "stim_recording", "parameters_csv", "parameters_mat"}.issubset(roles):
            errors.append("files: need at least one imaging TIFF, one DAT, and the parameter CSV and MAT")
        registered = {f["file_id"]: f for f in record["files"]}
        if report is None or {s["role"] for s in report["sources"]} != {"parameters_csv", "parameters_mat"}:
            errors.append("metadata.extraction: select and read one parameter CSV and MAT")
        elif any(registered.get(s["file_id"], {}).get("role") != s["role"] for s in report["sources"]):
            errors.append("metadata.extraction.sources: unresolved parameter file identity")
    names = [p["name"] for p in report["parameters"]] if report else []
    if len(set(names)) != len(names):
        errors.append("metadata.extraction.parameters: duplicate parameter name")
    overrides = m["parameter_overrides"]
    if len({p["name"] for p in overrides}) != len(overrides) or any(p["name"] not in names for p in overrides):
        errors.append("metadata.parameter_overrides: duplicate or unknown parameter")
    effective = {p["name"]: p["value"] for p in report["parameters"]} if report else {}
    effective.update({p["name"]: p["value"] for p in overrides})
    numeric = {"laser_power_mw", "instrument_power_percent", "spiral_size", "pulse_duration_s",
               "pause_duration_s", "pulse_count", "spiral_count", "pattern_count",
               "configured_stim_locations", "sequence_repeats", "initial_padding", "pulse_parameter_sets"}
    def numbers(value):
        if isinstance(value, list):
            return [n for v in value for n in numbers(v)]
        return [value]
    for name in numeric & effective.keys():
        value = effective[name]
        if value is None:
            continue
        flat = numbers(value)
        valid = flat and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in flat)
        if valid and name in ("pulse_duration_s", "pulse_count", "pattern_count", "pulse_parameter_sets"):
            valid = all(v > 0 for v in flat)
        if valid and name.endswith(("count", "repeats", "padding", "sets", "locations")):
            valid = all(v == int(v) for v in flat)
        if valid and name == "instrument_power_percent":
            valid = all(v <= 100 for v in flat)
        if not valid:
            errors.append(f"metadata.parameters.{name}: invalid numeric setting")
    return errors


def _prepare(store, draft):
    r = draft["record"]
    require_valid(r)
    if r.get("data_type") != "stim" or r["record_state"] != "draft":
        raise LoggerError("Only stimulation drafts can be moved")
    for key in ("mouse_id", "session_date", "operator"):
        if not r.get(key) or not str(r[key]).strip():
            raise LoggerError(f"{key}: required")
    mouse_folder(r["mouse_id"])
    day_folder(r["session_date"])
    # Preflight registration references before moving any file.
    probe = deepcopy(r)
    probe["files"] = [{"file_id": i["file_id"], "role": i["role"]} for i in draft["inputs"]]
    errors = record_errors(probe, True)
    if errors:
        raise LoggerError("\n".join(errors))
    from .imaging import _validate_behavior
    _validate_behavior(store, r)
    inputs = draft["inputs"]
    if len({os.path.normcase(i["source"]) for i in inputs}) != len(inputs) or len({i["file_id"] for i in inputs}) != len(inputs):
        raise LoggerError("Duplicate source path or file identity")
    saved = store._path(r["record_id"], store.local / "drafts")
    if saved.exists() and read_json(saved)["revision"] != draft["revision"]:
        raise LoggerError("Stale draft; reload before moving")
    report = r["metadata"]["extraction"]
    if any(draft["parameter_choices"].get(s["role"]) != s["file_id"] for s in report["sources"]):
        raise LoggerError("Parameter choices changed; refresh parameters and review again")
    fingerprint = digest(encode({"record": r, "inputs": inputs, "choices": draft["parameter_choices"],
                                 "signatures": draft["extraction_signatures"]}))
    journal = store._path(r["record_id"], store.local / "stim-transfers")
    if journal.exists():
        prior = read_json(journal)
        if prior["fingerprint"] == fingerprint:
            if prior["phase"] == "complete":
                raise LoggerError("Duplicate finalization; stimulation already logged")
            return journal, prior
        if prior["authorized"]:
            raise LoggerError("Transfer choices changed after starting; use Recover")
    index = store.index()
    if r["record_id"] in index or r["session_id"] in index:
        raise LoggerError("Duplicate finalized record or session")
    if any(r["record_id"] in op["record_ids"] for op in store.pending()):
        raise LoggerError("Publication pending; use Recover")
    report = r["metadata"]["extraction"]
    for source in report["sources"]:
        item = next(i for i in inputs if i["file_id"] == source["file_id"])
        if list(signature(item["source"])) != draft["extraction_signatures"].get(item["source"]):
            raise LoggerError("Parameter file changed since extraction; refresh parameters and review again")
    for item in inputs:
        if item["bucket"] not in BUCKETS or not Path(item["source"]).is_absolute():
            raise LoggerError("Invalid file destination or source path")
        # Validate each relative name independently, so it cannot leave its bucket.
        resolve(Path(store.roots["archive"]), item["relative_name"])
        if item["role"] == "imaging_tiff":
            inspect_tiff(item["source"])
    layout = store._reserve_layout(r)
    base = f"stim/{mouse_folder(r['mouse_id'])}/{day_folder(r['session_date'])}/{layout['number']}"
    names, items = set(), []
    for item in inputs:
        relative = f"{base}/{item['bucket']}/{item['relative_name']}"
        target = resolve(store.roots["archive"], relative)
        number = 1
        while target.exists() or relative.casefold() in names:
            number += 1
            parent = Path(item["relative_name"])
            name = parent.with_name(f"{parent.stem}__{number}{parent.suffix}").as_posix()
            relative = f"{base}/{item['bucket']}/{name}"
            target = resolve(store.roots["archive"], relative)
        names.add(relative.casefold())
        items.append({**item, "side": item["role"], "relative_path": relative,
            "source_signature": list(signature(item["source"])), "phase": "planned",
            "mode": None, "hashes": None, "source_removed": False})
    plan = {"record": deepcopy(r), "fingerprint": fingerprint, "items": items,
        "archive": store.roots["archive"], "metadata": str(store.metadata), "layout": layout,
        "authorized": False, "phase": "planned", "created_at": now()}
    atomic_json(journal, plan)
    return journal, plan


def plan_stim(store, draft):
    store._check_ready()
    with lock(store.metadata):
        return deepcopy(_prepare(store, draft)[1])


def finalize_stim(store, draft, *, move_confirmed=False, source_complete=False,
                  progress=None, cancel=None, failpoint=None):
    if not move_confirmed or not source_complete:
        raise LoggerError("Confirm acquisition is complete and authorize moving these files")
    store._check_ready()
    with lock(store.metadata):
        journal, plan = _prepare(store, draft)
        plan["authorized"] = True
        atomic_json(journal, plan)
        return finish_transfers(store, journal, plan, progress=progress, cancel=cancel,
                                failpoint=failpoint, fresh=True)


def pending_stim(store):
    plans = [read_json(p) for p in sorted((store.local / "stim-transfers").glob("*.json"))]
    return [p for p in plans if p["authorized"] and p["phase"] != "complete"]


def recover_stim_locked(store, progress=None, cancel=None):
    return [finish_transfers(store, store._path(p["record"]["record_id"], store.local / "stim-transfers"),
                             p, progress=progress, cancel=cancel) for p in pending_stim(store)]
