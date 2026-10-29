from copy import deepcopy
from pathlib import Path
import shutil
import subprocess

import pytest

from experiment_logger.io import LoggerError, Cancelled, loads, encode, digest, hash_file, resolve, lock, atomic_json
from experiment_logger.models import inspect_csv, validate


def test_draft_roundtrip_and_invalid_finalization(store, sample, draft):
    assert draft == store.load_draft(draft["record"]["record_id"])
    assert draft["record"]["mouse_id"] == "007"
    assert draft["record"]["session_date"] == "2026-10-15"
    invalid = store.new(sample)
    errors = validate(invalid["record"], True)
    assert any("mouse_weight_g" in e for e in errors)
    with pytest.raises(LoggerError, match="mouse_weight_g"):
        store.finalize(invalid, source_complete=True)
    assert sample.exists()
    assert not store.index()


def test_finalize_manifest_git_amendment(store, draft, sample):
    receipt = store.finalize(draft, source_complete=True)
    assert receipt["git_committed"] and not receipt["backed_up"]
    rid = draft["record"]["record_id"]
    info = store.index()[rid]
    path = store.metadata / info["relative_path"]
    original = path.read_bytes()
    assert digest(original) == info["sha256"]
    r = store.read_record(rid)
    assert r["finalization"]["finalized_by"] == "Synthetic Tester"
    assert r["metadata"]["is_imaging_session"] is False
    session = store.read_record(r["session_id"])
    assert session["datasets"] == [{"record_id": rid, "sha256": info["sha256"]}]
    assert session["acquisition"] == r["acquisition"]
    assert "source_path" not in original.decode()
    assert sample.exists()
    with pytest.raises(LoggerError, match="already finalized"):
        store.save_draft(draft)
    with pytest.raises(LoggerError, match="Duplicate finalization"):
        store.finalize(draft, source_complete=True)
    store.amend(rid, "Tester", "Correct weight", {"metadata.mouse_weight_g": 25.2})
    assert len(store.amendments(rid)) == 1
    assert path.read_bytes() == original
    assert not store.git("status", "--porcelain")


@pytest.mark.parametrize("training_day", [0, 12])
def test_index_training_day_for_data_and_session(store, draft, training_day):
    draft["record"]["metadata"]["training_day"] = training_day
    receipt = store.finalize(draft, source_complete=True)
    bundle = store.metadata / receipt["record_directory"]
    original = {path: path.read_bytes() for path in bundle.glob("*.json")}
    index = store.index()
    assert {info["record_type"] for info in index.values()} == {"data", "session"}
    assert all(info["training_day"] == training_day for info in index.values())
    assert store.rebuild_index() == index
    assert all(path.read_bytes() == raw for path, raw in original.items())


def test_content_change_relink_root_relocation(store, draft, tmp_path):
    store.finalize(draft, source_complete=True)
    rid = draft["record"]["record_id"]
    r = store.read_record(rid)
    before = encode(r)
    f = r["files"][0]
    loc = store.file_locations(f["file_id"])[0]
    path = Path(loc["path"])
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"0.1", b"9.9", 1))
    assert store.verify_file(f["file_id"])[0]["verification_status"] == "mismatch"
    with pytest.raises(LoggerError, match="different content"):
        store.relink(f["file_id"], "archive", loc["relative_path"])
    path.write_bytes(raw)
    renamed = path.with_name("renamed.csv")
    path.rename(renamed)
    found = store.locate(f["file_id"], ["archive"])
    assert len(found) == 1
    store.relink(f["file_id"], **found[0])
    store.commit_catalog("Tester")
    moved_root = tmp_path / "relocated"
    shutil.copytree(store.roots["archive"], moved_root)
    store.roots["archive"] = str(moved_root)
    assert any(x["available"] for x in store.file_locations(f["file_id"]))
    assert store.verify_file(f["file_id"])[-1]["verification_status"] == "verified"
    assert encode(store.read_record(rid)) == before
    (store.metadata / "catalog.json").unlink()
    assert f["file_id"] in store.rebuild_catalog()["files"]
    assert rid in store.rebuild_index()


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "C:/x", "a/../../x", "a\\b", "a//b", "./a"])
def test_path_rejection(tmp_path, path):
    with pytest.raises(LoggerError):
        resolve(tmp_path, path)


