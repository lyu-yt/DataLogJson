"""Two-area stimulation form. Parsing and moving live in independent modules."""
from copy import deepcopy
from pathlib import Path
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QLabel, QWidget, QScrollArea, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QPlainTextEdit, QComboBox, QCheckBox, QFileDialog,
    QMessageBox, QTableWidget, QTableWidgetItem, QHeaderView)

from .io import LoggerError, loads
from .stim import (add_inputs, choose_behavior_csv, new_stim, refresh_behavior,
                   refresh_parameters)
from .stim_parameters import TEXT_PARAMETERS


def logged_parameter_value(parameter, text):
    """Choose the editor's type by field meaning, even when the source is null."""
    text = text.strip()
    if not text:
        return None
    if parameter['name'] in TEXT_PARAMETERS or isinstance(parameter['value'], str):
        return text
    try:
        value = loads(text)
        if isinstance(value, (dict, str)):
            raise ValueError('Expected a numeric, boolean, or array value')
        return value
    except (ValueError, LoggerError) as exc:
        label = parameter['name'].replace('_', ' ')
        unit = f" in {parameter['unit']}" if parameter['unit'] else ''
        expected = ('true/false or an array of those values' if isinstance(parameter['value'], bool)
                    else f'a number{unit} or a numeric array such as [1, 2]')
        raise LoggerError(f"{label}: enter {expected}. Leave blank if unknown.") from exc


class FilesDrop(QLabel):
    dropped = Signal(list)

    def __init__(self, text):
        super().__init__(text)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(65)
        self.setObjectName("dropzone")

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if (urls and all(u.isLocalFile() for u in urls)
                and event.possibleActions() & Qt.DropAction.CopyAction):
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if (urls and all(u.isLocalFile() for u in urls)
                and event.possibleActions() & Qt.DropAction.CopyAction):
            self.dropped.emit([u.toLocalFile() for u in urls])
            # Dragging only selects files; the source must not delete anything.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()


