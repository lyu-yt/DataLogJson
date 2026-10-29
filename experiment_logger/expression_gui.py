"""Expression entry UI; folder detection and publication live in expression.py."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QLabel, QScrollArea, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QPlainTextEdit, QCheckBox, QComboBox, QFileDialog, QMessageBox)

from .expression import CHANNELS, mouse_folders, selection
from .io import LoggerError
from .models import SCHEMA_VERSION


class FolderDrop(QLabel):
    dropped = Signal(str)

    def __init__(self):
        super().__init__("Drop an expression mouse folder or date folder here")
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(80)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).is_dir():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile() and Path(urls[0].toLocalFile()).is_dir():
            self.dropped.emit(urls[0].toLocalFile())
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()


class ExpressionTab:
    def _expression_tab(self):
        self.expression_draft = None
        outer = self._page("Expression")
        self.expression_page = outer.parentWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.expression_drop = FolderDrop()
        self.expression_drop.dropped.connect(self.load_expression_folder)
        layout.addWidget(self.expression_drop)
        source = QHBoxLayout()
        self.expression_source = QLineEdit()
        self.expression_source.setReadOnly(True)
        self.expression_source.setPlaceholderText("Selected folder")
        source.addWidget(self.expression_source)
        self._button("Choose folder…", self.choose_expression_folder, source)
        layout.addLayout(source)
        row = QHBoxLayout()
        row.addWidget(QLabel("Mouse folder"))
        self.expression_mouse_picker = QComboBox()
        self.expression_mouse_picker.setMinimumWidth(110)
        self.expression_mouse_picker.currentIndexChanged.connect(self.choose_expression_mouse)
        row.addWidget(self.expression_mouse_picker)
        self.expression_draft_picker = QComboBox()
        row.addWidget(self.expression_draft_picker, 1)
        self._button("Open saved draft", self.open_expression_draft, row)
        layout.addLayout(row)
        identity = QFormLayout()
        self.expression_mouse = QLineEdit()
        self.expression_mouse.setPlaceholderText("Three digits, e.g. 007")
        self.expression_date = QLineEdit()
        self.expression_date.setPlaceholderText("YYYY-MM-DD")
        identity.addRow("Mouse ID", self.expression_mouse)
        identity.addRow("Experiment date", self.expression_date)
        layout.addLayout(identity)
        layout.addWidget(QLabel("Review the detected files. Each selected channel can contain multiple TIFFs, one path per line."))
        self.expression_checks, self.expression_paths = {}, {}
        for key, (label, _) in CHANNELS.items():
            channel = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(80)
            channel.addWidget(name)
            check = QCheckBox()
            check.setToolTip(f"Include {label} files in this expression record")
            channel.addWidget(check)
            paths = QPlainTextEdit()
            paths.setPlaceholderText("One full TIFF path per line; edit or remove paths here")
            paths.setFixedHeight(68)
            paths.setEnabled(False)
            channel.addWidget(paths, 1)
            browse = self._button("Add TIFFs…", lambda _checked=False, k=key: self.choose_expression_files(k), channel)
            browse.setEnabled(False)
            check.toggled.connect(paths.setEnabled)
            check.toggled.connect(browse.setEnabled)
            self.expression_checks[key], self.expression_paths[key] = check, paths
            layout.addLayout(channel)
        self.expression_notes = QPlainTextEdit()
        self.expression_notes.setPlaceholderText("Optional expression-check notes")
        self.expression_notes.setFixedHeight(65)
        layout.addWidget(self.expression_notes)
        self.expression_complete = QCheckBox("Acquisition is finished and all source TIFFs are closed")
        layout.addWidget(self.expression_complete)
        self.expression_move = QCheckBox('Move sources: direct move when possible; otherwise verified copy and source removal')
        layout.addWidget(self.expression_move)
        layout.addWidget(QLabel("Destination: <archive>/expression/mouse_<ID>/YYMMDD/left/green, left/red, right, or other."))
        actions = QHBoxLayout()
        self._button("Save draft", self.save_expression_draft, actions)
        self._button("Validate", self.validate_expression_form, actions)
        self._button("Finalize / transfer TIFFs…", self.finalize_expression_form, actions).setObjectName("primary")
        outer.addLayout(actions)
        self.expression_messages = QLabel("Drop a folder to begin. Choose Move to relocate sources, or leave it unchecked to retain them. Transfers run in the background.")
        self.expression_messages.setWordWrap(True)
        self.expression_messages.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.expression_messages)

    def choose_expression_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose expression mouse or date folder")
        if folder:
            self.load_expression_folder(folder)

    def _replace_expression(self):
        return not self.expression_draft or QMessageBox.question(self, "Replace expression form",
            "Replace the current expression form? Save the draft first if you need to keep edits.") == QMessageBox.StandardButton.Yes

    def load_expression_folder(self, folder):
        try:
            if not self.store:
                raise LoggerError("Initialize storage in Settings first")
            candidates = mouse_folders(folder)
            if not self._replace_expression():
                return
            self.expression_draft = None
            self.clear_expression_fields()
            self.expression_source.setText(str(Path(folder).resolve()))
            self.expression_mouse_picker.blockSignals(True)
            self.expression_mouse_picker.clear()
            for path in candidates:
                self.expression_mouse_picker.addItem(path.name, str(path))
            self.expression_mouse_picker.setCurrentIndex(-1)
            self.expression_mouse_picker.blockSignals(False)
            self.tabs.setCurrentWidget(self.expression_page)
            if len(candidates) == 1:
                self.expression_mouse_picker.setCurrentIndex(0)
            else:
                self.expression_messages.setText(f"Found {len(candidates)} mouse folders. Choose a mouse to log; each mouse gets its own record.")
        except Exception as exc:
            self.error(str(exc))

    def choose_expression_mouse(self):
        folder = self.expression_mouse_picker.currentData()
        if not folder:
            return
        try:
            if not self._replace_expression():
                previous = self.expression_mouse_picker.findData(self.expression_draft["source_folder"])
                self.expression_mouse_picker.blockSignals(True)
                self.expression_mouse_picker.setCurrentIndex(previous)
                self.expression_mouse_picker.blockSignals(False)
                return
            self.expression_draft = self.store.new_expression(folder)
            self.fill_expression_form()
        except Exception as exc:
            self.error(str(exc))

    def clear_expression_fields(self):
        self.expression_mouse.clear()
        self.expression_date.clear()
        self.expression_notes.clear()
        self.expression_complete.setChecked(False)
        self.expression_move.setChecked(False)
        for key in CHANNELS:
            self.expression_checks[key].setChecked(False)
            self.expression_paths[key].clear()

    def fill_expression_form(self):
        d = self.expression_draft
        r = d["record"]
        self.expression_source.setText(d["source_folder"])
        self.expression_mouse.setText(r.get("mouse_id") or "")
        self.expression_date.setText(r.get("session_date") or "")
        self.expression_notes.setPlainText(r.get("notes") or "")
        for key in CHANNELS:
            self.expression_checks[key].setChecked(r["metadata"]["channels"][key]["selected"])
            self.expression_paths[key].setPlainText("\n".join(d["channel_paths"][key]))
        self.expression_complete.setChecked(False)
        self.expression_move.setChecked(False)
        count = sum(len(paths) for key, paths in d["channel_paths"].items()
                    if r["metadata"]["channels"][key]["selected"])
        self.expression_messages.setText(f"{count} TIFF(s) selected. Review channels, paths, mouse, and date before saving.")
        self.tabs.setCurrentWidget(self.expression_page)

    def collect_expression(self):
        if not self.expression_draft:
            raise LoggerError("Drop or choose an expression folder first")
        d = deepcopy(self.expression_draft)
        r = d["record"]
        r.update(schema_version=SCHEMA_VERSION, mouse_id=self.expression_mouse.text().strip() or None,
                 session_date=self.expression_date.text().strip() or None, operator=self.config.get("operator"),
                 notes=self.expression_notes.toPlainText().strip() or None)
        for key in CHANNELS:
            r["metadata"]["channels"][key]["selected"] = self.expression_checks[key].isChecked()
            d["channel_paths"][key] = [line.strip() for line in self.expression_paths[key].toPlainText().splitlines() if line.strip()]
        return d

    def choose_expression_files(self, key):
        files, _ = QFileDialog.getOpenFileNames(self, f"Add {CHANNELS[key][0]} TIFFs", "", "TIFF images (*.tif *.tiff *.TIF *.TIFF)")
        if files:
            existing = [line.strip() for line in self.expression_paths[key].toPlainText().splitlines() if line.strip()]
            self.expression_paths[key].setPlainText("\n".join(dict.fromkeys([*existing, *files])))
            self.expression_checks[key].setChecked(True)

    def save_expression_draft(self):
        try:
            self.expression_draft = self.store.save_draft(self.collect_expression())
            self.expression_messages.setText(f"Expression draft saved — revision {self.expression_draft['revision']}")
            self.refresh_drafts()
        except Exception as exc:
            self.error(str(exc))

    def open_expression_draft(self):
        try:
            rid = self.expression_draft_picker.currentData()
            if rid and self._replace_expression():
                self.expression_draft = self.store.load_draft(rid)
                self.expression_mouse_picker.blockSignals(True)
                self.expression_mouse_picker.clear()
                folder = self.expression_draft["source_folder"]
                self.expression_mouse_picker.addItem(Path(folder).name, folder)
                self.expression_mouse_picker.blockSignals(False)
                self.fill_expression_form()
        except Exception as exc:
            self.error(str(exc))

    def validate_expression_form(self):
        try:
            items = selection(self.collect_expression())
            self.expression_messages.setText(f"Entry fields and {len(items)} TIFF signature(s) are valid. Full hashes are verified during copying.")
        except Exception as exc:
            self.error(str(exc))

    def finalize_expression_form(self):
        try:
            d = self.collect_expression()
            if not self.expression_complete.isChecked():
                raise LoggerError("Confirm acquisition has finished and source TIFFs are closed")
            plan = self.store.plan_expression(d)
            move = self.expression_move.isChecked()
            lines = [f"{CHANNELS[item['channel']][0]}: {item['source']}\n  → {Path(self.store.roots['archive']) / item['relative_path']}" for item in plan["items"]]
            question = ("Move and verify these TIFFs?\n\n" if move else "Copy and verify these TIFFs?\n\n") + "\n\n".join(lines)
            question += f"\n\nMetadata: {self.store.metadata / 'records' / plan['layout']['directory_name']}\n\n"
            question += ('A direct move is attempted first. Otherwise a verified copy is made and the original is removed after the metadata Git commit. Already moved files stay in the archive after cancellation; use Recover to resume.' if move else 'Original files will be retained.')
            if QMessageBox.question(self, "Finalize expression check", question) != QMessageBox.StandardButton.Yes:
                return
            self.expression_draft = self.store.save_draft(d)
            frozen = deepcopy(self.expression_draft)
            self.run_job(lambda progress, cancel: self.store.finalize_expression(frozen, move=move, source_complete=True,
                         progress=progress, cancel=cancel), self.expression_finalized)
        except Exception as exc:
            self.error(str(exc))

    def expression_finalized(self, receipt):
        self.expression_draft = None
        self.clear_expression_fields()
        self.expression_source.clear()
        moved = all(f.get('source_removed', False) for f in receipt['files'])
        pending = any(f.get('method') for f in receipt['files']) and not moved
        action = 'Moved' if moved else 'Transferred' if pending else 'Copied'
        self.expression_messages.setText(f"{action} {len(receipt['files'])} TIFF(s); finalized locally · " +
            ("Git committed" if receipt["git_committed"] else "Git pending — use Recover in Browse") +
            f" · {'Sources moved' if moved else 'Source removal pending — use Recover' if pending else 'Sources retained'}\nMetadata: {self.store.metadata / receipt['record_directory']}")
        self.status.setText(self.expression_messages.text())