def test_stale_save_lock_and_strict_json(store, draft):
    saved = store.save_draft(draft)
    assert saved["revision"] == 2
    with pytest.raises(LoggerError, match="Stale"):
        store.save_draft(draft)
    with pytest.raises(LoggerError, match="Stale"):
        store.finalize(draft, source_complete=True)
    with lock(store.metadata):
        with pytest.raises(LoggerError, match="Writer lock"):
            store.save_draft(saved)
    for raw in ('{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(LoggerError):
            loads(raw)
    invalid = deepcopy(draft["record"])
    invalid["created_at"] = "2026-10-15T12:00:00"
    assert any("created_at" in e for e in validate(invalid))


@pytest.mark.parametrize("phase", ["prepared", "published"])
def test_publication_recovery(store, draft, phase):
    with pytest.raises(LoggerError, match="Injected"):
        store.finalize(draft, source_complete=True, failpoint=phase)
    assert len(store.pending()) == 1
    if phase == "prepared":
        assert not store.index()
    result = store.recover()
    assert result[0]["git_committed"]
    assert len(store.index()) == 2
    assert not store.pending()


def test_git_failure_preserves_source_and_pending(store, draft, sample, monkeypatch):
    import errno
    from experiment_logger import transfers
    rename = transfers.rename_path
    def cross_device(source, destination):
        if Path(source) == sample.resolve():
            raise OSError(errno.EXDEV, 'Synthetic cross-device move')
        return rename(source, destination)
    monkeypatch.setattr(transfers, 'rename_path', cross_device)
    original_git = store.git
    def broken(*args):
        if "commit" in args:
            raise LoggerError("Synthetic Git failure")
        return original_git(*args)
    monkeypatch.setattr(store, "git", broken)
    result = store.finalize(draft, source_complete=True, move=True)
    assert result["finalized_locally"] and not result["git_committed"]
    assert sample.exists() and len(store.index()) == 2
    monkeypatch.setattr(store, "git", original_git)
    assert store.recover()[0]["git_committed"]
    assert not sample.exists(), "The explicitly authorized move completes only after verified recovery and Git commit"


def test_intended_git_paths_only(store, draft):
    unrelated = store.metadata / "unrelated.txt"
    unrelated.write_text("Must stay staged but uncommitted")
    store.git("add", "unrelated.txt")
    result = store.finalize(draft, source_complete=True)
    assert result["git_committed"]
    assert "unrelated.txt" not in store.git("ls-tree", "-r", "--name-only", "HEAD")
    assert "unrelated.txt" in store.git("diff", "--cached", "--name-only")


def test_approved_move_and_cancel(store, draft, sample):
    with pytest.raises(Cancelled):
        store.finalize(draft, source_complete=True, cancel=lambda: True)
    assert sample.exists() and not store.index()
    result = store.finalize(draft, source_complete=True, move=True)
    assert result["source_removed"] and not sample.exists()


def test_mapping_and_unstable_hash(sample):
    mapping, preview = inspect_csv(sample)
    assert len(mapping["columns"]) == 13
    assert mapping["columns"][0]["source_name"] is None
    assert mapping["time_reference"]["sample_interval_s"] is None
    assert mapping["row_count"] == 40
    def mutate(n, total):
        if n == total:
            with sample.open("ab") as f:
                f.write(b"1")
    with pytest.raises(LoggerError, match="changed during hashing"):
        hash_file(sample, mutate)


def test_same_name_different_bytes_and_multiple_replicas(store, draft, tmp_path):
    store.finalize(draft, source_complete=True)
    f = store.read_record(draft["record"]["record_id"])["files"][0]
    source = Path(store.file_locations(f["file_id"])[0]["path"])
    other = tmp_path / "selected-search-root"
    other.mkdir()
    store.roots["selected"] = str(other)
    bad = other / source.name
    bad.write_bytes(b"X" * source.stat().st_size)
    assert store.locate(f["file_id"], ["selected"]) == []
    for name in ("one.csv", "two.csv"):
        shutil.copyfile(source, other / name)
    assert len(store.locate(f["file_id"], ["selected"])) == 2


def test_record_tamper_detected(store, draft):
    store.finalize(draft, source_complete=True)
    rid = draft["record"]["record_id"]
    p = store.metadata / store.index()[rid]["relative_path"]
    p.write_bytes(p.read_bytes().replace(b"25.1", b"25.9"))
    assert store.index()[rid]["integrity"].startswith("INVALID")
    with pytest.raises(LoggerError, match="checksum mismatch"):
        store.read_record(rid)


def test_simultaneous_draft_saves(store, draft):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    gate = threading.Barrier(2)
    def save():
        gate.wait()
        try:
            return store.save_draft(deepcopy(draft))["revision"]
        except LoggerError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(save), pool.submit(save)
        assert sorted([str(a.result()), str(b.result())]) == ["2", "rejected"]
    assert store.load_draft(draft["record"]["record_id"])["revision"] == 2


def test_shared_repository_ownership_is_trusted_only_for_logger_commands(store, draft, tmp_path, monkeypatch):
    # Git's own test switch reproduces NAS ownership without changing file owners.
    global_config = tmp_path / "test-global-gitconfig"
    global_config.write_text("[user]\n\tname = Synthetic\n\temail = synthetic@localhost\n")
    before = global_config.read_bytes()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    blocked = subprocess.run(["git", "-c", "safe.directory=", "-C", str(store.metadata),
                              "status", "--porcelain"], capture_output=True, text=True)
    assert blocked.returncode != 0 and "dubious ownership" in blocked.stderr

    # Retrying Settings must finish setup even after the earlier partial init.
    (store.metadata / "logger_repository.json").unlink()
    store.initialize()
    assert store.git("rev-parse", "--show-toplevel").replace("\\", "/").casefold() == store.metadata.as_posix().casefold()
    receipt = store.finalize(draft, source_complete=True)
    assert receipt["git_committed"]
    assert store.git("config", "--get-all", "safe.directory").splitlines() == [store.metadata.as_posix()]
    assert global_config.read_bytes() == before
    still_blocked = subprocess.run(["git", "-c", "safe.directory=", "-C", str(store.metadata),
                                    "status", "--porcelain"], capture_output=True, text=True)
    assert still_blocked.returncode != 0 and "dubious ownership" in still_blocked.stderr