class StimTab:
    def _stim_tab(self):
        self.stim_draft = None
        outer = self._page("Stim")
        self.stim_page = outer.parentWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        saved = QHBoxLayout()
        self.stim_draft_picker = QComboBox()
        saved.addWidget(self.stim_draft_picker, 1)
        self._button("Open saved draft", self.open_stim_draft, saved)
        self._button("New session", self.new_stim_form, saved)
        layout.addLayout(saved)
        self.stim_drops = {}
        for area, title in (("imaging", "1 · Imaging / recording files\nDrop TIFFs, recording DATs, or a folder → imaging/"),
                            ("stimpara", "2 · Stim parameters / other files\nDrop stimPara*.mat, stim_parameters.csv, and optional other files")):
            drop = FilesDrop(title)
            drop.dropped.connect(lambda paths, a=area: self.load_stim_files(paths, a))
            self.stim_drops[area] = drop
            layout.addWidget(drop)
            buttons = QHBoxLayout()
            self._button("Choose files…", lambda _=False, a=area: self.choose_stim_files(a), buttons)
            self._button("Choose folder…", lambda _=False, a=area: self.choose_stim_folder(a), buttons)
            layout.addLayout(buttons)
        self.stim_files = QTableWidget(0, 3)
        self.stim_files.setHorizontalHeaderLabels(["Destination folder", "File / relative name", "Source path"])
        self.stim_files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stim_files.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stim_files.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.stim_files.horizontalHeader().setStretchLastSection(True)
        self.stim_files.setMinimumHeight(130)
        layout.addWidget(self.stim_files)
        self._button("Remove selected files", self.remove_stim_files, layout)
        form = QFormLayout()
        self.stim_mouse, self.stim_date = QLineEdit(), QLineEdit()
        self.stim_mouse.setPlaceholderText("Three digits; parameter fields, then folder name")
        self.stim_date.setPlaceholderText("YYYY-MM-DD; inferred from folder date")
        form.addRow("Mouse ID", self.stim_mouse)
        form.addRow("Experiment date", self.stim_date)
        layout.addLayout(form)
        self.stim_behavior = QCheckBox("Behavior session — link a robot CSV")
        self.stim_behavior.toggled.connect(self.refresh_stim_behavior)
        layout.addWidget(self.stim_behavior)
        behavior = QHBoxLayout()
        self.stim_behavior_picker = QComboBox()
        self.stim_behavior_picker.setMinimumContentsLength(24)
        self.stim_behavior_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.stim_behavior_picker.setEnabled(False)
        self.stim_behavior_picker.currentIndexChanged.connect(self.stim_behavior_selected)
        behavior.addWidget(self.stim_behavior_picker, 1)
        self._button("Find matching robot CSVs", self.refresh_stim_behavior, behavior)
        self._button("Choose robot CSV…", self.choose_stim_robot_csv, behavior)
        layout.addLayout(behavior)
        self.stim_behavior_path = QLineEdit()
        self.stim_behavior_path.setReadOnly(True)
        self.stim_behavior_path.setPlaceholderText("Check Behavior session to find a logged CSV with the same mouse/date")
        layout.addWidget(self.stim_behavior_path)
        behavior_help = QLabel('A unique same-mouse/date logged CSV is filled automatically. Choose a CSV to match it by content; '
                              'if it is new, log it in Behavior, then return and find matches.')
        behavior_help.setWordWrap(True)
        layout.addWidget(behavior_help)
        self.stim_mouse.editingFinished.connect(self.refresh_stim_behavior_if_checked)
        self.stim_date.editingFinished.connect(self.refresh_stim_behavior_if_checked)
        form = QFormLayout()
        self.stim_choices = {}
        for role, label in (("parameters_csv", "Parameter CSV to read"), ("parameters_mat", "Parameter MAT to read")):
            picker = QComboBox()
            picker.currentIndexChanged.connect(self.stim_parameter_choice_changed)
            form.addRow(label, picker)
            self.stim_choices[role] = picker
        layout.addLayout(form)
        self._button("Refresh parameters", self.refresh_stim_parameters, layout)
        help_ = QLabel("Review saved settings below. Edit Logged value for a correction; original extracted values are retained. "
                       "Arrays use [value, value]. Spiral size uses ScanImage units. These settings do not confirm delivered pulses.")
        help_.setWordWrap(True)
        layout.addWidget(help_)
        self.stim_parameters = QTableWidget(0, 4)
        self.stim_parameters.setHorizontalHeaderLabels(["Parameter", "Extracted value", "Logged value", "Unit"])
        self.stim_parameters.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate((225, 185, 185, 115)):
            self.stim_parameters.setColumnWidth(col, width)
        self.stim_parameters.setMinimumHeight(220)
        self.stim_parameters.itemChanged.connect(lambda: self.stim_reviewed.setChecked(False))
        layout.addWidget(self.stim_parameters)
        self.stim_warnings = QLabel()
        self.stim_warnings.setWordWrap(True)
        layout.addWidget(self.stim_warnings)
        self.stim_notes = QPlainTextEdit()
        self.stim_notes.setPlaceholderText("Optional experiment notes / explanation of corrections")
        self.stim_notes.setFixedHeight(60)
        layout.addWidget(self.stim_notes)
        self.stim_reviewed = QCheckBox("I reviewed the mouse, date, extracted settings, and any warnings")
        self.stim_complete = QCheckBox("Acquisition is finished and all selected files are closed")
        layout.addWidget(self.stim_reviewed)
        layout.addWidget(self.stim_complete)
        self.stim_mouse.textEdited.connect(lambda: self.stim_reviewed.setChecked(False))
        self.stim_date.textEdited.connect(lambda: self.stim_reviewed.setChecked(False))
        destination = QLabel("Destination: <archive>/stim/mouse_<ID>/YYMMDD/<number>/imaging, stimPara, or other.\n"
            "Nothing moves when dropped. Review destinations and confirm before moving.")
        destination.setWordWrap(True)
        layout.addWidget(destination)
        actions = QHBoxLayout()
        self._button("Save draft", self.save_stim_draft, actions)
        self._button("Review / move files…", self.finalize_stim_form, actions).setObjectName("primary")
        self._button("Resume pending transfers", self.recover, actions)
        outer.addLayout(actions)
        self.stim_messages = QLabel("Drop imaging and parameter files to begin.")
        self.stim_messages.setWordWrap(True)
        outer.addWidget(self.stim_messages)

    def choose_stim_files(self, area):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose completed files", "", "All files (*)")
        if paths:
            self.load_stim_files(paths, area)

    def choose_stim_folder(self, area):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder; its files and subfolders will be listed")
        if folder:
            self.load_stim_files([folder], area)

    def load_stim_files(self, paths, area):
        try:
            d = self.collect_stim() if self.stim_draft else new_stim()
            d["record"]["operator"] = self.config.get("operator")
            self.tabs.setCurrentWidget(self.stim_page)
            def work(progress, cancel):
                progress.phase("Reading stimulation settings")
                result = add_inputs(d, paths, area, cancel=cancel)
                return refresh_behavior(self.store, result) if self.store and result['record']['metadata']['behavior_session'] else result
            self.run_job(work, self.stim_loaded, refresh_archive=False)
        except Exception as exc:
            self.error(str(exc))

    def stim_loaded(self, draft):
        self.stim_draft = draft
        self.fill_stim_form()
        self.stim_messages.setText(f"{len(draft['inputs'])} files selected. Review the destination folders and settings; no files have moved.")

    def collect_stim(self):
        if self.stim_draft is None:
            raise LoggerError("Drop files to begin a stimulation session")
        d = deepcopy(self.stim_draft)
        r = d["record"]
        r.update(mouse_id=self.stim_mouse.text().strip() or None,
                 session_date=self.stim_date.text().strip() or None,
                 operator=self.config.get("operator"), notes=self.stim_notes.toPlainText().strip() or None)
        report = r["metadata"]["extraction"]
        overrides = []
        for row, parameter in enumerate(report["parameters"] if report else []):
            cell = self.stim_parameters.item(row, 2)
            try:
                value = logged_parameter_value(parameter, cell.text())
            except LoggerError:
                self.stim_parameters.setCurrentCell(row, 2)
                self.stim_parameters.scrollToItem(cell)
                self.stim_parameters.setFocus()
                raise
            if value != parameter["value"]:
                overrides.append({"name": parameter["name"], "value": value})
        r["metadata"].update(parameter_overrides=overrides, reviewed=self.stim_reviewed.isChecked())
        behavior = self.stim_behavior_picker.currentData() if self.stim_behavior.isChecked() else None
        r["metadata"].update(behavior_session=self.stim_behavior.isChecked(), behavior=behavior)
        r["related_records"] = [{k: behavior[k] for k in ("record_id", "sha256")}] if behavior else []
        for role, picker in self.stim_choices.items():
            d["parameter_choices"][role] = picker.currentData()
        return d

    def fill_stim_form(self):
        d = self.stim_draft
        if d is None:
            return
        r = d["record"]
        self.stim_mouse.setText(r["mouse_id"] or "")
        self.stim_date.setText(r["session_date"] or "")
        self.stim_notes.setPlainText(r["notes"] or "")
        self.show_stim_behavior()
        self.stim_complete.setChecked(False)
        self.stim_files.setRowCount(len(d["inputs"]))
        for row, item in enumerate(d["inputs"]):
            for col, value in enumerate((item["bucket"], item["relative_name"], item["source"])):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.stim_files.setItem(row, col, cell)
        for role, picker in self.stim_choices.items():
            picker.blockSignals(True)
            picker.clear()
            picker.addItem("Choose a file" if any(i["role"] == role for i in d["inputs"]) else "Not supplied", None)
            for item in d["inputs"]:
                if item["role"] == role and item["bucket"] == "stimPara":
                    picker.addItem(item["source"], item["file_id"])
            picker.setCurrentIndex(max(0, picker.findData(d["parameter_choices"][role])))
            picker.blockSignals(False)
        report = r["metadata"]["extraction"]
        parameters = report["parameters"] if report else []
        overrides = {p["name"]: p["value"] for p in r["metadata"]["parameter_overrides"]}
        self.stim_parameters.blockSignals(True)
        self.stim_parameters.setRowCount(len(parameters))
        for row, p in enumerate(parameters):
            def display(value):
                return '' if value is None else value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            values = (p["name"].replace("_", " "), display(p["value"]),
                      display(overrides.get(p["name"], p["value"])), p["unit"] or "")
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(f"{p['source_role']}: {p['source_field']}\n{value}")
                if col != 2:
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.stim_parameters.setItem(row, col, cell)
        self.stim_parameters.blockSignals(False)
        self.stim_warnings.setText("\n".join(report["warnings"]) if report else "")
        self.stim_reviewed.setChecked(r["metadata"]["reviewed"])

    def refresh_stim_parameters(self):
        try:
            d = self.collect_stim()
            def work(progress, cancel):
                updated = refresh_parameters(d, cancel=cancel)
                return refresh_behavior(self.store, updated) if self.store and updated['record']['metadata']['behavior_session'] else updated
            self.run_job(work, self.stim_loaded, refresh_archive=False)
        except Exception as exc:
            # A rejected table edit must not leave a new source selected while
            # the preview still describes the previous file. Retain all edits.
            if self.stim_draft is not None:
                for role, picker in self.stim_choices.items():
                    picker.blockSignals(True)
                    picker.setCurrentIndex(max(0, picker.findData(self.stim_draft['parameter_choices'][role])))
                    picker.blockSignals(False)
            self.error(str(exc))

    def stim_parameter_choice_changed(self, _index=None):
        if self.stim_draft is not None:
            self.refresh_stim_parameters()

    def show_stim_behavior(self):
        d = self.stim_draft
        m = d['record']['metadata']
        self.stim_behavior.blockSignals(True)
        self.stim_behavior.setChecked(m['behavior_session'])
        self.stim_behavior.blockSignals(False)
        picker = self.stim_behavior_picker
        picker.blockSignals(True)
        picker.clear()
        choices = d.get('behavior_choices', [])
        picker.addItem('Choose a matching session' if choices else 'No matching logged CSV', None)
        selected = 0
        for choice in choices:
            picker.addItem(choice['label'], choice['reference'])
            if choice['reference'] == m['behavior']:
                selected = picker.count()-1
        picker.setCurrentIndex(selected)
        picker.setEnabled(m['behavior_session'])
        picker.blockSignals(False)
        path = next((c['path'] for c in choices if c['reference'] == m['behavior']), '')
        self.stim_behavior_path.setText(path)
        self.stim_behavior_path.setToolTip(path)

    def refresh_stim_behavior_if_checked(self):
        if self.stim_behavior.isChecked():
            self.refresh_stim_behavior()

    def refresh_stim_behavior(self, _checked=None):
        if self.stim_draft is None:
            return
        try:
            if not self.store:
                raise LoggerError('Configure storage in Settings first')
            d = self.collect_stim()
            if not self.stim_behavior.isChecked():
                d['preferred_behavior_csv_sha256'] = None
                d['pending_behavior_source'] = None
            def work(progress, cancel):
                progress.phase('Finding robot CSVs for this mouse/date')
                return refresh_behavior(self.store, d)
            self.run_job(work, self.stim_loaded, refresh_archive=False)
        except Exception as exc:
            self.error(str(exc))

    def stim_behavior_selected(self, _index=None):
        if self.stim_draft is None:
            return
        ref = self.stim_behavior_picker.currentData()
        self.stim_draft['record']['metadata']['behavior'] = deepcopy(ref)
        self.stim_draft['preferred_behavior_csv_sha256'] = None
        self.stim_draft['pending_behavior_source'] = None
        self.stim_behavior_path.setText(next((c['path'] for c in self.stim_draft.get('behavior_choices', [])
                                             if c['reference'] == ref), ''))
        self.stim_reviewed.setChecked(False)

    def choose_stim_robot_csv(self):
        try:
            if not self.store:
                raise LoggerError('Configure storage in Settings first')
            d = self.collect_stim()
            if not d['record']['mouse_id'] or not d['record']['session_date']:
                raise LoggerError('Enter the stimulation mouse and date before choosing a robot CSV')
            path, _ = QFileDialog.getOpenFileName(self, 'Choose a robot CSV', '', 'Robot CSV (*.csv)')
            if path:
                self.run_job(lambda p, c: choose_behavior_csv(self.store, d, path, progress=p, cancel=c),
                             self.stim_robot_csv_chosen, refresh_archive=False)
        except Exception as exc:
            self.error(str(exc))

    def stim_robot_csv_chosen(self, draft):
        self.stim_loaded(draft)
        if not draft.get('behavior_choices'):
            # Reuse the normal behavior form for required weight/task/day fields.
            # Its own Finalize action registers the CSV; it never edits Stim.
            self.tabs.setCurrentIndex(0)
            self.load_source(draft['pending_behavior_source'])
            self.stim_messages.setText('Log the selected CSV in Behavior, then return here and click Find matching robot CSVs. The Stim form is retained.')
            self.status.setText(self.stim_messages.text())

    def remove_stim_files(self):
        try:
            d = self.collect_stim()
            rows = {i.row() for i in self.stim_files.selectedIndexes()}
            d["inputs"] = [item for row, item in enumerate(d["inputs"]) if row not in rows]
            def work(progress, cancel):
                updated = refresh_parameters(d, cancel=cancel)
                return refresh_behavior(self.store, updated) if self.store and updated['record']['metadata']['behavior_session'] else updated
            self.run_job(work, self.stim_loaded, refresh_archive=False)
        except Exception as exc:
            self.error(str(exc))

    def new_stim_form(self):
        if self.stim_draft and QMessageBox.question(self, "New stimulation session",
                "Clear this form? Save a draft first to retain it.") != QMessageBox.StandardButton.Yes:
            return
        self.stim_draft = new_stim()
        self.fill_stim_form()

    def save_stim_draft(self):
        try:
            if not self.store:
                raise LoggerError("Configure storage in Settings first")
            self.stim_draft = self.store.save_draft(self.collect_stim())
            self.refresh_drafts()
            self.stim_messages.setText(f"Stimulation draft saved · revision {self.stim_draft['revision']}")
        except Exception as exc:
            self.error(str(exc))

    def open_stim_draft(self):
        try:
            rid = self.stim_draft_picker.currentData()
            if not rid:
                return
            if self.stim_draft and QMessageBox.question(self, "Open stimulation draft",
                    "Replace this form? Save it first to retain edits.") != QMessageBox.StandardButton.Yes:
                return
            self.stim_draft = self.store.load_draft(rid)
            self.fill_stim_form()
        except Exception as exc:
            self.error(str(exc))

    def finalize_stim_form(self):
        try:
            if not self.store:
                raise LoggerError("Configure storage in Settings first")
            d = self.collect_stim()
            if not self.stim_complete.isChecked():
                raise LoggerError("Confirm acquisition is finished and all files are closed")
            plan = self.store.plan_stim(d)
            lines = [f"{i['source']}\n  → {Path(self.store.roots['archive']) / i['relative_path']}" for i in plan["items"]]
            question = ("Move and register these files?\n\n" + "\n\n".join(lines) +
                "\n\nDirect moves are attempted first. Otherwise copies are verified before originals are removed after the metadata Git commit. "
                "Already moved files stay in the archive after cancellation; use Recover to resume.\n\n" +
                f"Metadata: {self.store.metadata / 'records' / plan['layout']['directory_name']}")
            if QMessageBox.question(self, "Move stimulation files", question) != QMessageBox.StandardButton.Yes:
                return
            self.stim_draft = self.store.save_draft(d)
            frozen = deepcopy(self.stim_draft)
            self.run_job(lambda p, c: self.store.finalize_stim(frozen, move_confirmed=True,
                         source_complete=True, progress=p, cancel=c), self.stim_finalized)
        except Exception as exc:
            self.error(str(exc))

    def stim_finalized(self, receipt):
        self.stim_draft = new_stim()
        self.fill_stim_form()
        self.stim_draft = None
        self.stim_messages.setText("Finalized locally · " + ("Git committed" if receipt["git_committed"] else "Git pending — use Recover") +
            " · " + ("Sources moved" if receipt["source_removed"] else "Source removal pending — use Recover") +
            f"\nMetadata: {self.store.metadata / receipt['record_directory']}")
        self.status.setText(self.stim_messages.text())
