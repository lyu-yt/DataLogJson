"""Bounded archive reads, independent colony jobs, and safe snapshot reuse."""
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from experiment_logger.gui import Window
from experiment_logger.io import LoggerError, atomic_json, read_json
from test_gui import wait_job


def count_scans(store, monkeypatch):
    calls = []
    original = store.index
    def index():
        calls.append(True)
        return original()
    monkeypatch.setattr(store, "index", index)
    return calls


def test_listing_many_drafts_uses_one_archive_scan(store, sample, draft, monkeypatch):
    store.finalize(draft, source_complete=True)
    for _ in range(5):
        store.save_draft(store.new(sample))
    calls = count_scans(store, monkeypatch)
    assert len(store.drafts()) == 5
    assert len(calls) == 1
    current = store.index()
    calls.clear()
    assert len(store.drafts(index=current)) == 5
    assert not calls


def test_browse_refresh_scans_once_and_checks_current_file_locations(store, sample, draft, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    store.finalize(draft, source_complete=True)
    for _ in range(4):
        store.save_draft(store.new(sample))
    w = Window(tmp_path / "absent-config.json")
    w.config = {"roots": store.roots, "operator": "Synthetic"}
    w.store = store
    calls = count_scans(store, monkeypatch)
    try:
        w.refresh()
        assert len(calls) == 1
        assert w.records.rowCount() == 2
        assert w.draft_picker.count() == 4
        assert "Present" in w.records.item(0, 5).text()
        # Signal payloads from text/combobox filters are not index snapshots.
        calls.clear()
        w.filter_kind.setCurrentText("data")
        assert len(calls) == 1 and w.records.rowCount() == 1
        record = store.read_record(draft["record"]["record_id"])
        csv = Path(store.file_locations(record["files"][0]["file_id"])[0]["path"])
        csv.rename(csv.with_name("moved.csv"))
        calls.clear()
        w.refresh()
        assert len(calls) == 1
        assert w.records.item(0, 5).text() == "Missing"
    finally:
        w.close()


def test_reused_index_does_not_accept_changed_record_bytes(store, draft, monkeypatch):
    store.finalize(draft, source_complete=True)
    rid = draft["record"]["record_id"]
    snapshot = store.index()
    calls = count_scans(store, monkeypatch)
    record = store.read_record(rid, index=snapshot)
    assert record["record_id"] == rid and not calls
    path = store.metadata / snapshot[rid]["relative_path"]
    altered = read_json(path)
    altered["metadata"]["mouse_weight_g"] = 99
    atomic_json(path, altered)
    with pytest.raises(LoggerError, match="checksum mismatch"):
        store.read_record(rid, index=snapshot)
    assert not calls


def test_failed_colony_job_restores_controls_without_archive_scan(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    w = Window(tmp_path / "absent-config.json")
    errors, refreshes, successes = [], [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(w, "refresh", lambda: refreshes.append(True))
    def fail(_progress, _cancel):
        raise LoggerError("Synthetic colony failure")
    try:
        w.run_job(fail, successes.append, refresh_archive=False)
        wait_job(app, w)
        assert errors == ["Synthetic colony failure"]
        assert not successes and not refreshes
        assert w.tabs.isEnabled() and w.progress.isHidden() and w.cancel_button.isHidden()
        assert w.progress_eta.isHidden() and not w.progress_timer.isActive()
    finally:
        w.close()
