"""Meaningful storage-format, compatibility, numbering, and migration checks."""
from copy import deepcopy
from pathlib import Path
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiment_logger.io import LoggerError, atomic_json, digest, encode, read_json, uid
from experiment_logger.layout import bundle_entries
from experiment_logger.models import new_task, validate, SCHEMA_VERSION


def legacy_bundle(store, draft):
    """Produce an actual four-file v1 bundle in temporary storage only."""
    receipt = store.finalize(draft, source_complete=True)
    new = store.metadata / receipt["record_directory"]
    pairs = [(p.read_bytes(), read_json(p), seal) for p, seal in bundle_entries(new)]
    old_id = uid()
    old = store.metadata / "records" / old_id
    old.mkdir()
    for raw, r, seal in pairs:
        (old / f"{r['record_id']}.json").write_bytes(raw)
        atomic_json(old / f"{r['record_id']}.seal.json", {k: v for k, v in seal.items() if k != "filename"})
    for p in new.iterdir():
        p.unlink()
    new.rmdir()
    catalog = store._catalog()
    data = next(r for _, r, _ in pairs if r["record_type"] == "data")
    f = data["files"][0]
    loc = catalog["files"][f["file_id"]]["locations"][0]
    old_csv_relative = f"csv/{data['mouse_id']}_{data['session_date']}/{data['session_id']}/{f['original_filename']}"
    source = Path(store.roots["archive"]) / loc["relative_path"]
    target = Path(store.roots["archive"]) / old_csv_relative
    target.parent.mkdir(parents=True)
    source.rename(target)
    source.parent.rmdir()
    loc["relative_path"] = old_csv_relative
    store._write_catalog(catalog)
    store.git("add", "--", "records", "catalog.json", "catalog_history")
    store.git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@localhost", "commit", "-m", "Synthetic legacy fixture")
    return old, {r["record_id"]: raw for raw, r, _ in pairs}, f["file_id"]


def test_custom_task_and_three_file_readable_bundle(store, draft):
    draft["record"]["experiment_type"] = "User choice / task: A"
    result = store.finalize(draft, source_complete=True)
    assert result["session_number"] == 1
    assert result["csv_relative_path"] == "robot-csv/mouse_007/261015/1/001.csv"
    assert result["record_directory"] == "records/mouse_007/261015/user_choice_task_a_1"
    bundle = store.metadata / result["record_directory"]
    assert {p.name for p in bundle.iterdir()} == {"task.json", "session.json", "manifest.json"}
    r = store.read_record(draft["record"]["record_id"])
    assert r["schema_version"] == SCHEMA_VERSION
    assert r["experiment_type"] == "User choice / task: A"
    assert store.read_record(r["session_id"])["experiment_type"] == r["experiment_type"]
    manifest = read_json(bundle / "manifest.json")
    assert manifest["version"] == 2
    for seal in manifest["records"]:
        assert digest((bundle / seal["filename"]).read_bytes()) == seal["sha256"]
        assert seal["schema_sha256"] == digest((store.metadata / "schemas" / "record.json").read_bytes())


def test_counter_is_shared_across_task_types_and_past_nine(store, draft):
    first = store.reserve_layout(draft["record"])
    assert first["number"] == 1
    assert store.reserve_layout(draft["record"]) == first
    for number in range(2, 11):
        r = deepcopy(draft["record"])
        r.update(record_id=uid(), session_id=uid(), experiment_type="Custom task" if number % 2 else "water two-angle push")
        assert store.reserve_layout(r)["number"] == number
    reopened = type(store)(store.metadata, store.roots, store.local)
    r["session_id"] = uid()
    assert reopened.reserve_layout(r)["number"] == 11


def test_simultaneous_number_reservations(store, draft):
    barrier = threading.Barrier(2)
    def reserve():
        r = deepcopy(draft["record"])
        r["session_id"] = uid()
        barrier.wait()
        try:
            return store.reserve_layout(r)["number"]
        except LoggerError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(reserve), pool.submit(reserve)
        values = [a.result(), b.result()]
    numbers = [n for n in values if n is not None]
    assert numbers and len(numbers) == len(set(numbers))


def test_inference_supports_mouse_prefixed_and_old_folders(tmp_path):
    for mouse in ("007", "mouse007", "mouse_007"):
        r = new_task(tmp_path / "261015" / mouse / "001.csv")
        assert r["mouse_id"] == "007" and r["session_date"] == "2026-10-15"
    nested = new_task(tmp_path / "robot-csv" / "mouse_007" / "261015" / "1" / "001.csv")
    assert nested["mouse_id"] == "007" and nested["session_date"] == "2026-10-15"


def test_folder_selection_uses_new_names_without_renaming_existing_archives(tmp_path):
    from experiment_logger.layout import metadata_directory
    assert metadata_directory(tmp_path) == tmp_path / "metadata-json"
    legacy = tmp_path / "metadata"
    legacy.mkdir()
    atomic_json(legacy / "logger_repository.json", {"format": "mouse-logger-v1"})
    assert metadata_directory(tmp_path) == legacy
    assert not (tmp_path / "metadata-json").exists()
    (tmp_path / "metadata-json").mkdir()
    assert metadata_directory(tmp_path) == tmp_path / "metadata-json"


