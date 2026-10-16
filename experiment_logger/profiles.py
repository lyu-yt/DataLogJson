"""Editable mouse profiles with stale-save protection and journaled Git recovery."""
from copy import deepcopy

from .colony import require_profile
from .io import LoggerError, atomic_bytes, atomic_json, digest, encode, lock, now, read_json, resolve, uid
from .layout import mouse_folder


def relative_path(mouse_id):
    return f"records/{mouse_folder(mouse_id)}/mouse_info.json"


def load_profile(store, mouse_id):
    path = resolve(store.metadata, relative_path(mouse_id))
    if not path.exists():
        return None
    profile = read_json(path)
    schema, _ = store._archived_schema(profile.get("schema_version"))
    require_profile(profile, schema)
    if profile["mouse_id"] != mouse_id:
        raise LoggerError("Mouse profile ID does not match its directory")
    return profile


def save_profile(store, profile, author, failpoint=None):
    store._check_ready()
    require_profile(profile)
    if not author or not author.strip():
        raise LoggerError("An operator name is required")
    with lock(store.metadata):
        mouse_id = profile["mouse_id"]
        if any(op["mouse_id"] == mouse_id for op in store.pending_profiles()):
            raise LoggerError("This mouse has a pending profile save. Use Recover before editing again.")
        relative = relative_path(mouse_id)
        path = resolve(store.metadata, relative)
        current = load_profile(store, mouse_id)
        revision = current["revision"] if current else 0
        if profile["revision"] != revision:
            raise LoggerError("Stale mouse profile: reload before saving")
        if current and any(profile[key] != current[key] for key in ("profile_id", "mouse_id", "created_at", "created_by")):
            raise LoggerError("Mouse identity and original creation fields cannot change")
        if current and profile["import_history"][:len(current["import_history"])] != current["import_history"]:
            raise LoggerError("Existing import history cannot be removed or changed")
        store._ensure_schema(profile["schema_version"])
        store._runtime_ignores()
        saved = deepcopy(profile)
        saved.update(revision=revision + 1, updated_at=now(), updated_by=author.strip())
        require_profile(saved)
        operation = {"operation_id": uid(), "mouse_id": mouse_id, "phase": "prepared",
                     "author": author.strip(), "relative_path": relative, "profile": saved,
                     "previous_sha256": digest(path.read_bytes()) if path.exists() else None,
                     "sha256": digest(encode(saved))}
        atomic_json(store.metadata / ".profile-operations" / f"{operation['operation_id']}.json", operation)
        if failpoint == "prepared":
            raise LoggerError("Injected profile interruption after preparation")
        return finish_profile(store, operation)


def finish_profile(store, operation):
    """Caller holds the repository lock. Never overwrite unexpected profile bytes."""
    op = deepcopy(operation)
    profile = op["profile"]
    schema, _ = store._archived_schema(profile["schema_version"])
    require_profile(profile, schema)
    if op["relative_path"] != relative_path(profile["mouse_id"]) or op["mouse_id"] != profile["mouse_id"]:
        raise LoggerError("Profile journal path does not match mouse identity")
    raw = encode(profile)
    if digest(raw) != op["sha256"]:
        raise LoggerError("Profile journal checksum mismatch")
    path = resolve(store.metadata, op["relative_path"])
    actual = digest(path.read_bytes()) if path.exists() else None
    if actual != op["sha256"]:
        if actual != op["previous_sha256"]:
            raise LoggerError("Mouse profile changed outside the pending save; manual review required")
        atomic_bytes(path, raw)
    journal = store.metadata / ".profile-operations" / f"{op['operation_id']}.json"
    op["phase"] = "saved_locally"
    atomic_json(journal, op)
    receipt = {"mouse_id": op["mouse_id"], "revision": profile["revision"],
               "relative_path": op["relative_path"], "saved_locally": True,
               "git_committed": False, "backed_up": False, "commit": None}
    try:
        paths = [op["relative_path"], "schemas", ".gitignore", "logger_repository.json"]
        if not store._has_head():
            paths.extend(["catalog.json", "catalog_history", "session_labels.json"])
        store.git("add", "--", *paths)
        if not store._has_head() or store.git("diff", "HEAD", "--", *paths):
            store.git("-c", f"user.name={op['author']}", "-c", "user.email=logger@localhost",
                      "commit", "--only", "-m", f"Update mouse {op['mouse_id']} profile revision {profile['revision']}", "--", *paths)
        receipt.update(git_committed=True, commit=store.git("rev-parse", "HEAD"))
        op["phase"] = "git_committed"
    except LoggerError as exc:
        receipt["pending_error"] = str(exc)
    atomic_json(journal, {**op, "receipt": receipt})
    return {"profile": profile, "receipt": receipt}
