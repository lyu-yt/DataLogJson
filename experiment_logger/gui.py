"""Native Qt desktop window; all persistence goes through Store."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFormLayout, QDoubleSpinBox, QSpinBox, QComboBox,
    QTextEdit, QPlainTextEdit, QCheckBox, QTabWidget, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QInputDialog, QSplitter)

from .io import LoggerError, atomic_json, read_json
from .models import TASK_TYPES, SCHEMA_VERSION, validate
from .store import Store
from .layout import metadata_directory
from .colony_gui import ColonyTab
from .expression_gui import ExpressionTab
from .imaging_gui import ImagingTab
from .time_estimate import TimeEstimate


class Job(QThread):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(int)

    def __init__(self, function, *, refresh_archive=True):
        super().__init__()
        self.function = function
        self.refresh_archive = refresh_archive
        self.stop = threading.Event()
        self.eta = TimeEstimate()

    def run(self):
        try:
            def report(n, total):
                self.eta.observe(n, total)
                self.progress.emit(max(0, min(100, int(n * 100 / max(total, 1)))))
            report.phase = self.eta.phase
            self.result.emit(self.function(report, self.stop.is_set))
        except Exception as exc:
            self.error.emit(str(exc))


class DropZone(QLabel):
    dropped = Signal(str)

    def __init__(self):
        super().__init__("Drop a task CSV here\nMouse and date are read from its folders")
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(100)
        self.setObjectName("dropzone")

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).suffix.lower() == ".csv":
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.dropped.emit(urls[0].toLocalFile())
            # CopyAction means the OS drag source must not delete anything.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()


class Trace(QWidget):
    """Bounded generic numeric preview. No success or physical-time interpretation."""
    def __init__(self):
        super().__init__()
        self.values = []
        self.setMinimumHeight(130)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f3f7fa"))
        painter.setPen(QColor("#526173"))
        painter.drawText(12, 20, "Column preview • sample index • raw units unknown")
        if len(self.values) < 2:
            return
        low, high = min(self.values), max(self.values)
        painter.setPen(QPen(QColor("#137f86"), 2))
        from PySide6.QtCore import QPointF
        points = [QPointF(15 + i * (self.width() - 30) / (len(self.values) - 1),
                          self.height() - 15 - (v - low) / (high - low or 1) * (self.height() - 50))
                  for i, v in enumerate(self.values)]
        for a, b in zip(points, points[1:]):
            painter.drawLine(a, b)


class Window(QMainWindow, ColonyTab, ExpressionTab, ImagingTab):
    def __init__(self, config_path=None):
        super().__init__()
        self.config_path = Path(config_path or ".logger-local/config.json").resolve()
        self.config = read_json(self.config_path) if self.config_path.exists() else {}
        self.store = None
        self.draft = None
        self.job = None
        self.selected_id = None
        self.preview_rows = []
        self.setWindowTitle("Mouse experiment logger")
        self.resize(1100, 850)
        base = QWidget()
        self.setCentralWidget(base)
        layout = QVBoxLayout(base)
        title = QLabel("Experiment logger")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(QLabel("Local records  ·  Mouse task data  ·  America/New_York"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._entry_tab()
        self._browse_tab()
        self._colony_tab()
        self._expression_tab()
        self._imaging_tab()
        self._locations_tab()
        self._settings_tab()
        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.cancel_button = QPushButton("Cancel operation")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(lambda: self.job.stop.set() if self.job else None)
        footer.addWidget(self.progress)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        self.progress_eta = QLabel()
        self.progress_eta.setWordWrap(True)
        self.progress_eta.hide()
        layout.addWidget(self.progress_eta)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self.update_transfer_estimate)
        self.status = QLabel("Choose an archive folder in Settings to get started.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # Qt reports pointSize() == -1 for pixel-sized fonts. Use points so
        # native controls can safely reuse the inherited font's point size.
        self.setStyleSheet("""
            QWidget { font-family: 'Segoe UI', sans-serif; font-size: 10pt; }
            QMainWindow { background: #f6f8fb; }
            QLabel#title { font-size: 20pt; font-weight: 700; color: #163648; padding: 8px 0; }
            QLabel#dropzone { background: #e8f4f3; border: 2px dashed #459b99; border-radius: 10px; color: #155c61; font-size: 12pt; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit { padding: 6px; background: white; }
            QPushButton { padding: 8px 14px; }
            QPushButton#primary { background: #166c72; color: white; border-radius: 5px; font-weight: 600; }
            QTabWidget::pane { border: 1px solid #d4dee7; background: white; }
            QTabBar::tab { padding: 10px 20px; }
        """)
        if self.config:
            self._connect_store()
            self.refresh()
        else:
            self.tabs.setCurrentIndex(self.tabs.count() - 1)

    def _page(self, name):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        self.tabs.addTab(page, name)
        return layout

    def _button(self, text, fn, layout):
        button = QPushButton(text)
        button.clicked.connect(fn)
        layout.addWidget(button)
        return button

    def _entry_tab(self):
        layout = self._page("Behavior")
        self.drop = DropZone()
        self.drop.dropped.connect(self.load_source)
        layout.addWidget(self.drop)
        source_row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setReadOnly(True)
        self.source.setPlaceholderText("Original file path")
        source_row.addWidget(self.source, 1)
        self._button("Choose CSV…", self.choose_source, source_row)
        layout.addLayout(source_row)
        saved = QHBoxLayout()
        self.draft_picker = QComboBox()
        saved.addWidget(self.draft_picker, 1)
        self._button("Open saved draft", self.open_draft, saved)
        layout.addLayout(saved)
        form = QFormLayout()
        self.mouse = QLineEdit()
        self.mouse.setPlaceholderText("Three digits, e.g. 007")
        self.date = QLineEdit()
        self.date.setPlaceholderText("YYYY-MM-DD")
        self.weight = QDoubleSpinBox()
        self.weight.setRange(0, 999)
        self.weight.setDecimals(2)
        self.weight.setSuffix(" g")
        self.weight.setSpecialValueText("Enter weight")
        self.task = QComboBox()
        self.task.addItems(["Choose task…", *TASK_TYPES, "Other / custom…"])
        self.custom_task = QLineEdit()
        self.custom_task.setPlaceholderText("Enter your task type")
        self.custom_task.setMaxLength(120)
        self.custom_task.setVisible(False)
        self.task.currentIndexChanged.connect(lambda index: self.custom_task.setVisible(index == len(TASK_TYPES) + 1))
        task_field = QWidget()
        task_layout = QVBoxLayout(task_field)
        task_layout.setContentsMargins(0, 0, 0, 0)
        task_layout.addWidget(self.task)
        task_layout.addWidget(self.custom_task)
        self.training_day = QSpinBox()
        self.training_day.setRange(-1, 100000)
        self.training_day.setValue(-1)
        self.training_day.setSpecialValueText("Enter training day")
        self.imaging = QComboBox()
        self.imaging.addItems(["Choose…", "No", "Yes"])
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(70)
        self.notes.setPlaceholderText("Optional observations or deviations")
        for name, widget in [("Mouse ID", self.mouse), ("Experiment date", self.date),
                             ("Mouse weight", self.weight), ("Task type", task_field),
                             ("Training day", self.training_day), ("Imaging session", self.imaging),
                             ("Notes", self.notes)]:
            form.addRow(name, widget)
        layout.addLayout(form)
        layout.addWidget(QLabel("Sampling interval: unknown  ·  Exact acquisition start/end: unknown"))
        self.complete = QCheckBox("Acquisition is finished and the source file is closed")
        self.move = QCheckBox("Move source: direct move when possible; otherwise verified copy and source removal")
        layout.addWidget(self.complete)
        layout.addWidget(self.move)
        actions = QHBoxLayout()
        self._button("Save draft", self.save_draft, actions)
        self._button("Validate", self.validate_form, actions)
        self.finalize_button = self._button("Finalize…", self.finalize, actions)
        self.finalize_button.setObjectName("primary")
        layout.addLayout(actions)
        self.messages = QLabel("")
        self.messages.setWordWrap(True)
        self.messages.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.messages)
        layout.addStretch()

    def _browse_tab(self):
        layout = self._page("Browse / details")
        filters = QHBoxLayout()
        self.filter_mouse = QLineEdit()
        self.filter_mouse.setPlaceholderText("Mouse")
        self.filter_date = QLineEdit()
        self.filter_date.setPlaceholderText("Date YYYY-MM-DD")
        self.filter_day = QLineEdit()
        self.filter_day.setPlaceholderText("Training day")
        self.filter_task = QComboBox()
        self.filter_task.addItems(["All experiment types", *TASK_TYPES])
        self.filter_task.setEditable(True)
        self.filter_kind = QComboBox()
        self.filter_kind.addItems(["All record types", "data", "session", "amendment", "analysis_run"])
        for w in (self.filter_mouse, self.filter_date, self.filter_day, self.filter_task):
            filters.addWidget(w)
        layout.addLayout(filters)
        kinds = QHBoxLayout()
        self.filter_data_type = QComboBox()
        self.filter_data_type.addItems(["All data types", "task", "expression", "imaging"])
        kinds.addWidget(self.filter_kind)
        kinds.addWidget(self.filter_data_type)
        self._button("Refresh", self.refresh, kinds)
        kinds.addStretch()
        layout.addLayout(kinds)
        for w in (self.filter_mouse, self.filter_date, self.filter_day):
            w.textChanged.connect(self.refresh_records)
        self.filter_task.currentTextChanged.connect(self.refresh_records)
        self.filter_kind.currentIndexChanged.connect(self.refresh_records)
        self.filter_data_type.currentIndexChanged.connect(self.refresh_records)
        self.records = QTableWidget(0, 8)
        self.records.setHorizontalHeaderLabels(["Mouse", "Date", "Day", "Record type", "Experiment", "Files", "Integrity", "Record ID"])
        self.records.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.records.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.records.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.records.itemSelectionChanged.connect(self.show_detail)
        layout.addWidget(self.records, 2)
        self.detail_tabs = QTabWidget()
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.detail_tabs.addTab(self.raw, "Metadata & history (read-only)")
        self.csv_table = QTableWidget()
        self.csv_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.detail_tabs.addTab(self.csv_table, "CSV preview")
        self.expression_files = QTableWidget(0, 5)
        self.expression_files.setHorizontalHeaderLabels(["Channel", "Original filename", "Bytes", "Archive path", "Status"])
        self.expression_files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.expression_files.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.detail_tabs.addTab(self.expression_files, "TIFF files")
        plot_page = QWidget()
        plot_layout = QVBoxLayout(plot_page)
        self.plot_column = QComboBox()
        self.plot_column.currentIndexChanged.connect(self.plot_update)
        self.trace = Trace()
        plot_layout.addWidget(self.plot_column)
        plot_layout.addWidget(self.trace)
        self.detail_tabs.addTab(plot_page, "Numeric preview")
        layout.addWidget(self.detail_tabs, 3)
        actions = QHBoxLayout()
        self._button("Add amendment…", self.add_amendment, actions)
        self.linked_behavior_button = self._button("Open linked behavior", self.open_linked_behavior, actions)
        self.linked_behavior_button.setEnabled(False)
        self.linked_behavior_id = None
        self._button("Rebuild index", self.rebuild_index, actions)
        self._button("Recover pending publication / Git", self.recover, actions)
        layout.addLayout(actions)

    def _locations_tab(self):
        layout = self._page("Locations")
        layout.addWidget(QLabel("Select a registered file. Search is restricted to the root you choose."))
        self.file_picker = QComboBox()
        layout.addWidget(self.file_picker)
        self.location_info = QPlainTextEdit()
        self.location_info.setReadOnly(True)
        layout.addWidget(self.location_info, 1)
        self.file_picker.currentIndexChanged.connect(self.show_locations)
        self.root_picker = QComboBox()
        layout.addWidget(self.root_picker)
        actions = QHBoxLayout()
        self._button("Full SHA-256 verification", self.verify, actions)
        self._button("Locate missing files", self.locate, actions)
        self._button("Relink selected copy", self.relink, actions)
        self._button("Rebuild catalog", self.rebuild_catalog, actions)
        self._button("Commit catalog", self.commit_catalog, actions)
        layout.addLayout(actions)
        self.candidates = QComboBox()
        layout.addWidget(self.candidates)
        layout.addWidget(QLabel("Relinking changes the catalog only. Original scientific records stay unchanged."))

    def _settings_tab(self):
        layout = self._page("Settings")
        form = QFormLayout()
        self.archive = QLineEdit(self.config.get("roots", {}).get("archive", ""))
        self.operator = QLineEdit(self.config.get("operator", ""))
        self.other_roots = QPlainTextEdit(json.dumps({k: v for k, v in self.config.get("roots", {}).items() if k != "archive"}, indent=2))
        self.other_roots.setMaximumHeight(110)
        form.addRow("Archive root (NAS or local folder)", self.archive)
        form.addRow("Operator", self.operator)
        form.addRow("Additional roots (JSON object)", self.other_roots)
        layout.addLayout(form)
        self._button("Choose archive folder…", self.choose_archive, layout)
        self._button("Save settings / initialize storage", self.save_settings, layout)
        layout.addWidget(QLabel("Metadata: <archive>/metadata-json/records/mouse_<ID>/YYMMDD/<task>_<number>/\n"
                               "CSVs: <archive>/robot-csv/mouse_<ID>/YYMMDD/<number>/\n"
                               "Mouse profile: <archive>/metadata-json/records/mouse_<ID>/mouse_info.json\n"
                               "Expression TIFFs: <archive>/expression/mouse_<ID>/YYMMDD/left/green, left/red, right, other\n"
                               "Imaging TIFFs: <archive>/imaging/mouse_<ID>/YYMMDD/left or right\n"
                               "Machine paths stay in local settings. Git stays local. Backups are separate.\n"
                               "To relocate a root, update its path here. Keep its logical root name the same."))
        layout.addStretch()

    def error(self, text):
        self.status.setText(text)
        QMessageBox.warning(self, "Logger", text)

    def _connect_store(self):
        root = self.config["roots"]["archive"]
        self.store = Store(metadata_directory(root), self.config["roots"], self.config_path.parent)
        self.status.setText(f"Metadata repository: {self.store.metadata}")

    def choose_archive(self):
        path = QFileDialog.getExistingDirectory(self, "Archive root")
        if path:
            self.archive.setText(path)

    def save_settings(self):
        try:
            from .io import loads
            extras = loads(self.other_roots.toPlainText())
            if not isinstance(extras, dict) or "archive" in extras or not all(isinstance(k, str) and isinstance(v, str) for k, v in extras.items()):
                raise LoggerError("Additional roots must be a JSON object of name: absolute path pairs; archive is reserved")
            root = Path(self.archive.text()).expanduser()
            if not root.is_absolute() or not self.operator.text().strip():
                raise LoggerError("Enter an absolute archive path and operator name")
            config = {**self.config, "roots": {"archive": str(root), **extras}, "operator": self.operator.text().strip(),
                      "timezone": "America/New_York"}
            store = Store(metadata_directory(root), config["roots"], self.config_path.parent)
            store.initialize()
            atomic_json(self.config_path, config)
            self.config, self.store = config, store
            self.colony_profile = self.colony_saved_profile = None
            self.expression_draft = None
            self.clear_expression_fields()
            self.imaging_draft = None
            self.clear_imaging_fields()
            self.refresh()
            self.status.setText(f"Storage ready. Metadata repository: {store.metadata}. Drop a CSV to log a session.")
            self.tabs.setCurrentIndex(0)
        except Exception as exc:
            self.error(str(exc))

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose completed task file", "", "Task CSV (*.csv)")
        if path:
            self.load_source(path)

    def load_source(self, path):
        if not self.store:
            self.error("Configure storage in Settings first")
            return
        if self.draft and QMessageBox.question(self, "New draft", "Replace the current form? Save it first if you need to retain edits.") != QMessageBox.StandardButton.Yes:
            return
        self.draft = self.store.new(path)
        self.fill_form()

    def fill_form(self):
        r = self.draft["record"]
        m = r["metadata"]
        self.source.setText(self.draft["source_path"])
        self.mouse.setText(r["mouse_id"] or "")
        self.date.setText(r["session_date"] or "")
        task_type = r.get("experiment_type")
        self.custom_task.setText(task_type if task_type and task_type not in TASK_TYPES else "")
        self.task.setCurrentIndex(TASK_TYPES.index(task_type) + 1 if task_type in TASK_TYPES else len(TASK_TYPES) + 1 if task_type else 0)
        self.weight.setValue(m.get("mouse_weight_g") or 0)
        self.training_day.setValue(m["training_day"] if m.get("training_day") is not None else -1)
        self.imaging.setCurrentIndex(0 if m.get("is_imaging_session") is None else 2 if m["is_imaging_session"] else 1)
        self.notes.setPlainText(r["notes"] or "")
        self.complete.setChecked(False)
        self.move.setChecked(False)
        self.messages.setText("Review the inferred mouse and date. Weight, training day, and imaging status are never carried forward.")
        self.tabs.setCurrentIndex(0)

    def collect(self):
        if not self.draft:
            raise LoggerError("Drop or choose a CSV first")
        r = self.draft["record"]
        task_type = (self.custom_task.text().strip() if self.task.currentIndex() == len(TASK_TYPES) + 1
                     else self.task.currentText() if self.task.currentIndex() else None)
        r["schema_version"] = SCHEMA_VERSION
        r.update(mouse_id=self.mouse.text().strip() or None, session_date=self.date.text().strip() or None,
                 experiment_type=task_type or None,
                 operator=self.config.get("operator"), notes=self.notes.toPlainText().strip() or None)
        r["metadata"].update(mouse_weight_g=self.weight.value() or None,
                             training_day=self.training_day.value() if self.training_day.value() >= 0 else None,
                             is_imaging_session=None if not self.imaging.currentIndex() else self.imaging.currentIndex() == 2)
        return self.draft

    def save_draft(self):
        try:
            self.draft = self.store.save_draft(self.collect())
            self.messages.setText(f"Draft saved — revision {self.draft['revision']}")
            self.refresh_drafts()
        except Exception as exc:
            self.error(str(exc))

    def open_draft(self):
        try:
            rid = self.draft_picker.currentData()
            if rid:
                self.draft = self.store.load_draft(rid)
                self.fill_form()
        except Exception as exc:
            self.error(str(exc))

    def validate_form(self):
        try:
            errors = [e for e in validate(self.collect()["record"], True) if not e.startswith("files:")]
            self.messages.setText("\n".join(errors) if errors else "Entry fields are valid. Full CSV validation and file registration run during finalization.")
        except Exception as exc:
            self.error(str(exc))

    def run_job(self, function, success, *, refresh_archive=True):
        if self.job and self.job.isRunning():
            return
        self.job = Job(function, refresh_archive=refresh_archive)
        self.tabs.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        self.progress_eta.setText(self.job.eta.text())
        self.progress_eta.show()
        self.progress_timer.start()
        self.status.setText("Working… file verification and NAS transfer may take longer than form entry.")
        self.job.progress.connect(self.progress.setValue)
        self.job.result.connect(success)
        self.job.error.connect(self.error)
        self.job.finished.connect(self.job_finished)
        self.job.start()

    def job_finished(self):
        self.progress_timer.stop()
        self.progress_eta.hide()
        self.tabs.setEnabled(True)
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        if self.job is None or self.job.refresh_archive:
            self.refresh()

    def update_transfer_estimate(self):
        if self.job:
            self.progress_eta.setText(self.job.eta.text())

    def finalize(self):
        try:
            draft = self.collect()
            errors = [e for e in validate(draft["record"], True) if not e.startswith("files:")]
            if errors:
                raise LoggerError("\n".join(errors))
            if not self.complete.isChecked():
                raise LoggerError("Confirm acquisition has finished and the file is closed")
            layout = self.store.reserve_layout(draft["record"])
            _, target = self.store.destination(draft["record"], draft["source_path"], layout=layout)
            move = self.move.isChecked()
            question = (f"{'MOVE' if move else 'COPY'} this file?\n\nSource: {draft['source_path']}\n\nDestination: {target}\n\n"
                        + f"Metadata: {self.store.metadata / 'records' / layout['directory_name']}\n\n"
                        + ("A direct move is attempted first. Otherwise a verified copy is made and the original is removed after the metadata Git commit. Already moved files remain in the archive after cancellation; Recover resumes registration." if move else "The original will be retained.")
                        + "\n\nFinalized metadata is read-only. Corrections use amendments.")
            if QMessageBox.question(self, "Finalize session", question) != QMessageBox.StandardButton.Yes:
                return
            self.draft = self.store.save_draft(draft)
            frozen_draft = json.loads(json.dumps(self.draft))
            self.run_job(lambda progress, cancel: self.store.finalize(frozen_draft, move=move, source_complete=True,
                                                                     progress=progress, cancel=cancel), self.finalized)
        except Exception as exc:
            self.error(str(exc))

    def finalized(self, receipt):
        self.messages.setText(json.dumps(receipt, indent=2))
        self.status.setText("Finalized locally • " + ("Git committed" if receipt["git_committed"] else "Git pending — use Recover") + " • Backup not confirmed")
        self.draft = None
        self.source.clear()

    def refresh_drafts(self, *, index=None):
        self.draft_picker.clear()
        self.expression_draft_picker.clear()
        self.imaging_draft_picker.clear()
        if self.store:
            for d in self.store.drafts(index=index):
                r = d["record"]
                picker = (self.imaging_draft_picker if r.get("data_type") == "imaging" else
                          self.expression_draft_picker if r.get("data_type") == "expression" else self.draft_picker)
                picker.addItem(f"{r.get('mouse_id') or '?'} · {r.get('session_date') or '?'} · {r['record_id'][:8]}", r["record_id"])

    def refresh(self):
        if not self.store:
            return
        try:
            # A fresh scan belongs to this refresh only. Avoid repeating the NAS
            # traversal for every draft, row, and record read.
            index = self.store.index()
            catalog = self.store._catalog()
            self.refresh_drafts(index=index)
            self.filter_task.blockSignals(True)
            try:
                for info in index.values():
                    task_type = info.get("experiment_type")
                    if task_type and self.filter_task.findText(task_type) < 0:
                        self.filter_task.addItem(task_type)
            finally:
                self.filter_task.blockSignals(False)
            self.refresh_records(index=index, catalog=catalog)
            self.file_picker.blockSignals(True)
            try:
                self.file_picker.clear()
                for fid, f in catalog["files"].items():
                    self.file_picker.addItem(f"{f['original_filename']} · {fid}", fid)
            finally:
                self.file_picker.blockSignals(False)
            self.show_locations()
            self.root_picker.clear()
            self.root_picker.addItems(list(self.store.roots))
            pending = (self.store.pending() + self.store.pending_migrations() + self.store.pending_profiles()
                       + self.store.pending_imaging() + self.store.pending_transfers())
            if pending:
                self.status.setText(f"{len(pending)} pending publication/Git operation(s). Use Recover in Browse.")
        except Exception as exc:
            self.status.setText(str(exc))

    def refresh_records(self, _signal=None, *, index=None, catalog=None):
        if not self.store:
            return
        try:
            index = self.store.index() if index is None else index
            catalog = self.store._catalog() if catalog is None else catalog
            self.records.setRowCount(0)
            for info in index.values():
                if self.filter_mouse.text() and self.filter_mouse.text() not in str(info["mouse_id"]):
                    continue
                if self.filter_date.text() and self.filter_date.text() not in str(info["session_date"]):
                    continue
                if self.filter_day.text() and self.filter_day.text() != str(info["training_day"]):
                    continue
                task_filter = self.filter_task.currentText().strip()
                if task_filter and task_filter != "All experiment types" and info["experiment_type"] != task_filter:
                    continue
                if self.filter_data_type.currentIndex() and info.get("data_type") != self.filter_data_type.currentText():
                    continue
                if self.filter_kind.currentIndex() and info["record_type"] != self.filter_kind.currentText():
                    continue
                available = "—"
                if info["record_type"] == "data" and info["integrity"] == "ok":
                    r = self.store.read_record(info["record_id"], index=index)
                    try:
                        available = "Present (hash not rechecked)" if all(any(l["available"] for l in self.store.file_locations(f["file_id"], catalog=catalog)) for f in r["files"]) else "Missing"
                    except LoggerError:
                        available = "Catalog pending"
                row = self.records.rowCount()
                self.records.insertRow(row)
                values = [info["mouse_id"], info["session_date"], info["training_day"],
                          info["record_type"], info["experiment_type"], available, info["integrity"], info["record_id"]]
                for col, value in enumerate(values):
                    self.records.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
        except Exception as exc:
            self.status.setText(str(exc))

    def show_detail(self):
        self.linked_behavior_id = None
        self.linked_behavior_button.setEnabled(False)
        row = self.records.currentRow()
        if row < 0 or not self.records.item(row, 7):
            return
        rid = self.records.item(row, 7).text()
        self.selected_id = rid
        try:
            record = self.store.read_record(rid)
            analysis_history = [self.store.read_record(i["record_id"]) for i in self.store.index().values()
                                if i["record_type"] == "analysis_run" and i["integrity"] == "ok"
                                and any(x["record_id"] == rid for x in self.store.read_record(i["record_id"])["run"].get("inputs", []))]
            detail = {"record": record, "amendments_explicit_not_applied": self.store.amendments(rid),
                      "analysis_history": analysis_history}
            self.raw.setPlainText(json.dumps(detail, indent=2, ensure_ascii=False))
            self.csv_table.clear()
            self.csv_table.setRowCount(0)
            self.expression_files.setRowCount(0)
            self.plot_column.clear()
            self.preview_rows = []
            if record.get("data_type") == "task":
                headers, rows = self.store.preview(rid, 200)
                self.preview_rows = rows
                self.csv_table.setColumnCount(len(headers))
                self.csv_table.setHorizontalHeaderLabels(headers)
                self.csv_table.setRowCount(min(20, len(rows)))
                for i, row in enumerate(rows[:20]):
                    for j, value in enumerate(row):
                        self.csv_table.setItem(i, j, QTableWidgetItem(value))
                self.plot_column.addItems(headers)
            elif record.get("data_type") in ("expression", "imaging"):
                from .expression import CHANNELS
                if record.get("data_type") == "imaging" and record["metadata"]["behavior"]:
                    self.linked_behavior_id = record["metadata"]["behavior"]["record_id"]
                    self.linked_behavior_button.setEnabled(True)
                for f in record["files"]:
                    locations = self.store.file_locations(f["file_id"])
                    for location in locations or [{}]:
                        row = self.expression_files.rowCount()
                        self.expression_files.insertRow(row)
                        label = f["role"].capitalize() if record["data_type"] == "imaging" else CHANNELS[f["role"]][0]
                        values = [label, f["original_filename"], f["size_bytes"],
                                  location.get("path") or location.get("relative_path", ""),
                                  "Present (hash not rechecked)" if location.get("available") else "Missing"]
                        for col, value in enumerate(values):
                            self.expression_files.setItem(row, col, QTableWidgetItem(str(value)))
        except Exception as exc:
            self.raw.setPlainText(str(exc))

    def open_linked_behavior(self):
        rid = self.linked_behavior_id
        if not rid:
            return
        filters = (self.filter_mouse, self.filter_date, self.filter_day, self.filter_task,
                   self.filter_kind, self.filter_data_type)
        for field in filters:
            field.blockSignals(True)
        try:
            for field in (self.filter_mouse, self.filter_date, self.filter_day):
                field.clear()
            for field in (self.filter_task, self.filter_kind, self.filter_data_type):
                field.setCurrentIndex(0)
        finally:
            for field in filters:
                field.blockSignals(False)
        self.refresh_records()
        for row in range(self.records.rowCount()):
            if self.records.item(row, 7).text() == rid:
                self.records.selectRow(row)
                return
        self.status.setText('Linked behavior record is missing; use Locations or Recover.')

    def plot_update(self):
        col = self.plot_column.currentIndex()
        self.trace.values = [float(row[col]) for row in self.preview_rows] if col >= 0 else []
        self.trace.update()

    def add_amendment(self):
        if not self.selected_id:
            return
        reason, ok = QInputDialog.getText(self, "Amendment", "Reason for correction:")
        if not ok or not reason.strip():
            return
        values, ok = QInputDialog.getMultiLineText(self, "Corrected values", "Explicit corrected fields as JSON (original is preserved):", '{"metadata.mouse_weight_g": 25.0}')
        if ok:
            try:
                from .io import loads
                receipt = self.store.amend(self.selected_id, self.config["operator"], reason, loads(values))
                self.status.setText(json.dumps(receipt))
                self.refresh()
            except Exception as exc:
                self.error(str(exc))

    def rebuild_index(self):
        if self.store:
            try:
                self.store.rebuild_index()
                self.refresh()
            except Exception as exc:
                self.error(str(exc))

    def rebuild_catalog(self):
        if self.store:
            try:
                self.store.rebuild_catalog()
                self.refresh()
            except Exception as exc:
                self.error(str(exc))

    def recover(self):
        if self.store:
            self.run_job(lambda p, c: self.store.recover(p, c), lambda result: self.status.setText(json.dumps(result)))

    def commit_catalog(self):
        if self.store:
            self.run_job(lambda p, c: self.store.commit_catalog(self.config["operator"]),
                         lambda result: self.status.setText(json.dumps(result)))

    def show_locations(self):
        if self.store and self.file_picker.currentData():
            try:
                self.location_info.setPlainText(json.dumps(self.store.file_locations(self.file_picker.currentData()), indent=2))
            except Exception as exc:
                self.location_info.setPlainText(str(exc))

    def verify(self):
        fid = self.file_picker.currentData()
        if fid:
            self.run_job(lambda p, c: self.store.verify_file(fid, p, c),
                         lambda result: self.location_info.setPlainText(json.dumps(result, indent=2)))

    def locate(self):
        fid, root = self.file_picker.currentData(), self.root_picker.currentText()
        if fid and root:
            self.run_job(lambda p, c: self.store.locate(fid, [root], p, c), self.show_candidates)

    def show_candidates(self, items):
        self.candidates.clear()
        for item in items:
            self.candidates.addItem(f"{item['root_id']} / {item['relative_path']}", item)
        self.status.setText(f"{len(items)} checksum-matching copies found. Choose one to relink.")

    def relink(self):
        item, fid = self.candidates.currentData(), self.file_picker.currentData()
        if item and fid:
            self.run_job(lambda p, c: self.store.relink(fid, item["root_id"], item["relative_path"], p, c),
                         lambda result: self.status.setText("Verified copy registered; scientific record unchanged."))

    def closeEvent(self, event):
        if self.job and self.job.isRunning():
            self.status.setText("Cancel the operation and wait for it to finish before closing.")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