def test_counter_respects_both_old_and_new_csv_roots(store, draft):
    (Path(store.roots["archive"]) / "csv" / "007_2026-10-15" / "7").mkdir(parents=True)
    (Path(store.roots["archive"]) / "robot-csv" / "mouse007_2026-10-15" / "8").mkdir(parents=True)
    assert store.reserve_layout(draft["record"])["number"] == 9


@pytest.mark.parametrize("text", ["", "   ", "bad\ntask", "bad\x00task", "x" * 121])
def test_invalid_custom_task_types(draft, text):
    draft["record"]["experiment_type"] = text
    assert any("experiment_type" in e for e in validate(draft["record"], True))


def test_legacy_index_and_migration_preview_are_read_only(store, draft):
    old, records, fid = legacy_bundle(store, draft)
    before = {p.relative_to(store.metadata).as_posix(): p.read_bytes()
              for p in store.metadata.rglob("*") if p.is_file() and ".git" not in p.parts}
    plan = store.plan_readable_migration()
    after = {p.relative_to(store.metadata).as_posix(): p.read_bytes()
             for p in store.metadata.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert before == after
    assert len(plan["bundles"]) == 1 and len(plan["csv_moves"]) == 1
    assert plan["bundles"][0]["new_directory"].startswith("records/mouse_007/261015/")
    assert plan["csv_moves"][0]["new_relative_path"] == "robot-csv/mouse_007/261015/1/001.csv"
    for rid, raw in records.items():
        assert encode(store.read_record(rid)) == raw


@pytest.mark.parametrize("interruption", [None, "csv_moved", "metadata_backed_up"])
def test_migration_preserves_exact_records_and_recovers(store, draft, interruption):
    old, records, fid = legacy_bundle(store, draft)
    plan = store.plan_readable_migration()
    if interruption:
        with pytest.raises(LoggerError, match="Injected migration"):
            store.migrate_paths(plan, "Synthetic", failpoint=interruption)
        assert store.pending_migrations()
        result = store.recover()[0]
    else:
        result = store.migrate_paths(plan, "Synthetic")
    assert result["git_committed"] and not store.pending_migrations()
    assert not old.exists()
    new = store.metadata / plan["bundles"][0]["new_directory"]
    assert {p.name for p in new.iterdir()} == {"task.json", "session.json", "manifest.json"}
    for rid, raw in records.items():
        assert encode(store.read_record(rid)) == raw
        assert (store.metadata / store.index()[rid]["relative_path"]).read_bytes() == raw
    assert store.file_locations(fid)[0]["available"]
    assert store.verify_file(fid)[0]["verification_status"] == "verified"
    assert not store.plan_readable_migration()["bundles"]


def test_migration_rejects_stale_preview_and_collisions(store, draft):
    old, records, fid = legacy_bundle(store, draft)
    plan = store.plan_readable_migration()
    target = Path(store.roots["archive"]) / plan["csv_moves"][0]["new_relative_path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"different bytes")
    with pytest.raises(LoggerError, match="destination already exists"):
        store.migrate_paths(plan, "Synthetic")
    assert old.exists() and target.read_bytes() == b"different bytes"
    target.unlink()
    store.verify_file(fid)
    with pytest.raises(LoggerError, match="stale"):
        store.migrate_paths(plan, "Synthetic")


def test_original_schema_and_old_publication_journals_remain_supported(store, synthetic_task_record):
    from experiment_logger.models import session_for
    data = deepcopy(synthetic_task_record)
    # Simulate a historical archive; no obsolete schema ships with the app.
    data["schema_version"] = "1.0.0"
    raw = encode(data)
    historical_schema = read_json(store.metadata / "schemas" / "record.json")
    historical_schema["properties"]["schema_version"]["const"] = "1.0.0"
    historical_schema["properties"]["experiment_type"] = {"enum": [None, "water linear push", "water two-angle push"]}
    atomic_json(store.metadata / "schemas" / "historical_record.json", historical_schema)
    old_schema = (store.metadata / "schemas" / "historical_record.json").read_bytes()
    session = session_for(data, digest(raw))
    session["schema_version"] = "1.0.0"
    tx = uid()
    stage = store.metadata / ".staging" / tx
    stage.mkdir()
    for r, bytes_ in ((data, raw), (session, encode(session))):
        (stage / f"{r['record_id']}.json").write_bytes(bytes_)
        atomic_json(stage / f"{r['record_id']}.seal.json", store._seal(r, bytes_))
    files = [{**f, "locations": []} for f in data["files"]]
    atomic_json(store.metadata / ".operations" / f"{tx}.json", {
        "transaction_id": tx, "record_ids": [data["record_id"], session["record_id"]],
        "phase": "prepared", "catalog_entries": files, "author": "Synthetic", "created_at": data["created_at"]})
    assert store.recover()[0]["git_committed"]
    assert store.read_record(data["record_id"]) == data
    assert (store.metadata / "records" / tx / f"{data['record_id']}.json").read_bytes() == raw
    assert (store.metadata / "schemas" / "historical_record.json").read_bytes() == old_schema
