"""Mouse-colony editor. Identity choices are explicit and persistence uses Store."""
from copy import deepcopy
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QScrollArea, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QTextEdit, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QMessageBox)

from .colony import COLONY_FIELDS, new_profile, require_profile, search_workbook, import_candidate
from .io import LoggerError, atomic_json, uid
from .profiles import relative_path


class ColonyTab:
    def _colony_tab(self):
        self.colony_profile = None
        self.colony_saved_profile = None
        self.colony_matches = []
        outer = self._page("Mouse colony")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        identity = QHBoxLayout()
        identity.addWidget(QLabel("Local mouse ID"))
        self.colony_mouse = QLineEdit()
        self.colony_mouse.setPlaceholderText("007")
        self.colony_mouse.setMaximumWidth(100)
        identity.addWidget(self.colony_mouse)
        self._button("Open / create profile", self.open_colony_profile, identity)
        identity.addStretch()
        layout.addLayout(identity)
        self.colony_path = QLabel("One editable mouse_info.json per mouse. Changes are recorded in local Git history.")
        self.colony_path.setWordWrap(True)
        layout.addWidget(self.colony_path)
        workbook = QHBoxLayout()
        self.colony_workbook = QLineEdit(self.config.get("colony_workbook", ""))
        self.colony_workbook.setPlaceholderText("Choose a colony .xlsx file")
        workbook.addWidget(self.colony_workbook)
        self._button("Choose XLSX…", self.choose_colony_workbook, workbook)
        self._button("Find matching mice", self.search_colony, workbook)
        layout.addLayout(workbook)
        self.colony_match_info = QLabel("Search by the numeric ID; select and confirm a workbook row before importing.")
        self.colony_match_info.setWordWrap(True)
        layout.addWidget(self.colony_match_info)
        self.colony_results = QTableWidget(0, 8)
        self.colony_results.setHorizontalHeaderLabels(["Colony ID", "Strain", "Sex", "DOB", "Cage", "System ID", "Source row", "Notes"])
        self.colony_results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.colony_results.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.colony_results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.colony_results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.colony_results.setMinimumHeight(130)
        self.colony_results.setMaximumHeight(190)
        self.colony_results.itemSelectionChanged.connect(self.colony_selection_changed)
        layout.addWidget(self.colony_results)
        self.colony_import = self._button("Import selected row into form", self.import_colony_row, layout)
        self.colony_import.setEnabled(False)
        self.colony_edits = {}
        form = QFormLayout()
        labels = {"colony_id": "Colony ID (exact workbook ID)", "date_of_birth": "Date of birth (YYYY-MM-DD)",
                  "use_status": "Use / status", "system_id": "Colony system ID", "cage_id": "Cage ID",
                  "colony_notes": "Colony notes (from workbook)", "notes": "Additional notes (manual)"}
        for field in [*COLONY_FIELDS.values(), "notes"]:
            edit = QTextEdit() if field in ("colony_notes", "notes") else QLineEdit()
            if isinstance(edit, QTextEdit):
                edit.setMaximumHeight(85)
            self.colony_edits[field] = edit
            form.addRow(labels.get(field, field.replace("_", " ").capitalize()), edit)
        layout.addLayout(form)
        layout.addWidget(QLabel("Surgeries — enter dates and details manually; workbook notes are not interpreted as surgeries."))
        self.colony_surgeries = QTableWidget(0, 3)
        self.colony_surgeries.setHorizontalHeaderLabels(["Date (YYYY-MM-DD)", "Surgery details", "Surgeon (optional)"])
        self.colony_surgeries.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.colony_surgeries.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.colony_surgeries.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.colony_surgeries.setMinimumHeight(135)
        self.colony_surgeries.setMaximumHeight(190)
        layout.addWidget(self.colony_surgeries)
        surgery_actions = QHBoxLayout()
        self._button("Add surgery", self.add_surgery, surgery_actions)
        self._button("Remove selected surgery", self.remove_surgery, surgery_actions)
        surgery_actions.addStretch()
        layout.addLayout(surgery_actions)
        # Keep the save action visible even while the long profile form scrolls.
        self._button("Save mouse profile", self.save_colony_profile, outer).setObjectName("primary")
        self.colony_receipt = QLabel("")
        self.colony_receipt.setWordWrap(True)
        outer.addWidget(self.colony_receipt)
        self.colony_provenance = QPlainTextEdit()
        self.colony_provenance.setReadOnly(True)
        self.colony_provenance.setMaximumHeight(110)
        self.colony_provenance.setPlaceholderText("Import history: source workbook, sheet/row, exact ID, and SHA-256")
        layout.addWidget(QLabel("Workbook import history (read-only)"))
        layout.addWidget(self.colony_provenance)

    def open_colony_profile(self):
        try:
            if not self.store:
                raise LoggerError("Initialize storage in Settings first")
            mouse_id = self.colony_mouse.text().strip() or self.mouse.text().strip()
            if self.colony_profile and self.collect_colony_profile(check_mouse=False) != self.colony_saved_profile:
                if QMessageBox.question(self, "Unsaved profile", "Discard unsaved profile edits and open the selected mouse?") != QMessageBox.StandardButton.Yes:
                    return
            profile = self.store.load_mouse_profile(mouse_id) or new_profile(mouse_id, self.config["operator"])
            self.colony_profile = deepcopy(profile)
            self.colony_saved_profile = deepcopy(profile)
            self.colony_mouse.setText(mouse_id)
            self.show_colony_matches([])
            self.fill_colony_form()
            self.colony_receipt.setText(f"Revision {profile['revision']}" + (" · not saved yet" if not profile["revision"] else ""))
        except Exception as exc:
            self.error(str(exc))

    def fill_colony_form(self):
        profile = self.colony_profile
        for field, edit in self.colony_edits.items():
            value = profile.get(field) or ""
            edit.setPlainText(value) if isinstance(edit, QTextEdit) else edit.setText(value)
        self.colony_path.setText(str(self.store.metadata / relative_path(profile["mouse_id"])))
        self.colony_surgeries.setRowCount(0)
        for surgery in profile["surgeries"]:
            self.add_surgery(surgery)
        self.colony_provenance.setPlainText(json.dumps(profile["import_history"], indent=2, ensure_ascii=False))

    def collect_colony_profile(self, check_mouse=True):
        if not self.colony_profile:
            raise LoggerError("Open or create a mouse profile first")
        if check_mouse and self.colony_mouse.text().strip() != self.colony_profile["mouse_id"]:
            raise LoggerError("Mouse ID changed. Use Open / create profile before editing this mouse.")
        profile = deepcopy(self.colony_profile)
        for field, edit in self.colony_edits.items():
            value = edit.toPlainText() if isinstance(edit, QTextEdit) else edit.text()
            profile[field] = value.strip() or None
        profile["surgeries"] = []
        for row in range(self.colony_surgeries.rowCount()):
            items = [self.colony_surgeries.item(row, col) for col in range(3)]
            values = [(item.text().strip() if item else "") for item in items]
            if not any(values):
                continue
            profile["surgeries"].append({"surgery_id": items[0].data(Qt.ItemDataRole.UserRole) if items[0] else uid(),
                "date": values[0], "details": values[1], "surgeon": values[2] or None})
        return profile

    def add_surgery(self, surgery=None):
        surgery = surgery if isinstance(surgery, dict) else {"surgery_id": uid(), "date": "", "details": "", "surgeon": ""}
        row = self.colony_surgeries.rowCount()
        self.colony_surgeries.insertRow(row)
        for col, key in enumerate(("date", "details", "surgeon")):
            item = QTableWidgetItem(surgery[key] or "")
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, surgery["surgery_id"])
            self.colony_surgeries.setItem(row, col, item)

    def remove_surgery(self):
        row = self.colony_surgeries.currentRow()
        if row >= 0:
            self.colony_surgeries.removeRow(row)

    def choose_colony_workbook(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose colony workbook", "", "Excel workbook (*.xlsx)")
        if path:
            self.colony_workbook.setText(path)

    def search_colony(self):
        try:
            profile = self.collect_colony_profile()
            path = self.colony_workbook.text().strip()
            if not path:
                raise LoggerError("Choose a colony XLSX file")
            self.colony_profile = profile
            self.config["colony_workbook"] = path
            atomic_json(self.config_path, self.config)
            self.show_colony_matches([])
            self.run_job(lambda progress, cancel: search_workbook(path, profile["mouse_id"], progress, cancel), self.show_colony_matches)
        except Exception as exc:
            self.error(str(exc))

    def show_colony_matches(self, candidates):
        self.colony_matches = candidates
        self.colony_results.setRowCount(0)
        self.colony_results.setRowCount(len(candidates))
        for row, candidate in enumerate(candidates):
            fields, source = candidate["fields"], candidate["source"]
            values = [candidate["colony_id"], *[fields.get(k) for k in ("strain", "sex", "date_of_birth", "cage_id", "system_id")],
                      f"{source['sheet']}!{source['row']}", fields.get("colony_notes")]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                self.colony_results.setItem(row, col, item)
        self.colony_results.clearSelection()
        self.colony_results.setCurrentCell(-1, -1)
        self.colony_import.setEnabled(False)
        ambiguous = len(candidates) > 1 or any(c["requires_confirmation"] for c in candidates)
        self.colony_match_info.setText((f"{len(candidates)} matching row(s). " +
            ("Prefixed or repeated IDs: choose the correct mouse yourself. " if ambiguous else "") +
            "Select a row and click Import; save the profile separately.") if candidates else "No matching rows displayed. Open a mouse profile and search the workbook.")

    def colony_selection_changed(self):
        self.colony_import.setEnabled(bool(self.colony_results.selectionModel().selectedRows()))

    def import_colony_row(self):
        try:
            selection = self.colony_results.selectionModel().selectedRows()
            if not selection:
                raise LoggerError("Select the correct colony row")
            candidate = self.colony_matches[selection[0].row()]
            profile = self.collect_colony_profile()
            source = candidate["source"]
            question = (f"Use colony ID {candidate['colony_id']} for local mouse {profile['mouse_id']}?\n\n"
                f"Source: {source['workbook_filename']} — {source['sheet']}, row {source['row']}\n\n"
                + json.dumps(candidate["fields"], indent=2, ensure_ascii=False)
                + "\n\nThese values replace the colony fields in the form. Manual notes and surgeries are retained. Save the profile separately.")
            if profile.get("colony_id") and profile["colony_id"] != candidate["colony_id"]:
                question += f"\n\nThe previous colony ID was {profile['colony_id']}. Confirm this identity change."
            if QMessageBox.question(self, "Confirm colony identity", question) != QMessageBox.StandardButton.Yes:
                return
            self.colony_profile = import_candidate(profile, candidate, self.config["operator"], confirmed=True)
            self.fill_colony_form()
            self.colony_receipt.setText("Imported into form · Save mouse profile to keep these changes")
        except Exception as exc:
            self.error(str(exc))

    def save_colony_profile(self):
        try:
            profile = self.collect_colony_profile()
            require_profile(profile)
            self.run_job(lambda _progress, _cancel: self.store.save_mouse_profile(profile, self.config["operator"]), self.colony_saved)
        except Exception as exc:
            self.error(str(exc))

    def colony_saved(self, result):
        self.colony_profile = deepcopy(result["profile"])
        self.colony_saved_profile = deepcopy(result["profile"])
        self.fill_colony_form()
        receipt = result["receipt"]
        self.colony_receipt.setText(f"Revision {receipt['revision']} saved locally · " +
            ("Git committed" if receipt["git_committed"] else "Git pending — use Recover in Browse") + " · Backup not confirmed")
        self.status.setText(self.colony_receipt.text())
