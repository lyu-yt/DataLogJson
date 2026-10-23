"""Expression folder discovery and verified TIFF registration, independent of Qt.

Only filenames and TIFF signatures are inspected. Images/stacks are never decoded.
Source paths and resumable copy plans stay in private machine-local state.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import os
import re

from .io import (LoggerError, Cancelled, atomic_json, digest, encode, hash_file,
                 lock, now, read_json, rename_path, resolve, signature, uid)
from .layout import day_folder, mouse_folder
from .models import envelope, new_task, require_valid, seal_record, session_for

CHANNELS = {"left_red": ("Left red", "left/red"),
            "left_green": ("Left green", "left/green"),
            "right": ("Right", "right"), "other": ("Other", "other")}
MOUSE = re.compile(r"(?:mouse_?)?([0-9]{3})", re.IGNORECASE)
TIFF_SUFFIXES = {".tif", ".tiff"}


def mouse_folders(folder):
    """Return mouse choices from a day folder, or the supplied mouse/acquisition folder."""
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise LoggerError("Choose an existing expression folder")
    children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and MOUSE.fullmatch(p.name))
    if not MOUSE.fullmatch(folder.name) and children:
        return children
    return [folder]


def new_expression(folder):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise LoggerError("Choose an existing expression folder")
    paths = {k: [] for k in CHANNELS}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in TIFF_SUFFIXES:
            continue
        name = path.name.lower()
        key = next((key for prefix, key in (("red_", "left_red"), ("green_", "left_green"),
                    ("right_", "right")) if name.startswith(prefix)), "other")
        paths[key].append(str(path.resolve()))
    match = MOUSE.fullmatch(folder.name)
    date = None
    if re.fullmatch(r"[0-9]{6}", folder.parent.name):
        try:
            date = datetime.strptime("20" + folder.parent.name, "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    record = {**envelope("data"), "data_type": "expression", "session_id": uid(),
              "mouse_id": match.group(1) if match else None, "session_date": date,
              "experiment_type": "expression_check", "acquisition": deepcopy(new_task()["acquisition"]),
              "metadata": {"channels": {k: {"selected": bool(paths[k]), "file_ids": []} for k in CHANNELS}},
              "files": [], "related_records": [], "notes": None}
    return {"revision": 0, "record": record, "source_folder": str(folder), "channel_paths": paths}


def record_errors(record, final):
    errors = []
    channels = record["metadata"]["channels"]
    files = {f["file_id"]: f for f in record["files"]}
    referenced = []
    if final and not any(c["selected"] for c in channels.values()):
        errors.append("metadata.channels: select at least one channel")
    for key, channel in channels.items():
        ids = channel["file_ids"]
        if not channel["selected"] and ids:
            errors.append(f"metadata.channels.{key}: unchecked channels cannot register files")
        if final and channel["selected"] and not ids:
            errors.append(f"files: {CHANNELS[key][0]} needs at least one registered TIFF")
        for fid in ids:
            if fid not in files or files[fid]["role"] != key:
                errors.append(f"metadata.channels.{key}.file_ids: unresolved or mismatched file")
        referenced.extend(ids)
    if set(referenced) != set(files) or len(referenced) != len(set(referenced)):
        errors.append("files: channel file references must cover each TIFF exactly once")
    return errors


def inspect_tiff(path):
    path = Path(path)
    if path.suffix.lower() not in TIFF_SUFFIXES or not path.is_file():
        raise LoggerError(f"TIFF file not found: {path}")
    with path.open("rb") as stream:
        header = stream.read(8)
    classic = len(header) == 8 and header[:4] in (b"II*\x00", b"MM\x00*")
    big = header in (b"II+\x00\x08\x00\x00\x00", b"MM\x00+\x00\x08\x00\x00")
    if not (classic or big):
        raise LoggerError(f"Invalid TIFF/BigTIFF signature: {path.name}")


def selection(draft):
    """Cheap preflight; no hashing or archive writes. Disabled paths stay draft-only."""
    record = draft["record"]
    require_valid(record)
    if record.get("data_type") != "expression" or record["record_state"] != "draft":
        raise LoggerError("Only expression drafts can be finalized")
    for field in ("mouse_id", "session_date", "operator"):
        if not record.get(field) or not str(record[field]).strip():
            raise LoggerError(f"{field}: required")
    mouse_folder(record["mouse_id"])
    day_folder(record["session_date"])
    selected, seen = [], set()
    for key in CHANNELS:
        if not record["metadata"]["channels"][key]["selected"]:
            continue
        paths = draft.get("channel_paths", {}).get(key, [])
        if not isinstance(paths, list) or not paths:
            raise LoggerError(f"{CHANNELS[key][0]}: select at least one TIFF or uncheck this channel")
        for value in paths:
            source = Path(value).expanduser()
            if not source.is_absolute():
                raise LoggerError(f"{CHANNELS[key][0]}: use absolute file paths")
            source = source.resolve()
            normalized = os.path.normcase(str(source))
            if normalized in seen:
                raise LoggerError("The same TIFF was selected more than once")
            seen.add(normalized)
            inspect_tiff(source)
            selected.append({"channel": key, "source": str(source)})
    if not selected:
        raise LoggerError("Select at least one expression channel and TIFF")
    return selected


def _prepare(store, draft):
    """Caller holds the shared writer lock. Persist paths before any copying."""
    chosen = selection(draft)
    r = draft["record"]
    index = store.index()
    if r["record_id"] in index or r["session_id"] in index:
        raise LoggerError("Duplicate finalization; expression session already exists")
    saved = store._path(r["record_id"], store.local / "drafts")
    if saved.exists() and read_json(saved)["revision"] != draft["revision"]:
        raise LoggerError("Stale draft; reload before finalizing")
    context = {"mouse_id": r["mouse_id"], "session_date": r["session_date"],
               "session_id": r["session_id"], "selected_files": chosen}
    fingerprint = digest(encode(context))
    journal = store._path(r["record_id"], store.local / "expression-transfers")
    if journal.exists():
        plan = read_json(journal)
        if plan["selection_sha256"] == fingerprint:
            return journal, plan
        if any(item["hashes"] for item in plan["items"]):
            raise LoggerError("Expression selections changed after copying started. Drop the folder again to create a new draft.")
    layout = store._reserve_layout(r)
    occupied, items = set(), []
    base = f"expression/{mouse_folder(r['mouse_id'])}/{day_folder(r['session_date'])}"
    for item in chosen:
        source = Path(item["source"])
        directory = f"{base}/{CHANNELS[item['channel']][1]}"
        parent = resolve(store.roots["archive"], directory)
        names = {p.name.casefold() for p in parent.iterdir()} if parent.exists() else set()
        number, name = 1, source.name
        while name.casefold() in names or f"{directory}/{name}".casefold() in occupied:
            number += 1
            name = f"{source.stem}__{number}{source.suffix}"
        relative = f"{directory}/{name}"
        resolve(store.roots["archive"], relative)
        occupied.add(relative.casefold())
        items.append({**item, "file_id": uid(), "relative_path": relative,
                      "source_signature": list(signature(source)), "hashes": None})
    plan = {"record_id": r["record_id"], "selection_sha256": fingerprint, "layout": layout,
            "items": items, "created_at": now(), "phase": "planned"}
    atomic_json(journal, plan)
    return journal, plan


def plan_expression(store, draft):
    store._check_ready()
    with lock(store.metadata):
        _, plan = _prepare(store, draft)
        return deepcopy(plan)


def finalize_expression(store, draft, *, source_complete=False, progress=None, cancel=None, failpoint=None):
    if not source_complete:
        raise LoggerError("Confirm acquisition has finished and all source TIFFs are closed")
    store._check_ready()
    with lock(store.metadata):
        journal, plan = _prepare(store, draft)
        total = sum(item["source_signature"][0] * 3 for item in plan["items"])
        completed = 0
        def report(n, _total):
            if progress:
                progress(min(completed + n, total), total)
        entries, files, verified = [], [], []
        for item in plan["items"]:
            if cancel and cancel():
                raise Cancelled("Expression copying cancelled; sources retained")
            source = Path(item["source"])
            before = tuple(item["source_signature"])
            if signature(source) != before:
                raise LoggerError("Source changed after destination planning; create a new expression draft")
            hashes = hash_file(source, report, cancel)
            completed += before[0]
            if item["hashes"] and hashes != item["hashes"]:
                raise LoggerError("Source content changed since the previous copy attempt")
            item["hashes"] = hashes
            atomic_json(journal, plan)
            target = resolve(store.roots["archive"], item["relative_path"])
            if target.exists():
                completed += before[0]
                if hash_file(target, report, cancel) != hashes:
                    raise LoggerError("Expression destination contains different bytes; nothing overwritten")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(f".{target.name}.{uid()}.partial")
                try:
                    copied = 0
                    with source.open("rb") as src, partial.open("xb") as dst:
                        while chunk := src.read(4 * 1024 * 1024):
                            if cancel and cancel():
                                raise Cancelled("Expression copying cancelled; sources retained")
                            dst.write(chunk)
                            copied += len(chunk)
                            if copied > before[0]:
                                raise LoggerError("Source grew during copying; source retained")
                            report(copied, before[0])
                        dst.flush()
                        os.fsync(dst.fileno())
                    completed += before[0]
                    if hash_file(partial, report, cancel) != hashes or signature(source) != before:
                        raise LoggerError("Source changed or copied TIFF differs; source retained")
                    rename_path(partial, target)
                finally:
                    partial.unlink(missing_ok=True)
            completed += before[0]
            verified.append((target, signature(target)))
            files.append({"file_id": item["file_id"], "role": item["channel"], "format": "tiff",
                          "media_type": "image/tiff", "original_filename": source.name, **hashes})
            entries.append({"file_id": item["file_id"], "original_filename": source.name, **hashes,
                            "locations": [{"root_id": "archive", "relative_path": item["relative_path"],
                                           "verified_at": now(), "verification_status": "verified"}]})
            if failpoint == "copied":
                raise LoggerError("Injected interruption after verified TIFF copy")
        for item in plan["items"]:
            if signature(item["source"]) != tuple(item["source_signature"]):
                raise LoggerError("Source changed during registration; source retained")
        if any(signature(path) != before for path, before in verified):
            raise LoggerError("Archived TIFF changed during registration; retry verification")
        if cancel and cancel():
            raise Cancelled("Expression finalization cancelled before publication; sources retained")
        r = deepcopy(draft["record"])
        r["files"] = files
        for key, channel in r["metadata"]["channels"].items():
            channel["file_ids"] = [f["file_id"] for f in files if f["role"] == key]
        r = seal_record(r, r["operator"])
        session = session_for(r, digest(encode(r)))
        receipt = store._publish([r, session], entries, r["operator"], failpoint, plan["layout"])
        receipt.update(source_removed=False, session_number=plan["layout"]["number"],
                       files=[{"channel": item["channel"], "relative_path": item["relative_path"]}
                              for item in plan["items"]])
        plan["phase"] = "published"
        atomic_json(journal, plan)
        atomic_json(store.metadata / ".receipts" / f"{receipt['transaction_id']}.json", receipt)
        if progress:
            progress(total, total)
        return receipt
