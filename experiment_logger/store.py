"""Shared transactional service. The GUI never writes scientific JSON directly."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import csv
import os
import shutil
import subprocess

from .io import (LoggerError, Cancelled, atomic_bytes, atomic_json, digest, encode,
                 hash_file, lock, loads, now, read_json, resolve, signature, uid,
                 sync_directory, rename_path)
from .models import (RESOURCE, SCHEMA_FILES, inspect_csv, new_task, require_valid,
                     seal_record, session_for, envelope, schema_filename)
from .layout import (bundle_entries, check_record, empty_labels, reserve_label,
                     role_filename, slug, csv_session_directory)


class Store:
    def __init__(self, metadata, roots, local_state):
        self.metadata = Path(metadata).expanduser().resolve()
        self.roots = {k: str(Path(v).expanduser().resolve()) for k, v in roots.items()}
        self.local = Path(local_state).resolve()
        if not all(Path(v).is_absolute() for v in roots.values()):
            raise LoggerError("Root configuration requires absolute paths")

    def initialize(self):
        """Explicit setup of a dedicated metadata repository, never a parent repo."""
        self.metadata.mkdir(parents=True, exist_ok=True)
        if not (self.metadata / ".git").exists():
            if any(self.metadata.iterdir()):
                raise LoggerError("Choose an empty metadata directory, or an existing logger repository")
            self.git("init")
        for name in ("records", "schemas", "catalog_history", ".staging", ".operations", ".receipts", ".migrations", ".migration-backups", ".profile-operations"):
            (self.metadata / name).mkdir(exist_ok=True)
        self.local.mkdir(parents=True, exist_ok=True)
        with lock(self.metadata):
            marker = self.metadata / "logger_repository.json"
            if marker.exists() and read_json(marker).get("format") != "mouse-logger-v1":
                raise LoggerError("Not a compatible metadata repository")
            if not marker.exists():
                # Existing non-logger Git repositories must never be repurposed.
                tracked = self.git("ls-files")
                if tracked.strip():
                    raise LoggerError("This repository already contains unrelated files")
                atomic_json(marker, {"format": "mouse-logger-v1"})
            for version in SCHEMA_FILES:
                self._ensure_schema(version)
            self._runtime_ignores()
            if not (self.metadata / "session_labels.json").exists():
                atomic_json(self.metadata / "session_labels.json", empty_labels())
            if not (self.metadata / "catalog.json").exists():
                self._write_catalog({"version": 1, "generation": 0, "files": {}})

    def git(self, *args):
        # A mounted lab share can report a NAS owner instead of the local user.
        # Trust the explicitly selected metadata repository for this invocation
        # only. Resolve mapped drives to UNC paths before Git compares ownership;
        # never write a global exception or trust every repository with '*'.
        r = subprocess.run(["git", "-c", "safe.directory=",
                            "-c", f"safe.directory={self.metadata.as_posix()}",
                            "-C", str(self.metadata), *args], capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if r.returncode:
            raise LoggerError(r.stderr.strip() or r.stdout.strip())
        return r.stdout.strip()

    def _check_ready(self):
        marker = self.metadata / "logger_repository.json"
        if not marker.exists() or read_json(marker).get("format") != "mouse-logger-v1":
            raise LoggerError("Initialize the metadata repository in Settings first")
        self._assert_no_migration()

    def _runtime_ignores(self):
        path = self.metadata / ".gitignore"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = text.splitlines()
        for item in (".write.lock", ".staging/", ".operations/", ".receipts/", "record_index.json", ".migrations/", ".migration-backups/", ".profile-operations/"):
            if item not in lines:
                lines.append(item)
        atomic_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))

    def pending_migrations(self):
        return [read_json(p) for p in sorted((self.metadata / ".migrations").glob("*.json"))
                if read_json(p).get("phase") != "git_committed"]

    def _assert_no_migration(self):
        if self.pending_migrations():
            raise LoggerError("A path migration is pending. Use Recover before saving or publishing records.")

    def _labels(self):
        path = self.metadata / "session_labels.json"
        return read_json(path) if path.exists() else empty_labels()

    def _ensure_schema(self, version):
        name = schema_filename(version)
        schema_path = self.metadata / "schemas" / name
        schema_bytes = (RESOURCE / "schemas" / name).read_bytes()
        if schema_path.exists() and schema_path.read_bytes() != schema_bytes:
            import re
            old_bytes = schema_path.read_bytes()
            old_version = loads(old_bytes).get("properties", {}).get("schema_version", {}).get("const")
            if old_version == version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", old_version or ""):
                raise LoggerError("Pinned schema differs; a new schema version is required")
            archive = self.metadata / "schemas" / f"record_v{old_version}.json"
            if archive.exists() and archive.read_bytes() != old_bytes:
                raise LoggerError("Historical schema archive conflicts; restore it before updating")
            atomic_bytes(archive, old_bytes)
            atomic_bytes(schema_path, schema_bytes)
        if not schema_path.exists():
            atomic_bytes(schema_path, schema_bytes)

    def _archived_schema(self, version, expected_hash=None):
        # Only the current rulebook ships with the app. Historical rulebooks
        # belong to the archive that sealed those records and must match the seal.
        for path in sorted((self.metadata / "schemas").glob("*.json")):
            raw = path.read_bytes()
            if expected_hash is not None and digest(raw) != expected_hash:
                continue
            schema = loads(raw)
            if schema.get("properties", {}).get("schema_version", {}).get("const") == version:
                return schema, digest(raw)
        raise LoggerError(f"Pinned schema {version} is missing or changed; restore it from the metadata backup")

    def _schema_hash(self, version):
        return self._archived_schema(version)[1]

    def _reserve_layout(self, record):
        self._assert_no_migration()
        labels = self._labels()
        layout = reserve_label(record, labels, self.roots["archive"], self.metadata / "records")
        atomic_json(self.metadata / "session_labels.json", labels)
        return layout

    def reserve_layout(self, record):
        self._check_ready()
        with lock(self.metadata):
            return self._reserve_layout(record)

    def _path(self, rid, parent):
        from uuid import UUID
        try:
            if str(UUID(rid)) != rid:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise LoggerError("Invalid UUID") from exc
        return parent / f"{rid}.json"

    def new(self, source):
        return {"revision": 0, "record": new_task(source), "source_path": str(Path(source).resolve())}

    def new_expression(self, folder):
        from .expression import new_expression
        return new_expression(folder)

    def plan_expression(self, draft):
        from .expression import plan_expression
        return plan_expression(self, draft)

    def finalize_expression(self, draft, *, source_complete=False, progress=None, cancel=None, failpoint=None):
        from .expression import finalize_expression
        return finalize_expression(self, draft, source_complete=source_complete,
                                   progress=progress, cancel=cancel, failpoint=failpoint)

    def save_draft(self, draft):
        r = draft["record"]
        require_valid(r)
        if r["record_state"] != "draft":
            raise LoggerError("Finalized records are read-only; create an amendment")
        self._check_ready()
        with lock(self.metadata):
            if r["record_id"] in self.index():
                raise LoggerError("This record is already finalized")
            p = self._path(r["record_id"], self.local / "drafts")
            rev = read_json(p)["revision"] if p.exists() else 0
            if draft["revision"] != rev:
                raise LoggerError("Stale draft: another save changed this draft. Reload before saving.")
            saved = deepcopy(draft)
            saved["revision"] = rev + 1
            atomic_json(p, saved)
            return saved

    def load_draft(self, rid):
        return read_json(self._path(rid, self.local / "drafts"))

    def drafts(self, *, index=None):
        paths = sorted((self.local / "drafts").glob("*.json"))
        if not paths:
            return []
        published = self.index() if index is None else index
        return [read_json(p) for p in paths if p.stem not in published]

    def _catalog(self):
        return read_json(self.metadata / "catalog.json")

    def _write_catalog(self, catalog):
        catalog = deepcopy(catalog)
        catalog["generation"] += 1
        name = f"catalog_history/{catalog['generation']:012d}.json"
        path = self.metadata / name
        if path.exists():
            raise LoggerError("Catalog generation already exists; rebuild catalog before retrying")
        atomic_json(path, catalog)
        atomic_json(self.metadata / "catalog.json", catalog)
        return name

    def rebuild_catalog(self):
        with lock(self.metadata):
            snapshots = sorted((self.metadata / "catalog_history").glob("*.json"))
            if snapshots:
                catalog = read_json(snapshots[-1])
            else:
                catalog = {"version": 1, "generation": 0, "files": {}}
                for info in self.index().values():
                    for f in self.read_record(info["record_id"]).get("files", []):
                        catalog["files"][f["file_id"]] = {**f, "locations": []}
            atomic_json(self.metadata / "catalog.json", catalog)
            return catalog

    def index(self):
        result = {}
        session_datasets = {}
        root = self.metadata / "records"
        # Profiles sit at mouse level and are not finalized acquisition bundles.
        # Discover both historical flat bundles and the nested mouse/date layout.
        for folder in sorted(root.rglob("*")):
            if not folder.is_dir() or folder.is_symlink():
                continue
            if (not (folder / "manifest.json").is_file() and not any(folder.glob("*.seal.json"))
                    and not any((folder / name).is_file() for name in ("task.json", "expression.json", "session.json", "analysis_run.json", "amendment.json"))):
                continue
            try:
                entries = bundle_entries(folder)
            except Exception as exc:
                invalid_id = f"invalid:{folder.relative_to(root).as_posix()}"
                result[invalid_id] = {"record_id": invalid_id,
                    "relative_path": folder.relative_to(self.metadata).as_posix(), "sha256": None,
                    "integrity": f"INVALID: {exc}", "record_type": "invalid", "mouse_id": None,
                    "session_date": None, "experiment_type": None, "data_type": None, "training_day": None}
                continue
            for p, seal in entries:
                rid = seal["record_id"]
                if rid in result:
                    raise LoggerError(f"Duplicate published record ID: {rid}")
                integrity = "ok"
                try:
                    version = loads(p.read_bytes()).get("schema_version")
                    schema, schema_hash = self._archived_schema(version, seal.get("schema_sha256"))
                    r = check_record(p, seal, schema_hash)
                    require_valid(r, True, schema)
                except Exception as exc:
                    integrity = f"INVALID: {exc}"
                    r = {"record_type": "invalid"}
                result[rid] = {"record_id": rid, "relative_path": p.relative_to(self.metadata).as_posix(),
                               "sha256": seal.get("sha256"), "integrity": integrity,
                               "record_type": r.get("record_type"), "mouse_id": r.get("mouse_id"),
                               "session_date": r.get("session_date"), "experiment_type": r.get("experiment_type"),
                               "data_type": r.get("data_type"),
                               "training_day": (r.get("training_day") if r.get("record_type") == "session"
                                                else r.get("metadata", {}).get("training_day"))}
                if r.get("record_type") == "session" and integrity == "ok":
                    session_datasets[rid] = r.get("datasets", [])
        # Category filters include a session when its fixed dataset references
        # resolve to one category. This is a rebuildable view, not a record edit.
        for rid, references in session_datasets.items():
            datasets = [result.get(ref["record_id"], {}) for ref in references]
            if datasets and all(info.get("integrity") == "ok" and info.get("sha256") == ref["sha256"]
                                for info, ref in zip(datasets, references)):
                categories = {info.get("data_type") for info in datasets}
                if len(categories) == 1:
                    result[rid]["data_type"] = next(iter(categories))
        return result

    def rebuild_index(self):
        with lock(self.metadata):
            result = self.index()
            atomic_json(self.metadata / "record_index.json", result)
            return result

    def read_record(self, rid, *, index=None):
        """Reuse a caller's current scan, but recheck the bytes actually read."""
        info = (self.index() if index is None else index).get(rid)
        if not info:
            raise LoggerError(f"Record not found: {rid}")
        if info["integrity"] != "ok":
            raise LoggerError(info["integrity"])
        raw = resolve(self.metadata, info["relative_path"]).read_bytes()
        if digest(raw) != info["sha256"]:
            raise LoggerError("Record checksum mismatch; refresh the archive before continuing")
        return loads(raw)

    def file_locations(self, fid, *, catalog=None):
        item = (self._catalog() if catalog is None else catalog)["files"].get(fid)
        if not item:
            raise LoggerError(f"Unregistered file ID: {fid}")
        results = []
        for location in item["locations"]:
            loc = dict(location)
            try:
                p = resolve(self.roots[loc["root_id"]], loc["relative_path"])
                loc["path"] = str(p)
                loc["available"] = p.is_file()
            except (KeyError, OSError, LoggerError):
                loc["available"] = False
            results.append(loc)
        return results

    def verify_file(self, fid, progress=None, cancel=None):
        with lock(self.metadata):
            catalog = self._catalog()
            item = catalog["files"][fid]
            for loc in item["locations"]:
                try:
                    p = resolve(self.roots[loc["root_id"]], loc["relative_path"])
                    actual = hash_file(p, progress, cancel)
                    loc["verification_status"] = "verified" if all(actual[k] == item[k] for k in actual) else "mismatch"
                except Cancelled:
                    raise
                except (OSError, KeyError, LoggerError):
                    loc["verification_status"] = "missing_or_unreadable"
                loc["verified_at"] = now()
            self._write_catalog(catalog)
            return item["locations"]

    def locate(self, fid, root_ids, progress=None, cancel=None):
        """Search only selected configured roots; a name is never identity."""
        item = self._catalog()["files"][fid]
        found = []
        for root_id in root_ids:
            if root_id not in self.roots:
                raise LoggerError(f"Unknown root: {root_id}")
            root = Path(self.roots[root_id])
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink() and d != ".git"]
                for name in files:
                    if cancel and cancel():
                        raise Cancelled("Search cancelled")
                    candidate = Path(directory) / name
                    try:
                        relative = candidate.relative_to(root).as_posix()
                        candidate = resolve(root, relative)
                        if candidate.stat().st_size != item["size_bytes"]:
                            continue
                        if hash_file(candidate, progress, cancel)["sha256"] == item["sha256"]:
                            found.append({"root_id": root_id, "relative_path": relative})
                    except Cancelled:
                        raise
                    except (OSError, LoggerError):
                        continue
        return found

    def relink(self, fid, root_id, relative_path, progress=None, cancel=None):
        with lock(self.metadata):
            catalog = self._catalog()
            item = catalog["files"][fid]
            actual = hash_file(resolve(self.roots[root_id], relative_path), progress, cancel)
            if any(actual[k] != item[k] for k in actual):
                raise LoggerError("Candidate has different content; relink rejected")
            location = {"root_id": root_id, "relative_path": relative_path,
                        "verified_at": now(), "verification_status": "verified"}
            item["locations"] = [loc for loc in item["locations"] if
                                 (loc["root_id"], loc["relative_path"]) != (root_id, relative_path)] + [location]
            self._write_catalog(catalog)
            return location

    def destination(self, record, source, root_id="archive", layout=None):
        layout = layout or self.reserve_layout(record)
        relative = f"{csv_session_directory(record, layout)}/{Path(source).name}"
        return relative, resolve(self.roots[root_id], relative)

    def _bundle_name(self, records, layout=None):
        if layout:
            return layout["directory_name"]
        r = records[0]
        origin = r.get("original", {}).get("record_id")
        if not origin and r.get("run", {}).get("inputs"):
            origin = r["run"]["inputs"][0]["record_id"]
        info = self.index().get(origin)
        base = Path(info["relative_path"]).parent.relative_to("records").as_posix() if info else "experiment"
        if info:
            original = self.read_record(origin)
            if original.get("mouse_id") and original.get("session_id"):
                base = self._reserve_layout(original)["directory_name"]
        kind = "amendment" if r["record_type"] == "amendment" else "analysis_" + slug(r.get("run", {}).get("analysis_name", "run"))
        prefix = f"{base}_{kind}_"
        leaf_prefix = Path(prefix).name
        parent = resolve(self.metadata, f"records/{Path(prefix).parent.as_posix()}") if "/" in prefix else self.metadata / "records"
        used = [int(p.name[len(leaf_prefix):]) for p in parent.glob(leaf_prefix + "*")
                if p.name[len(leaf_prefix):].isdecimal()]
        for op in self.pending():
            name = op.get("record_directory", "").removeprefix("records/")
            if name.startswith(prefix) and name[len(prefix):].isdecimal():
                used.append(int(name[len(prefix):]))
        return prefix + str(max(used, default=0) + 1)

    def _seal(self, r, data):
        refs = list(r.get("datasets", []))
        refs.extend(r.get("run", {}).get("inputs", []))
        if "original" in r:
            refs.append(r["original"])
        return {"version": 1, "record_id": r["record_id"], "sha256": digest(data),
                "schema_sha256": self._schema_hash(r["schema_version"]),
                "references": refs,
                "files": [{k: f[k] for k in ("file_id", "sha256", "size_bytes")}
                          for f in r.get("files", [])]}

    def _publish(self, records, catalog_entries, author, failpoint=None, layout=None):
        """Caller holds writer lock. Directory rename is the publication boundary."""
        self._assert_no_migration()
        index = self.index()
        ids = [r["record_id"] for r in records]
        if len(ids) != len(set(ids)) or any(rid in index for rid in ids):
            raise LoggerError("Duplicate finalization or record ID")
        new_checksums = {r["record_id"]: digest(encode(r)) for r in records}
        known_files = {**self._catalog()["files"], **{e["file_id"]: e for e in catalog_entries}}
        for r in records:
            if r.get("record_state") != "finalized":
                raise LoggerError("Only finalized records can be published")
            refs = list(r.get("datasets", [])) + r.get("run", {}).get("inputs", [])
            if "original" in r:
                refs.append(r["original"])
            for ref in refs:
                actual = new_checksums.get(ref["record_id"]) or index.get(ref["record_id"], {}).get("sha256")
                if actual != ref["sha256"]:
                    raise LoggerError("Unresolved or changed record reference")
            for f in r.get("files", []):
                known = known_files.get(f["file_id"])
                if not known or any(known[k] != f[k] for k in ("sha256", "size_bytes")):
                    raise LoggerError("Unregistered or changed file reference")
        tx = uid()
        data_record = next((r for r in records if r["record_type"] == "data"), None)
        if data_record and not layout:
            layout = self._reserve_layout(data_record)
        record_directory = f"records/{self._bundle_name(records, layout)}"
        if resolve(self.metadata, record_directory).exists():
            raise LoggerError("Metadata destination already exists; nothing overwritten")
        stage = self.metadata / ".staging" / tx
        stage.mkdir()
        seals = []
        for r in records:
            require_valid(r, True)
            self._ensure_schema(r["schema_version"])
            data = encode(r)
            filename = role_filename(r)
            if (stage / filename).exists():
                raise LoggerError("Multiple records of one category require a separate publication bundle")
            atomic_bytes(stage / filename, data)
            seals.append({"filename": filename, **self._seal(r, data)})
        atomic_json(stage / "manifest.json", {"version": 2, "layout": layout, "records": seals})
        op = {"transaction_id": tx, "record_ids": ids, "phase": "prepared",
              "catalog_entries": catalog_entries, "author": author, "created_at": now(),
              "record_directory": record_directory}
        atomic_json(self.metadata / ".operations" / f"{tx}.json", op)
        if failpoint == "prepared":
            raise LoggerError("Injected interruption after preparation")
        # Make file identities resolvable before the complete bundle is visible.
        self._install_entries(op)
        destination = resolve(self.metadata, record_directory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rename_path(stage, destination)
        sync_directory(destination.parent)
        if failpoint == "published":
            raise LoggerError("Injected interruption after publication")
        return self._finish(op)

    def _install_entries(self, op):
        catalog = self._catalog()
        changed = False
        for entry in op["catalog_entries"]:
            old = catalog["files"].get(entry["file_id"])
            if old and (old["sha256"], old["size_bytes"]) != (entry["sha256"], entry["size_bytes"]):
                raise LoggerError("Conflicting catalog identity")
            if not old:
                catalog["files"][entry["file_id"]] = entry
                changed = True
        if changed:
            self._write_catalog(catalog)

    def _finish(self, op):
        tx = op["transaction_id"]
        stage = self.metadata / ".staging" / tx
        record_directory = op.get("record_directory", f"records/{tx}")
        dest = resolve(self.metadata, record_directory)
        self._install_entries(op)
        if not dest.exists():
            if not stage.exists():
                raise LoggerError("Incomplete publication: missing staged bundle")
            # Recovery validates the complete staged record/seal set before exposing it.
            entries = bundle_entries(stage)
            if {seal["record_id"] for _, seal in entries} != set(op["record_ids"]):
                raise LoggerError("Staged bundle does not match the publication journal")
            for path, seal in entries:
                version = loads(path.read_bytes()).get("schema_version")
                schema, schema_hash = self._archived_schema(version, seal.get("schema_sha256"))
                r = check_record(path, seal, schema_hash)
                require_valid(r, True, schema)
                if r["record_id"] in self.index():
                    raise LoggerError("Duplicate record during recovery")
            dest.parent.mkdir(parents=True, exist_ok=True)
            rename_path(stage, dest)
        index = self.index()
        if any(index.get(rid, {}).get("integrity") != "ok" for rid in op["record_ids"]):
            raise LoggerError("Published bundle failed integrity verification")
        op["phase"] = "finalized_locally"
        atomic_json(self.metadata / ".operations" / f"{tx}.json", op)
        receipt = {"transaction_id": tx, "record_ids": op["record_ids"], "finalized_locally": True,
                   "git_committed": False, "backed_up": False, "commit": None,
                   "record_directory": record_directory}
        try:
            paths = ["logger_repository.json", ".gitignore", "schemas", "catalog.json", "catalog_history", record_directory]
            if (self.metadata / "session_labels.json").exists():
                paths.append("session_labels.json")
            self.git("add", "--", *paths)
            # --only commits these paths while preserving unrelated staged changes.
            diff = self.git("diff", "HEAD", "--", *paths) if self._has_head() else "initial"
            if diff:
                self.git("-c", f"user.name={op['author']}", "-c", "user.email=logger@localhost",
                         "commit", "--only", "-m", f"Finalize experiment bundle {tx}", "--", *paths)
            receipt["commit"] = self.git("rev-parse", "HEAD")
            receipt["git_committed"] = True
            op["phase"] = "git_committed"
        except LoggerError as exc:
            receipt["pending_error"] = str(exc)
        atomic_json(self.metadata / ".receipts" / f"{tx}.json", receipt)
        atomic_json(self.metadata / ".operations" / f"{tx}.json", op)
        return receipt

    def _has_head(self):
        try:
            self.git("rev-parse", "--verify", "HEAD")
            return True
        except LoggerError:
            return False

    def pending(self):
        return [read_json(p) for p in sorted((self.metadata / ".operations").glob("*.json"))
                if read_json(p)["phase"] != "git_committed"]

    def load_mouse_profile(self, mouse_id):
        from .profiles import load_profile
        return load_profile(self, mouse_id)

    def save_mouse_profile(self, profile, author, failpoint=None):
        from .profiles import save_profile
        return save_profile(self, profile, author, failpoint)

    def pending_profiles(self):
        return [read_json(p) for p in sorted((self.metadata / ".profile-operations").glob("*.json"))
                if read_json(p)["phase"] != "git_committed"]

    def recover(self):
        with lock(self.metadata):
            # Repair a catalog replacement interrupted after its history snapshot.
            snapshots = sorted((self.metadata / "catalog_history").glob("*.json"))
            if snapshots:
                atomic_json(self.metadata / "catalog.json", read_json(snapshots[-1]))
            from .migration import finish_migration
            from .profiles import finish_profile
            migrations = [finish_migration(self, op) for op in self.pending_migrations()]
            profiles = [finish_profile(self, op) for op in self.pending_profiles()]
            return migrations + profiles + [self._finish(op) for op in self.pending()]

    def plan_readable_migration(self):
        from .migration import plan_migration
        with lock(self.metadata):
            return plan_migration(self)

    def migrate_paths(self, plan, author, progress=None, cancel=None, failpoint=None):
        from .migration import apply_migration
        with lock(self.metadata):
            return apply_migration(self, plan, author, progress, cancel, failpoint)

    def commit_catalog(self, author):
        with lock(self.metadata):
            paths = ["catalog.json", "catalog_history"]
            self.git("add", "--", *paths)
            if not self._has_head() or self.git("diff", "HEAD", "--", *paths):
                self.git("-c", f"user.name={author}", "-c", "user.email=logger@localhost",
                         "commit", "--only", "-m", "Update verified file locations", "--", *paths)
            return {"git_committed": True, "commit": self.git("rev-parse", "HEAD"), "backed_up": False}

    def finalize(self, draft, *, move=False, source_complete=False, progress=None, cancel=None, failpoint=None):
        if draft["record"].get("data_type") == "expression":
            if move:
                raise LoggerError("Expression logging copies TIFFs; source removal is not supported")
            return self.finalize_expression(draft, source_complete=source_complete,
                                            progress=progress, cancel=cancel, failpoint=failpoint)
        if not source_complete:
            raise LoggerError("Confirm acquisition has finished and the source file is closed")
        self._check_ready()
        r = deepcopy(draft["record"])
        if r["record_state"] != "draft":
            raise LoggerError("Only drafts can be finalized")
        # Validate manual fields before spending time reading a large file.
        from .models import validate
        errors = [e for e in validate(r, True) if not e.startswith("files:")]
        if errors:
            raise LoggerError("\n".join(errors))
        source = Path(draft["source_path"]).resolve()
        with lock(self.metadata):
            existing = self.index()
            if r["record_id"] in existing or r["session_id"] in existing:
                raise LoggerError("Duplicate finalization; session or dataset already exists")
            saved_path = self._path(r["record_id"], self.local / "drafts")
            if saved_path.exists() and read_json(saved_path)["revision"] != draft["revision"]:
                raise LoggerError("Stale draft; reload before finalizing")
            layout = self._reserve_layout(r)
            before = signature(source)
            mapping, _ = inspect_csv(source, cancel)
            hashes = hash_file(source, progress, cancel)
            relative, target = self.destination(r, source, layout=layout)
            if target.exists():
                # A prior interrupted attempt may have copied these exact bytes.
                if hash_file(target, progress, cancel) != hashes:
                    raise LoggerError("Destination already exists with different bytes; nothing overwritten")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + "." + uid() + ".partial")
                try:
                    with source.open("rb") as src, partial.open("xb") as dst:
                        copied = 0
                        while chunk := src.read(4 * 1024 * 1024):
                            if cancel and cancel():
                                raise Cancelled("Copy cancelled; source retained")
                            dst.write(chunk)
                            copied += len(chunk)
                            if progress:
                                progress(copied, hashes["size_bytes"])
                        dst.flush()
                        os.fsync(dst.fileno())
                    if hash_file(partial, progress, cancel) != hashes or before != signature(source):
                        raise LoggerError("Source changed or copied bytes differ; source retained")
                    # Same lock prevents another logger from claiming this path.
                    if target.exists():
                        raise LoggerError("Destination appeared during copy")
                    os.rename(partial, target)
                    sync_directory(target.parent)
                finally:
                    partial.unlink(missing_ok=True)
            if before != signature(source):
                raise LoggerError("Source changed during registration; retry")
            f = {"file_id": uid(), "role": "task_events", "format": "csv", "media_type": "text/tab-separated-values",
                 "original_filename": source.name, **hashes, "csv": mapping}
            r["files"] = [f]
            r = seal_record(r, r["operator"])
            session = session_for(r, digest(encode(r)))
            entry = {"file_id": f["file_id"], **hashes, "original_filename": source.name,
                     "locations": [{"root_id": "archive", "relative_path": relative,
                                    "verified_at": now(), "verification_status": "verified"}]}
            # This private machine-local journal is never included in shared metadata.
            move_journal = self.local / "moves" / f"{r['record_id']}.json"
            atomic_json(move_journal, {"record_id": r["record_id"], "source": str(source),
                                      "source_signature": list(before), "file_id": f["file_id"],
                                      "move_requested": move, "source_removed": False})
            receipt = self._publish([r, session], [entry], r["operator"], failpoint, layout)
            receipt.update(session_number=layout["number"], csv_relative_path=relative)
            receipt["source_removed"] = False
            if move and receipt["git_committed"]:
                try:
                    if source == target.resolve() or before != signature(source):
                        raise LoggerError("Source changed; original retained")
                    if hash_file(source, progress, cancel) != hashes or hash_file(target, progress, cancel) != hashes:
                        raise LoggerError("Cleanup verification failed; original retained")
                    if before != signature(source):
                        raise LoggerError("Source changed; original retained")
                    source.unlink()
                    receipt["source_removed"] = True
                    journal = read_json(move_journal)
                    journal["source_removed"] = True
                    atomic_json(move_journal, journal)
                except (OSError, LoggerError) as exc:
                    receipt["cleanup_pending"] = str(exc)
            elif move:
                receipt["cleanup_pending"] = "Original retained until Git commit succeeds. Recovery never deletes it automatically."
            atomic_json(self.metadata / ".receipts" / f"{receipt['transaction_id']}.json", receipt)
            return receipt

    def amend(self, rid, author, reason, corrected_values):
        with lock(self.metadata):
            self.read_record(rid)
            r = envelope("amendment")
            r.update(operator=author, original={"record_id": rid, "sha256": self.index()[rid]["sha256"]},
                     reason=reason, corrected_values=corrected_values)
            return self._publish([seal_record(r, author)], [], author)

    def amendments(self, rid):
        return [r for info in self.index().values() if info["record_type"] == "amendment"
                for r in [self.read_record(info["record_id"]) ] if r["original"]["record_id"] == rid]

    def preview(self, rid, limit=20):
        r = self.read_record(rid)
        if r.get("data_type") != "task":
            raise LoggerError("CSV preview is available only for task records")
        if not r.get("files"):
            return [], []
        f = r["files"][0]
        for loc in self.file_locations(f["file_id"]):
            if loc["available"]:
                with Path(loc["path"]).open(encoding="utf-8-sig", newline="") as stream:
                    rows = []
                    for row in csv.reader(stream, delimiter="\t"):
                        rows.append(row)
                        if len(rows) >= limit:
                            break
                return [c["semantic_name"] for c in f.get("csv", {}).get("columns", [])], rows
        raise LoggerError("CSV is missing; use Locations to relink it")
