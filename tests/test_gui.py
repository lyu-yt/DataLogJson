import json
import time
from pathlib import Path
import pytest

from PySide6.QtCore import QMimeData, QUrl, QPointF, Qt, qInstallMessageHandler
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from experiment_logger.gui import Window
from experiment_logger.io import atomic_json


def test_gui_fonts_have_valid_point_sizes(tmp_path):
    app = QApplication.instance() or QApplication([])
    messages = []
    previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
    w = None
    try:
        # An absent local config prevents any NAS connection during this check.
        w = Window(tmp_path / "font-check" / "config.json")
        w.show()
        for index in range(w.tabs.count()):
            w.tabs.setCurrentIndex(index)
            app.processEvents()
        widgets = [w, w.mouse, w.weight, w.training_day, w.task, w.drop,
                   w.tabs.tabBar(), w.records.horizontalHeader(), w.colony_surgeries]
        assert all(widget.font().pointSizeF() > 0 for widget in widgets)
        assert not any("QFont::setPointSize" in message for message in messages)
    finally:
        if w is not None:
            w.close()
        qInstallMessageHandler(previous)


def wait_job(app, window):
    assert window.job is not None, window.status.text()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        app.processEvents()
        if window.job and not window.job.isRunning():
            app.processEvents()
            return
        # Explicitly release Python's GIL while the worker imports/parses XLSX.
        time.sleep(0.01)
    raise AssertionError(f"Background operation did not complete: {window.status.text()}")


@pytest.mark.parametrize("task_type", ["water two-angle push", "Custom shaping / phase A"])
def test_ui_drop_validation_finalize_filter_preview_relink(store, sample, tmp_path, monkeypatch, task_type):
    app = QApplication.instance() or QApplication([])
    config = tmp_path / "ui-local" / "config.json"
    atomic_json(config, {"roots": store.roots, "operator": "Synthetic UI Tester"})
    w = Window(config)
    w.show()
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(sample))])
    event = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    w.drop.dropEvent(event)
    assert w.mouse.text() == "007" and w.date.text() == "2026-10-15"
    w.validate_form()
    assert "mouse_weight_g" in w.messages.text()
    w.weight.setValue(23.4)
    if task_type == "water two-angle push":
        w.task.setCurrentIndex(2)
    else:
        w.task.setCurrentIndex(3)
        assert not w.custom_task.isHidden()
        w.custom_task.setText(task_type)
    w.training_day.setValue(12)
    w.imaging.setCurrentIndex(1)
    w.notes.setPlainText("SYNTHETIC UI test")
    w.save_draft()
    assert w.draft["revision"] == 1
    assert w.store.load_draft(w.draft["record"]["record_id"])["record"]["experiment_type"] == task_type
    w.fill_form()
    if task_type != "water two-angle push":
        assert w.task.currentIndex() == 3 and w.custom_task.text() == task_type
    w.complete.setChecked(True)
    w.finalize()
    wait_job(app, w)
    assert not errors
    assert sample.exists()
    assert w.records.columnCount() == 8
    assert w.records.horizontalHeaderItem(3).text() == "Record type"
    assert w.records.rowCount() == 2
    assert {w.records.item(row, 3).text() for row in range(2)} == {"data", "session"}
    assert all(w.records.item(row, 2).text() == "12" for row in range(2))
    w.filter_day.setText("12")
    assert w.records.rowCount() == 2
    w.filter_day.setText("999")
    assert w.records.rowCount() == 0
    w.filter_day.clear()
    w.filter_kind.setCurrentText("session")
    assert w.records.rowCount() == 1
    w.records.selectRow(0)
    app.processEvents()
    session_detail = json.loads(w.raw.toPlainText())["record"]
    assert session_detail["record_type"] == "session"
    assert session_detail["record_id"] == w.records.item(0, 7).text()
    assert session_detail["training_day"] == 12
    assert w.csv_table.rowCount() == 0
    w.filter_kind.setCurrentText("data")
    w.filter_task.setCurrentText(task_type)
    assert w.records.rowCount() == 1
    w.filter_mouse.setText("999")
    assert w.records.rowCount() == 0
    w.filter_mouse.setText("007")
    w.records.selectRow(0)
    app.processEvents()
    assert w.raw.isReadOnly() and w.csv_table.columnCount() == 13
    task_detail = json.loads(w.raw.toPlainText())["record"]
    assert task_detail["record_type"] == "data"
    assert task_detail["record_id"] == w.records.item(0, 7).text()
    fid = w.file_picker.currentData()
    old = Path(w.store.file_locations(fid)[0]["path"])
    new = old.with_name("relocated.csv")
    old.rename(new)
    w.locate()
    wait_job(app, w)
    assert w.candidates.count() == 1
    w.relink()
    wait_job(app, w)
    assert any(x["available"] for x in w.store.file_locations(fid))
    assert not errors
    w.close()
