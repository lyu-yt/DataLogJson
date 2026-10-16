"""Strict JSON, durable replacement, bounded path resolution and streamed hashes."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


class LoggerError(Exception):
    pass


class Cancelled(LoggerError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LoggerError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise LoggerError(f"Invalid JSON number: {value}")


def loads(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)


def read_json(path):
    return loads(Path(path).read_text(encoding="utf-8"))


def encode(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       indent=2) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    # Windows does not support opening a directory this way. SMB durability also
    # depends on the server; checksums/recovery are still required after outages.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uid()}.partial")
    try:
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Windows scanners/indexers can briefly hold the previous journal open.
        # Keep the complete temporary bytes and retry only transient Windows
        # access/sharing errors; persistent permissions still fail explicitly.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(0.02 * 2 ** attempt)
        sync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, encode(value))


def rename_path(source, destination):
    """Rename pre-resolved paths without overwriting, tolerating transient scanners."""
    source, destination = Path(source), Path(destination)
    for attempt in range(8):
        if destination.exists():
            raise LoggerError(f"Destination already exists: {destination}")
        try:
            os.rename(source, destination)
            sync_directory(destination.parent)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 7:
                raise
            time.sleep(0.02 * 2 ** attempt)


@contextmanager
def lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".write.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LoggerError(f"Writer lock exists: {path}. Retry later; see recovery instructions for a stale lock.") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encode({"pid": os.getpid(), "created_at": now()}))
            f.flush()
            os.fsync(f.fileno())
        yield
    finally:
        path.unlink(missing_ok=True)


def resolve(root, relative):
    root = Path(root).expanduser()
    if not root.is_absolute():
        raise LoggerError("Configured root must be absolute")
    rel = PurePosixPath(relative)
    if (not relative or "\\" in relative or ":" in relative or rel.is_absolute()
            or PureWindowsPath(relative).drive or any(x in ("..", ".", "") for x in relative.split("/"))):
        raise LoggerError(f"Unsafe relative path: {relative}")
    target = root.joinpath(*rel.parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise LoggerError(f"Path escapes configured root: {relative}")
    return target


def signature(path):
    s = Path(path).stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino, s.st_dev


def hash_file(path, progress=None, cancel=None):
    path = Path(path)
    before = signature(path)
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            if cancel and cancel():
                raise Cancelled("Cancelled; no complete file checksum was registered")
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            if size > before[0]:
                raise LoggerError("File changed during hashing. Finish acquisition and retry.")
            if progress:
                progress(size, before[0])
    if before != signature(path) or size != before[0]:
        raise LoggerError("File changed during hashing. Finish acquisition and retry.")
    return {"sha256": h.hexdigest(), "size_bytes": size}
