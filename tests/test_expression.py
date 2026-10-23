"""Expression registration uses generated TIFFs and temporary repositories only."""
from copy import deepcopy
from pathlib import Path
import struct

import pytest

from experiment_logger.expression import CHANNELS, mouse_folders, selection, inspect_tiff
from experiment_logger.io import LoggerError, Cancelled, atomic_json, digest, read_json
from experiment_logger.models import RESOURCE, SCHEMA_VERSION, validate


def write_tiff(path, pixel=1):
    """A valid synthetic 1x1 grayscale classic TIFF, without imaging libraries."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = [(256, 4, 1), (257, 4, 1), (258, 3, 8), (259, 3, 1),
            (262, 3, 1), (273, 4, 122), (277, 3, 1), (278, 4, 1), (279, 4, 1)]
    body = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", len(tags))
    for tag, kind, value in tags:
        body += struct.pack("<HHII", tag, kind, 1, value)
    body += struct.pack("<I", 0) + bytes([pixel])
    assert len(body) == 123
    path.write_bytes(body)
    return path


@pytest.fixture
def expression_folder(tmp_path):
    folder = tmp_path / "source" / "261021" / "007"
    for name, pixel in [("green_001.tif", 1), ("red_002.TIF", 2),
                        ("right_003.tif", 3), ("right_004.tiff", 4), ("extra_005.tif", 5)]:
        write_tiff(folder / name, pixel)
    (folder / "notes.txt").write_text("SYNTHETIC folder note, not registered", encoding="utf-8")
    return folder


@pytest.fixture
def expression_draft(store, expression_folder):
    draft = store.new_expression(expression_folder)
    draft["record"]["operator"] = "Synthetic Expression Tester"
    draft["record"]["notes"] = "SYNTHETIC expression check"
    return store.save_draft(draft)


def test_expression_discovery_multiple_missing_channels_and_day_folder(store, expression_folder):
    d = store.new_expression(expression_folder)
    assert d["record"]["mouse_id"] == "007"
    assert d["record"]["session_date"] == "2026-10-21"
    assert {k: len(v) for k, v in d["channel_paths"].items()} == {"left_red": 1, "left_green": 1, "right": 2, "other": 1}
    assert all(channel["selected"] for channel in d["record"]["metadata"]["channels"].values())
    other = expression_folder.parent / "mouse_008"
    write_tiff(other / "green_100.tif")
    assert mouse_folders(expression_folder) == [expression_folder]
    assert set(mouse_folders(expression_folder.parent)) == {expression_folder, other}
    d2 = store.new_expression(other)
    assert d2["record"]["mouse_id"] == "008" and not d2["record"]["metadata"]["channels"]["right"]["selected"]
    assert d2["channel_paths"]["right"] == []


def test_expression_finalize_copy_paths_seals_and_git(store, expression_folder, expression_draft):
    original = {p: p.read_bytes() for p in expression_folder.glob("*") if p.is_file()}
    assert store.load_draft(expression_draft["record"]["record_id"]) == expression_draft
    ticks = []
    receipt = store.finalize_expression(expression_draft, source_complete=True, progress=lambda n, total: ticks.append((n, total)))
    assert receipt["git_committed"] and not receipt["source_removed"] and not receipt["backed_up"]
    assert receipt["record_directory"] == "records/mouse_007/261021/expression_check_1"
    bundle = store.metadata / receipt["record_directory"]
    assert {p.name for p in bundle.iterdir()} == {"expression.json", "session.json", "manifest.json"}
    data = store.read_record(expression_draft["record"]["record_id"])
    assert data["data_type"] == "expression" and not validate(data, True)
    assert len(data["files"]) == 5
    assert len(data["metadata"]["channels"]["right"]["file_ids"]) == 2
    assert data["notes"] == "SYNTHETIC expression check"
    session = store.read_record(data["session_id"])
    assert session["training_day"] is None
    assert all(info["data_type"] == "expression" for info in store.index().values())
    assert session["datasets"] == [{"record_id": data["record_id"], "sha256": digest((bundle / "expression.json").read_bytes())}]
    for f in data["files"]:
        locations = store.file_locations(f["file_id"])
        relative = locations[0]["relative_path"]
        expected_directory = CHANNELS[f["role"]][1]
        assert relative == f"expression/mouse_007/261021/{expected_directory}/{f['original_filename']}"
        target = Path(locations[0]["path"])
        assert locations[0]["available"] and digest(target.read_bytes()) == f["sha256"]
        assert store.verify_file(f["file_id"])[0]["verification_status"] == "verified"
    assert all(p.read_bytes() == content for p, content in original.items())
    assert ticks[-1][0] == ticks[-1][1] and [n for n, _ in ticks] == sorted(n for n, _ in ticks)
    tracked = store.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert not any(name.lower().endswith((".tif", ".tiff")) for name in tracked)
    assert str(expression_folder) not in (bundle / "expression.json").read_text(encoding="utf-8")
    with pytest.raises(LoggerError, match="Duplicate finalization"):
        store.finalize_expression(expression_draft, source_complete=True)
    with pytest.raises(LoggerError, match="CSV preview"):
        store.preview(data["record_id"])


def test_unchecked_channels_are_not_copied_or_registered(store, expression_draft):
    d = deepcopy(expression_draft)
    for key in ("right", "other"):
        d["record"]["metadata"]["channels"][key]["selected"] = False
    receipt = store.finalize(d, source_complete=True)
    record = store.read_record(d["record"]["record_id"])
    assert {f["role"] for f in record["files"]} == {"left_red", "left_green"}
    assert not record["metadata"]["channels"]["right"]["file_ids"]
    assert not (Path(store.roots["archive"]) / "expression/mouse_007/261021/right").exists()
    assert len(receipt["files"]) == 2


def test_filename_collisions_preserve_existing_tiffs_and_origins(store, expression_draft):
    target = Path(store.roots["archive"]) / "expression/mouse_007/261021/left/green/green_001.tif"
    write_tiff(target, 200)
    before = target.read_bytes()
    plan = store.plan_expression(expression_draft)
    green = next(item for item in plan["items"] if item["channel"] == "left_green")
    assert green["relative_path"].endswith("green_001__2.tif")
    result = store.finalize_expression(expression_draft, source_complete=True)
    assert target.read_bytes() == before
    assert (target.parent / "green_001__2.tif").read_bytes() != before
    assert any(f["relative_path"].endswith("green_001__2.tif") for f in result["files"])


@pytest.mark.parametrize("problem", ["missing", "invalid", "duplicate", "empty", "relative"])
def test_expression_preflight_rejects_invalid_selections(store, expression_draft, problem, tmp_path):
    d = deepcopy(expression_draft)
    if problem == "missing":
        d["channel_paths"]["left_red"] = [str(tmp_path / "missing.tif")]
    elif problem == "invalid":
        p = tmp_path / "invalid.tif"
        p.write_bytes(b"not a TIFF")
        d["channel_paths"]["left_red"] = [str(p)]
    elif problem == "duplicate":
        d["channel_paths"]["right"].append(d["channel_paths"]["left_red"][0])
    elif problem == "empty":
        d["channel_paths"]["left_red"] = []
    else:
        d["channel_paths"]["left_red"] = ["relative.tif"]
    with pytest.raises(LoggerError):
        store.finalize_expression(d, source_complete=True)
    assert not store.index()
    assert not (Path(store.roots["archive"]) / "expression").exists()


def test_expression_requires_closed_files_and_selected_channel(store, expression_draft):
    with pytest.raises(LoggerError, match="Confirm acquisition"):
        store.finalize_expression(expression_draft)
    for channel in expression_draft["record"]["metadata"]["channels"].values():
        channel["selected"] = False
    with pytest.raises(LoggerError, match="Select at least one"):
        selection(expression_draft)


def test_stale_expression_draft_and_source_change_block_publication(store, expression_draft):
    newer = store.save_draft(expression_draft)
    with pytest.raises(LoggerError, match="Stale"):
        store.finalize_expression(expression_draft, source_complete=True)
    plan = store.plan_expression(newer)
    write_tiff(plan["items"][0]["source"], 222)
    with pytest.raises(LoggerError, match="Source changed"):
        store.finalize_expression(newer, source_complete=True)
    assert not store.index()


def test_expression_copy_interruption_reuses_exact_paths_and_hashes(store, expression_draft):
    plan = store.plan_expression(expression_draft)
    with pytest.raises(LoggerError, match="Injected interruption"):
        store.finalize_expression(expression_draft, source_complete=True, failpoint="copied")
    first = Path(store.roots["archive"]) / plan["items"][0]["relative_path"]
    assert first.exists() and not store.index()
    before = first.stat().st_mtime_ns
    receipt = store.finalize_expression(expression_draft, source_complete=True)
    assert receipt["git_committed"] and first.stat().st_mtime_ns == before
    assert not list(first.parent.glob("*__2*"))


def test_expression_cancellation_cleans_partial_and_retains_sources(store, expression_draft):
    cancelled = False
    def progress(n, total):
        nonlocal cancelled
        if n:
            cancelled = True
    with pytest.raises(Cancelled):
        store.finalize_expression(expression_draft, source_complete=True, progress=progress, cancel=lambda: cancelled)
    assert not store.index()
    assert all(Path(p).exists() for files in expression_draft["channel_paths"].values() for p in files)
    assert not list(Path(store.roots["archive"]).rglob("*.partial"))
    assert store.finalize_expression(expression_draft, source_complete=True)["git_committed"]


@pytest.mark.parametrize("phase", ["prepared", "published"])
def test_expression_publication_recovery(store, expression_draft, phase):
    with pytest.raises(LoggerError, match="Injected interruption"):
        store.finalize_expression(expression_draft, source_complete=True, failpoint=phase)
    assert store.pending()
    assert store.recover()[0]["git_committed"]
    assert all(info["integrity"] == "ok" for info in store.index().values())
    assert not store.pending()


def test_expression_git_failure_preserves_completed_record(store, expression_draft, monkeypatch):
    original = store.git
    def fail(*args):
        if "commit" in args:
            raise LoggerError("Synthetic Git failure")
        return original(*args)
    monkeypatch.setattr(store, "git", fail)
    receipt = store.finalize_expression(expression_draft, source_complete=True)
    assert receipt["finalized_locally"] and not receipt["git_committed"] and store.pending()
    assert len(store.read_record(expression_draft["record"]["record_id"])["files"]) == 5
    monkeypatch.setattr(store, "git", original)
    assert store.recover()[0]["git_committed"]


def test_expression_tampering_and_relink_keep_frozen_json(store, expression_draft):
    receipt = store.finalize_expression(expression_draft, source_complete=True)
    record = store.read_record(expression_draft["record"]["record_id"])
    scientific = store.metadata / receipt["record_directory"] / "expression.json"
    before = scientific.read_bytes()
    f = record["files"][0]
    path = Path(store.file_locations(f["file_id"])[0]["path"])
    raw = path.read_bytes()
    path.write_bytes(raw + b"changed")
    assert store.verify_file(f["file_id"])[0]["verification_status"] == "mismatch"
    path.write_bytes(raw)
    renamed = path.with_name("relocated.tif")
    path.rename(renamed)
    relative = renamed.relative_to(store.roots["archive"]).as_posix()
    store.relink(f["file_id"], "archive", relative)
    assert scientific.read_bytes() == before
    assert all(i["integrity"] == "ok" for i in store.rebuild_index().values())
    assert not store.plan_readable_migration()["csv_moves"]


def test_plan_can_be_edited_before_copying_and_rejects_later_changes(store, expression_draft):
    first = store.plan_expression(expression_draft)
    edited = deepcopy(expression_draft)
    edited["record"]["metadata"]["channels"]["right"]["selected"] = False
    second = store.plan_expression(edited)
    assert len(second["items"]) == len(first["items"]) - 2
    with pytest.raises(LoggerError, match="Injected interruption"):
        store.finalize_expression(edited, source_complete=True, failpoint="copied")
    with pytest.raises(LoggerError, match="selections changed after copying"):
        store.plan_expression(expression_draft)


def test_old_mouse_profile_upgrades_only_on_explicit_save(store):
    from experiment_logger.colony import new_profile
    from experiment_logger.io import encode
    schema_path = store.metadata / "schemas/record.json"
    historical = read_json(schema_path)
    historical["properties"]["schema_version"]["const"] = "1.2.0"
    historical["definitions"]["mouse_profile"]["properties"]["schema_version"]["const"] = "1.2.0"
    atomic_json(schema_path, historical)
    profile = new_profile("007", "Synthetic")
    profile.update(schema_version="1.2.0", revision=1)
    path = store.metadata / "records/mouse_007/mouse_info.json"
    atomic_json(path, profile)
    before = path.read_bytes()
    store.initialize()
    loaded = store.load_mouse_profile("007")
    assert loaded["schema_version"] == SCHEMA_VERSION and loaded["revision"] == 1
    assert path.read_bytes() == before
    result = store.save_mouse_profile(loaded, "Synthetic")
    assert result["receipt"]["git_committed"] and result["profile"]["revision"] == 2
    assert result["profile"]["profile_id"] == profile["profile_id"]
    assert read_json(store.metadata / "schemas/record_v1.2.0.json") == historical


def test_expression_cli_draft_and_finalize(store, expression_folder, tmp_path, capsys):
    from experiment_logger.cli import main
    from experiment_logger.io import loads
    config = tmp_path / "cli-local/config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic CLI"})
    args = ["--config", str(config)]
    assert main([*args, "expression-draft", str(expression_folder)]) == 0
    draft = loads(capsys.readouterr().out)
    assert draft["record"]["data_type"] == "expression" and draft["revision"] == 1
    assert main([*args, "finalize", draft["record"]["record_id"], "--source-complete"]) == 0
    result = loads(capsys.readouterr().out)
    assert result["git_committed"] and len(result["files"]) == 5


def test_expression_gui_drop_edit_save_finalize_and_browse(store, expression_folder, tmp_path, monkeypatch):
    import json
    from PySide6.QtCore import QMimeData, QUrl, QPointF, Qt
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    app = QApplication.instance() or QApplication([])
    config = tmp_path / "expression-ui/config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic UI"})
    errors = []
    confirmations = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    def yes(*args):
        confirmations.append(args[-1])
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", yes)
    w = Window(config)
    w.show()
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(expression_folder))])
        event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        w.expression_drop.dropEvent(event)
        assert w.tabs.currentWidget() == w.expression_page
        assert w.expression_mouse.text() == "007" and w.expression_date.text() == "2026-10-21"
        assert w.expression_checks["right"].isChecked()
        assert len(w.expression_paths["right"].toPlainText().splitlines()) == 2
        w.expression_checks["other"].setChecked(False)
        w.expression_notes.setPlainText("SYNTHETIC optional expression note")
        # The user can replace a detected path before saving.
        replacement = write_tiff(tmp_path / "edited/green_replacement.tif", 99)
        w.expression_paths["left_green"].setPlainText(str(replacement))
        w.validate_expression_form()
        assert "4 TIFF signature(s) are valid" in w.expression_messages.text()
        w.save_expression_draft()
        assert w.expression_draft["revision"] == 1
        assert w.expression_draft_picker.count() == 1 and w.draft_picker.count() == 0
        w.open_expression_draft()
        assert w.expression_paths["left_green"].toPlainText() == str(replacement)
        assert not w.expression_checks["other"].isChecked()
        w.expression_complete.setChecked(True)
        rid = w.expression_draft["record"]["record_id"]
        w.finalize_expression_form()
        wait_job(app, w)
        assert not errors
        assert "Copied 4 TIFF(s)" in w.expression_messages.text()
        assert "left/green" in confirmations[-1].replace("\\", "/") and "Original files will be retained" in confirmations[-1]
        record = w.store.read_record(rid)
        assert record["notes"] == "SYNTHETIC optional expression note"
        assert len(record["files"]) == 4
        w.filter_data_type.setCurrentText("expression")
        assert w.records.rowCount() == 2  # Both the dataset and its linked session.
        w.filter_kind.setCurrentText("data")
        assert w.records.rowCount() == 1
        w.records.selectRow(0)
        app.processEvents()
        assert json.loads(w.raw.toPlainText())["record"]["data_type"] == "expression"
        assert w.expression_files.rowCount() == 4 and w.csv_table.rowCount() == 0
        assert replacement.exists()
    finally:
        w.close()


def test_expression_gui_date_folder_offers_mouse_choice(store, expression_folder, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from experiment_logger.gui import Window
    app = QApplication.instance() or QApplication([])
    second = expression_folder.parent / "008"
    write_tiff(second / "green_010.tif")
    config = tmp_path / "date-ui/config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic UI"})
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    w = Window(config)
    try:
        w.load_expression_folder(str(expression_folder.parent))
        assert w.expression_mouse_picker.count() == 2
        assert w.expression_draft is None
        w.expression_mouse_picker.setCurrentIndex(1)
        assert w.expression_mouse.text() == "008"
        assert w.expression_checks["left_green"].isChecked()
        assert not w.expression_checks["right"].isChecked()
        assert not errors
    finally:
        w.close()
