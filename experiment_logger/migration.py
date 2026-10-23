"""Explicit, previewed migration of existing logger paths. Never runs on launch."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .io import (LoggerError, Cancelled, atomic_bytes, atomic_json, digest, encode,
                 hash_file, now, read_json, resolve, uid, rename_path)
from .layout import bundle_entries, reserve_label, role_filename, CSV_DIRECTORY, csv_session_directory, mouse_folder, day_folder


def _state(store):
    return digest(encode({"catalog": store._catalog(), "labels": store._labels(), "index": store.index()}))


def plan_migration(store):
    """Read-only preview. Numeric reservations are simulated, never saved here."""
    store._assert_no_migration()
    if store.pending():
        raise LoggerError("Recover pending publications before planning a path migration")
    index = store.index()
    if any(i["integrity"] != "ok" for i in index.values()):
        raise LoggerError("Repair record integrity failures before migration")
    labels = deepcopy(store._labels())
    catalog = store._catalog()
    data = [store.read_record(i["record_id"]) for i in index.values()
            if i["record_type"] == "data" and i.get("data_type") == "task"]
    data.sort(key=lambda r: (r["session_date"], r["created_at"], r["record_id"]))
    bundles, moves = [], []
    for r in data:
        label = reserve_label(r, labels, store.roots["archive"], store.metadata / "records")
        old_directory = Path(index[r["record_id"]]["relative_path"]).parent.as_posix()
        new_directory = f"records/{label['directory_name']}"
        old = resolve(store.metadata, old_directory)
        entries = bundle_entries(old)
        records = []
        for path, seal in entries:
            record = read_json(path)
            if record.get("session_id") != r["session_id"]:
                raise LoggerError("A mixed acquisition bundle needs an explicit migration mapping")
            records.append({"old_filename": path.name, "filename": role_filename(record), **seal})
        if len({s["filename"] for s in records}) != len(records):
            raise LoggerError("Ambiguous record names in migration bundle")
        if old_directory != new_directory or not (old / "manifest.json").exists():
            bundles.append({"old_directory": old_directory, "new_directory": new_directory,
                            "layout": label, "records": records})
        for f in r["files"]:
            item = catalog["files"][f["file_id"]]
            for loc in item["locations"]:
                prefixes = [f"{category}/{prefix}{r['mouse_id']}_{r['session_date']}/"
                            for category in (CSV_DIRECTORY, "csv") for prefix in ("", "mouse")]
                prefixes.extend(f"{category}/{mouse_folder(r['mouse_id'])}/{day_folder(r['session_date'])}/" for category in (CSV_DIRECTORY, "csv"))
                if loc["root_id"] != "archive" or not any(loc["relative_path"].startswith(p) for p in prefixes):
                    continue
                new_relative = f"{csv_session_directory(r, label)}/{Path(loc['relative_path']).name}"
                if new_relative != loc["relative_path"]:
                    moves.append({"file_id": f["file_id"], "root_id": "archive", "old_relative_path": loc["relative_path"],
                                  "new_relative_path": new_relative, "sha256": f["sha256"], "size_bytes": f["size_bytes"]})
    return {"version": 1, "state_sha256": _state(store), "bundles": bundles, "csv_moves": moves, "labels": labels}


def _verify(path, expected, progress=None, cancel=None):
    actual = hash_file(path, progress, cancel)
    if any(actual[k] != expected[k] for k in actual):
        raise LoggerError(f"Migration checksum mismatch: {path}")


def apply_migration(store, plan, author, progress=None, cancel=None, failpoint=None):
    if encode(plan) != encode(plan_migration(store)):
        raise LoggerError("Migration preview is stale or modified; generate a new preview")
    if not plan["bundles"] and not plan["csv_moves"]:
        return {"changed": False, "git_committed": False, "message": "Paths already use the current layout"}
    for b in plan["bundles"]:
        if b["old_directory"] == b["new_directory"] or resolve(store.metadata, b["new_directory"]).exists():
            raise LoggerError("Migration destination already exists; nothing overwritten")
        old = resolve(store.metadata, b["old_directory"])
        expected = {s["old_filename"] for s in b["records"]}
        expected.update(s["old_filename"].replace(".json", ".seal.json") for s in b["records"])
        expected.add("manifest.json")
        if any(p.name not in expected or not p.is_file() for p in old.iterdir()):
            raise LoggerError("Unexpected files in a legacy bundle; review it before migration")
    destinations = set()
    for m in plan["csv_moves"]:
        target = resolve(store.roots[m["root_id"]], m["new_relative_path"])
        if target.exists() or str(target) in destinations:
            raise LoggerError("CSV migration destination already exists or is duplicated")
        destinations.add(str(target))
        _verify(resolve(store.roots[m["root_id"]], m["old_relative_path"]), m, progress, cancel)
    store._runtime_ignores()
    op = {"migration_id": uid(), "phase": "prepared", "plan": plan, "author": author, "created_at": now()}
    path = store.metadata / ".migrations" / f"{op['migration_id']}.json"
    atomic_json(path, op)
    return finish_migration(store, op, progress, cancel, failpoint)


def finish_migration(store, op, progress=None, cancel=None, failpoint=None):
    """Caller holds writer lock. Complete a prepared migration without deleting originals."""
    mid, plan = op["migration_id"], op["plan"]
    journal = store._path(mid, store.metadata / ".migrations")
    for m in plan["csv_moves"]:
        if cancel and cancel():
            raise Cancelled("Migration interrupted; use Recover to finish the journaled operation")
        root = store.roots[m["root_id"]]
        old, new = resolve(root, m["old_relative_path"]), resolve(root, m["new_relative_path"])
        if old.exists():
            if new.exists():
                raise LoggerError("Both CSV migration paths exist; manual review required")
            _verify(old, m, progress, cancel)
            new.parent.mkdir(parents=True, exist_ok=True)
            rename_path(old, new)
        _verify(new, m, progress, cancel)
        # Remove only now-empty archive directories, never recursively delete data.
        if old.parent != new.parent and old.parent.exists():
            try:
                old.parent.rmdir()
            except OSError:
                pass
    if failpoint == "csv_moved":
        raise LoggerError("Injected migration interruption after CSV moves")
    for i, b in enumerate(plan["bundles"]):
        old = resolve(store.metadata, b["old_directory"])
        new = resolve(store.metadata, b["new_directory"])
        backup = resolve(store.metadata, f".migration-backups/{mid}/{b['old_directory']}")
        stage = resolve(store.metadata, f".staging/migration_{mid}_{i}")
        if not new.exists():
            source = old if old.exists() else backup
            stage.mkdir(parents=True, exist_ok=True)
            seals = []
            for s in b["records"]:
                raw = (source / s["old_filename"]).read_bytes()
                if digest(raw) != s["sha256"]:
                    raise LoggerError("Original scientific JSON changed; migration stopped")
                atomic_bytes(stage / s["filename"], raw)
                seals.append({k: v for k, v in s.items() if k != "old_filename"})
            atomic_json(stage / "manifest.json", {"version": 2, "layout": b["layout"], "records": seals})
            if old.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    raise LoggerError("Migration backup already exists; manual review required")
                rename_path(old, backup)
            if failpoint == "metadata_backed_up":
                raise LoggerError("Injected migration interruption after metadata backup")
            new.parent.mkdir(parents=True, exist_ok=True)
            rename_path(stage, new)
        for p, seal in bundle_entries(new):
            if digest(p.read_bytes()) != seal["sha256"]:
                raise LoggerError("Migrated record failed checksum validation")
        if {s["record_id"] for _, s in bundle_entries(new)} != {s["record_id"] for s in b["records"]}:
            raise LoggerError("Migrated bundle does not match the preview")
    catalog = store._catalog()
    changed = False
    for m in plan["csv_moves"]:
        for loc in catalog["files"][m["file_id"]]["locations"]:
            if loc["root_id"] == m["root_id"] and loc["relative_path"] == m["old_relative_path"]:
                loc.update(relative_path=m["new_relative_path"], verified_at=now(), verification_status="verified")
                changed = True
    if changed:
        store._write_catalog(catalog)
    atomic_json(store.metadata / "session_labels.json", plan["labels"])
    op["phase"] = "finalized_locally"
    atomic_json(journal, op)
    result = {"migration_id": mid, "changed": True, "finalized_locally": True,
              "git_committed": False, "backed_up": False, "commit": None,
              "bundles_moved": len(plan["bundles"]), "csvs_moved": len(plan["csv_moves"])}
    try:
        paths = [".gitignore", "catalog.json", "catalog_history", "session_labels.json"]
        for b in plan["bundles"]:
            paths.append(b["new_directory"])
            old_path = b["old_directory"]
            if store.git("ls-files", "--", old_path) or (store._has_head() and store.git("ls-tree", "-r", "--name-only", "HEAD", "--", old_path)):
                paths.append(old_path)
        store.git("add", "--", *paths)
        if not store._has_head() or store.git("diff", "HEAD", "--", *paths):
            store.git("-c", f"user.name={op['author']}", "-c", "user.email=logger@localhost", "commit", "--only",
                      "-m", "Use readable mouse session paths and combined manifests", "--", *paths)
        result.update(git_committed=True, commit=store.git("rev-parse", "HEAD"))
        op["phase"] = "git_committed"
    except LoggerError as exc:
        result["pending_error"] = str(exc)
    atomic_json(journal, {**op, "receipt": result})
    atomic_json(store.metadata / "record_index.json", store.index())
    return result
