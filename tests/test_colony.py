"""Synthetic workbook lookup, explicit identity decisions, and profile recovery."""
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile
from xml.sax.saxutils import escape

import pytest

from experiment_logger.colony import new_profile, search_workbook, import_candidate, require_profile
from experiment_logger.io import LoggerError, atomic_json, digest, encode, read_json, uid


@pytest.fixture
def workbook(tmp_path):
    """Minimal XLSX fixture, including the real export's incorrect A1 dimension."""
    path = tmp_path / "synthetic_colony.xlsx"
    rows = [
        ["Use", "Strain", "Sex", "DOB", "Mouse ID", "Genotype", "Cage ID", "Notes", "SystemId"],
        [None, "Strain: synthetic", None, None, "Count: 3"],
        ["Training", "Synthetic strain A", "Female", "04/06/2025", "G107", "Cre HET", "00374", "\nSYNTHETIC injection note", "system-A"],
        ["Training", "Synthetic strain B", "Male", "2025-05-01", "107", None, "00200", "Another mouse", "system-B"],
        [None, "Synthetic strain C", "Female", None, "T107", None, None, None, "system-C"],
        [None, "Synthetic strain D", "Female", "04/06/2025", "G109", None, None, None, "system-D"],
        [None, "Synthetic strain E", "Male", None, "007", None, None, None, "system-E"],
    ]
    sheet = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData>']
    for number, row in enumerate(rows, 1):
        sheet.append(f'<row r="{number}">')
        for col, value in enumerate(row):
            if value is not None:
                cell = f"{chr(65 + col)}{number}"
                sheet.append(f'<c r="{cell}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        sheet.append('</row>')
    sheet.append('</sheetData></worksheet>')
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Colony" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet))
    return path


def test_lookup_duplicates_prefixes_and_broken_dimensions(workbook):
    before = workbook.read_bytes()
    matches = search_workbook(workbook, "107")
    assert [m["colony_id"] for m in matches] == ["G107", "107", "T107"]
    assert all(m["requires_confirmation"] for m in matches)
    assert matches[0]["source"]["row"] == 3
    assert matches[0]["source"]["cells"]["date_of_birth"] == "D3"
    assert matches[0]["fields"]["date_of_birth"] == "2025-04-06"
    assert matches[0]["fields"]["cage_id"] == "00374"
    assert matches[0]["source"]["workbook_sha256"] == digest(before)
    assert search_workbook(workbook, "109")[0]["requires_confirmation"]
    assert not search_workbook(workbook, "007")[0]["requires_confirmation"]
    assert not search_workbook(workbook, "999")
    assert workbook.read_bytes() == before


def test_import_requires_selection_and_preserves_manual_info(workbook):
    profile = new_profile("107", "Synthetic")
    profile["notes"] = "Manually entered note"
    profile["surgeries"] = [{"surgery_id": uid(), "date": "2026-10-01", "details": "Synthetic window surgery", "surgeon": "Tester"}]
    candidate = search_workbook(workbook, "107")[0]
    with pytest.raises(LoggerError, match="Select and confirm"):
        import_candidate(profile, candidate, "Synthetic")
    imported = import_candidate(profile, candidate, "Synthetic", confirmed=True)
    assert profile["colony_id"] is None
    assert imported["mouse_id"] == "107" and imported["colony_id"] == "G107"
    assert imported["notes"] == profile["notes"] and imported["surgeries"] == profile["surgeries"]
    assert len(imported["surgeries"]) == 1  # Injection text never creates surgeries.
    assert imported["import_history"][0]["source"]["source_values"]["DOB"] == "04/06/2025"
    assert imported["import_history"][0]["source"]["source_values"]["Notes"] == "\nSYNTHETIC injection note"
    assert imported["import_history"][0]["imported_values"]["date_of_birth"] == "2025-04-06"
    with pytest.raises(LoggerError, match="another local mouse"):
        import_candidate(new_profile("109", "Synthetic"), candidate, "Synthetic", confirmed=True)


def test_profile_save_stale_edit_and_task_integrity(store, draft, workbook):
    receipt = store.finalize(draft, source_complete=True)
    task_paths = {p: p.read_bytes() for p in (store.metadata / receipt["record_directory"]).glob("*.json")}
    profile = import_candidate(new_profile("107", "Synthetic"), search_workbook(workbook, "107")[0], "Synthetic", confirmed=True)
    saved = store.save_mouse_profile(profile, "Synthetic")
    assert saved["receipt"]["git_committed"] and not saved["receipt"]["backed_up"]
    target = store.metadata / "records" / "mouse_107" / "mouse_info.json"
    assert read_json(target) == saved["profile"] == store.load_mouse_profile("107")
    assert saved["profile"]["revision"] == 1
    with pytest.raises(LoggerError, match="Stale"):
        store.save_mouse_profile(profile, "Synthetic")
    edit = deepcopy(saved["profile"])
    edit["notes"] = "Changed manual note"
    saved2 = store.save_mouse_profile(edit, "Synthetic")
    assert saved2["profile"]["revision"] == 2
    assert len(store.index()) == 2
    assert all(p.read_bytes() == raw for p, raw in task_paths.items())
    assert len(store.git("log", "--format=%H", "--", "records/mouse_107/mouse_info.json").splitlines()) == 2


def test_profile_first_commit_excludes_unrelated_staged_changes(store):
    unrelated = store.metadata / "unrelated.txt"
    unrelated.write_text("unrelated staged text", encoding="utf-8")
    store.git("add", "--", unrelated.name)
    result = store.save_mouse_profile(new_profile("007", "Synthetic"), "Synthetic")
    assert result["receipt"]["git_committed"]
    assert unrelated.name not in store.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert unrelated.name in store.git("diff", "--cached", "--name-only").splitlines()


def test_profile_commit_failure_preserves_and_recovers(store, monkeypatch):
    original_git = store.git
    def fail_commit(*args):
        if "commit" in args:
            raise LoggerError("Synthetic commit failure")
        return original_git(*args)
    monkeypatch.setattr(store, "git", fail_commit)
    result = store.save_mouse_profile(new_profile("007", "Synthetic"), "Synthetic")
    assert result["receipt"]["saved_locally"] and not result["receipt"]["git_committed"]
    assert store.load_mouse_profile("007")["revision"] == 1
    with pytest.raises(LoggerError, match="pending profile"):
        store.save_mouse_profile(result["profile"], "Synthetic")
    monkeypatch.setattr(store, "git", original_git)
    assert store.recover()[0]["receipt"]["git_committed"]
    assert not store.pending_profiles()


def test_interrupted_profile_save_and_conflict_detection(store):
    with pytest.raises(LoggerError, match="Injected profile"):
        store.save_mouse_profile(new_profile("007", "Synthetic"), "Synthetic", failpoint="prepared")
    assert store.load_mouse_profile("007") is None
    target = store.metadata / "records" / "mouse_007" / "mouse_info.json"
    atomic_json(target, {"unexpected": True})
    with pytest.raises(LoggerError, match="outside the pending"):
        store.recover()
    target.unlink()
    assert store.recover()[0]["profile"]["revision"] == 1


@pytest.mark.parametrize("field,value", [("date_of_birth", "2026-02-30"), ("mouse_id", "G107"), ("surgeries", [None])])
def test_invalid_profiles_rejected(store, field, value):
    profile = new_profile("107", "Synthetic")
    profile[field] = value
    with pytest.raises(LoggerError, match=field):
        store.save_mouse_profile(profile, "Synthetic")
    assert not store.pending_profiles()


def test_profile_surgery_and_history_validation(store, workbook):
    profile = import_candidate(new_profile("107", "Synthetic"), search_workbook(workbook, "107")[0], "Synthetic", confirmed=True)
    profile["surgeries"] = [{"surgery_id": uid(), "date": "2025-01-01", "details": "Before DOB", "surgeon": None}]
    with pytest.raises(LoggerError, match="precedes date of birth"):
        require_profile(profile)
    profile["surgeries"][0]["date"] = "2026-10-15"
    saved = store.save_mouse_profile(profile, "Synthetic")["profile"]
    saved["import_history"] = []
    with pytest.raises(LoggerError, match="import history"):
        store.save_mouse_profile(saved, "Synthetic")


def test_schema_upgrade_keeps_historical_rulebook(store):
    current = read_json(store.metadata / "schemas" / "record.json")
    historical = deepcopy(current)
    historical["properties"]["schema_version"]["const"] = "1.1.0"
    atomic_json(store.metadata / "schemas" / "record.json", historical)
    before = (store.metadata / "schemas" / "record.json").read_bytes()
    store.initialize()
    assert (store.metadata / "schemas" / "record_v1.1.0.json").read_bytes() == before
    assert read_json(store.metadata / "schemas" / "record.json") == current


def test_nested_counters_and_missing_manifest_integrity(store, draft):
    (Path(store.roots["archive"]) / "robot-csv" / "mouse_007" / "261015" / "8").mkdir(parents=True)
    receipt = store.finalize(draft, source_complete=True)
    assert receipt["session_number"] == 9
    (store.metadata / receipt["record_directory"] / "manifest.json").unlink()
    assert all(info["integrity"].startswith("INVALID") for info in store.index().values())


def test_import_history_cannot_cross_mice_or_repeat_ids(workbook):
    candidate = search_workbook(workbook, "107")[0]
    profile = import_candidate(new_profile("107", "Synthetic"), candidate, "Synthetic", confirmed=True)
    profile["import_history"][0]["matched_mouse_id"] = "109"
    with pytest.raises(LoggerError, match="does not match local mouse"):
        require_profile(profile)
    profile["import_history"][0]["matched_mouse_id"] = "107"
    profile["import_history"].append(deepcopy(profile["import_history"][0]))
    with pytest.raises(LoggerError, match="duplicate import IDs"):
        require_profile(profile)


def test_mouse_colony_ui_requires_row_choice_and_saves(store, workbook, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    app = QApplication.instance() or QApplication([])
    config = tmp_path / "colony-ui" / "config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic UI Tester"})
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    w = Window(config)
    w.show()
    archive_scans = []
    original_index = w.store.index
    def counted_index():
        archive_scans.append(True)
        return original_index()
    monkeypatch.setattr(w.store, "index", counted_index)
    w.tabs.setCurrentIndex(2)
    assert w.tabs.tabText(2) == "Mouse colony"
    w.colony_mouse.setText("107")
    w.open_colony_profile()
    w.colony_edits["notes"].setPlainText("Manual surgery follow-up")
    w.colony_workbook.setText(str(workbook))
    w.search_colony()
    wait_job(app, w)
    assert not archive_scans, "Colony search must not refresh the acquisition archive"
    assert w.tabs.isEnabled() and w.progress.isHidden()
    assert "matching row(s)" in w.status.text()
    assert w.colony_results.rowCount() == 3
    assert not w.colony_results.selectionModel().selectedRows()
    assert not w.colony_import.isEnabled()
    assert not store.load_mouse_profile("107")
    w.colony_results.selectRow(0)
    assert w.colony_import.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    w.import_colony_row()
    assert not w.colony_profile["import_history"]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    w.import_colony_row()
    assert w.colony_edits["colony_id"].text() == "G107"
    assert w.colony_edits["date_of_birth"].text() == "2025-04-06"
    assert w.colony_edits["notes"].toPlainText() == "Manual surgery follow-up"
    assert w.colony_surgeries.rowCount() == 0
    w.add_surgery()
    w.colony_surgeries.item(0, 0).setText("2026-10-10")
    w.colony_surgeries.item(0, 1).setText("Synthetic window surgery")
    w.colony_surgeries.item(0, 2).setText("Synthetic surgeon")
    w.save_colony_profile()
    wait_job(app, w)
    assert not archive_scans, "Saving a colony profile must not refresh acquisition records"
    assert w.tabs.isEnabled() and w.progress.isHidden()
    assert not errors
    profile = store.load_mouse_profile("107")
    assert profile["colony_id"] == "G107" and profile["mouse_id"] == "107"
    assert profile["surgeries"][0]["details"] == "Synthetic window surgery"
    assert profile["revision"] == 1 and "Git committed" in w.colony_receipt.text()
    w.open_colony_profile()
    assert w.colony_surgeries.rowCount() == 1
    assert w.colony_edits["strain"].text() == "Synthetic strain A"
    assert not errors
    w.close()


def test_cli_profile_services(store, workbook, tmp_path, capsys):
    from experiment_logger.cli import main
    from experiment_logger.io import loads
    config = tmp_path / "cli" / "config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic CLI"})
    args = ["--config", str(config)]
    assert main([*args, "mouse-profile", "107"]) == 0
    profile = loads(capsys.readouterr().out)
    assert profile["revision"] == 0
    assert main([*args, "colony-search", str(workbook), "107"]) == 0
    assert len(loads(capsys.readouterr().out)) == 3
    path = tmp_path / "profile.json"
    atomic_json(path, profile)
    assert main([*args, "save-mouse-profile", str(path)]) == 0
    assert loads(capsys.readouterr().out)["profile"]["revision"] == 1
