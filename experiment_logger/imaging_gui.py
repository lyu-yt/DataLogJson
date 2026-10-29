"""Imaging entry form; transfer and identity rules live in imaging.py."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QScrollArea, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QPlainTextEdit, QLabel, QComboBox, QCheckBox, QFileDialog, QMessageBox)

from .expression import mouse_folders
from .expression_gui import FolderDrop
from .imaging import SIDES
from .io import LoggerError
from .models import SCHEMA_VERSION


class ImagingTab:
    def _imaging_tab(self):
        self.imaging_draft = None
        outer = self._page('Imaging')
        self.imaging_page = outer.parentWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.imaging_drop = FolderDrop()
        self.imaging_drop.setText('Drop an imaging mouse folder or date folder here')
        self.imaging_drop.dropped.connect(self.load_imaging_folder)
        layout.addWidget(self.imaging_drop)
        source = QHBoxLayout()
        self.imaging_source = QLineEdit()
        self.imaging_source.setReadOnly(True)
        source.addWidget(self.imaging_source)
        self._button('Choose folder…', self.choose_imaging_folder, source)
        layout.addLayout(source)
        picks = QHBoxLayout()
        picks.addWidget(QLabel('Mouse folder'))
        self.imaging_mouse_picker = QComboBox()
        self.imaging_mouse_picker.currentIndexChanged.connect(self.choose_imaging_mouse)
        picks.addWidget(self.imaging_mouse_picker)
        self.imaging_draft_picker = QComboBox()
        self.imaging_draft_picker.setMinimumContentsLength(16)
        self.imaging_draft_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        picks.addWidget(self.imaging_draft_picker, 1)
        self._button('Open saved draft', self.open_imaging_draft, picks)
        layout.addLayout(picks)
        identity = QFormLayout()
        self.imaging_mouse, self.imaging_date = QLineEdit(), QLineEdit()
        self.imaging_mouse.setPlaceholderText('Three digits')
        self.imaging_date.setPlaceholderText('YYYY-MM-DD')
        identity.addRow('Mouse ID', self.imaging_mouse)
        identity.addRow('Experiment date', self.imaging_date)
        layout.addLayout(identity)
        self.imaging_paths, self.imaging_powers, self.imaging_colors = {}, {}, {}
        for side in SIDES:
            file_row = QHBoxLayout()
            file_row.addWidget(QLabel(side.capitalize() + ' TIFF'))
            path = QLineEdit()
            path.setPlaceholderText('Full TIFF path; leave blank if this side was not recorded')
            file_row.addWidget(path, 1)
            self._button('Choose TIFF…', lambda _checked=False, s=side: self.choose_imaging_tiff(s), file_row)
            layout.addLayout(file_row)
            fields = QHBoxLayout()
            fields.addWidget(QLabel('Laser power'))
            power = QLineEdit()
            power.setPlaceholderText('Optional; enter values, units, and words')
            fields.addWidget(power, 1)
            colors = {}
            for color in ('red', 'green'):
                check = QCheckBox(color.capitalize())
                check.setTristate(True)
                check.setCheckState(Qt.CheckState.PartiallyChecked)
                check.setToolTip('Square = unspecified; tick = present; empty = absent. Review both colors for each recorded side.')
                fields.addWidget(check)
                colors[color] = check
            layout.addLayout(fields)
            self.imaging_paths[side], self.imaging_powers[side], self.imaging_colors[side] = path, power, colors
        color_help = QLabel('Color boxes: square = unspecified, tick = present, empty = absent. TIFF channel numbers do not determine colors.')
        color_help.setWordWrap(True)
        layout.addWidget(color_help)
        behavior = QHBoxLayout()
        behavior.addWidget(QLabel('Behavior session'))
        self.imaging_behavior = QComboBox()
        self.imaging_behavior.addItems(['Choose Yes or No', 'No', 'Yes'])
        behavior.addWidget(self.imaging_behavior)
        self.imaging_behavior.currentIndexChanged.connect(self.imaging_behavior_changed)
        self.imaging_behavior_picker = QComboBox()
        self.imaging_behavior_picker.setMinimumContentsLength(22)
        self.imaging_behavior_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.imaging_behavior_picker.setEnabled(False)
        self.imaging_behavior_picker.currentIndexChanged.connect(self.imaging_behavior_selected)
        behavior.addWidget(self.imaging_behavior_picker, 1)
        self._button('Refresh matching CSVs', self.refresh_imaging_behavior, behavior)
        layout.addLayout(behavior)
        self.imaging_behavior_path = QLabel('Choose Yes to link a logged robot CSV for this mouse/date.')
        self.imaging_behavior_path.setWordWrap(True)
        self.imaging_behavior_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.imaging_behavior_path)
        self._button('Log another robot CSV…', self.log_imaging_behavior, layout)
        self.imaging_notes = QPlainTextEdit()
        self.imaging_notes.setPlaceholderText('Optional imaging notes')
        self.imaging_notes.setFixedHeight(65)
        layout.addWidget(self.imaging_notes)
        self.imaging_complete = QCheckBox('Acquisition is finished and all TIFFs are closed')
        layout.addWidget(self.imaging_complete)
        destination_help = QLabel('Destination: <archive>/imaging/mouse_<ID>/YYMMDD/left/ or right/.\n'
            'Direct moves are used where supported. Otherwise copies are verified before source removal after the metadata commit.')
        destination_help.setWordWrap(True)
        layout.addWidget(destination_help)
        actions = QHBoxLayout()
        self._button('Save draft', self.save_imaging_draft, actions)
        self._button('Review / move TIFFs…', self.finalize_imaging_form, actions).setObjectName('primary')
        self._button('Resume pending transfers', self.recover, actions)
        outer.addLayout(actions)
        self.imaging_messages = QLabel('Drop a folder to begin. Hashing and transfers run in the background.')
        self.imaging_messages.setWordWrap(True)
        outer.addWidget(self.imaging_messages)

    def choose_imaging_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose imaging mouse or date folder')
        if folder:
            self.load_imaging_folder(folder)

    def _replace_imaging(self):
        return not self.imaging_draft or QMessageBox.question(self, 'Replace imaging form',
            'Replace the imaging form? Save the draft first to keep edits. Started transfers can be resumed with Recover.') == QMessageBox.StandardButton.Yes

    def load_imaging_folder(self, folder):
        try:
            if not self.store:
                raise LoggerError('Initialize storage in Settings first')
            choices = mouse_folders(folder)
            if not self._replace_imaging():
                return
            self.imaging_draft = None
            self.clear_imaging_fields()
            self.imaging_source.setText(str(Path(folder).resolve()))
            self.imaging_mouse_picker.blockSignals(True)
            self.imaging_mouse_picker.clear()
            for p in choices:
                self.imaging_mouse_picker.addItem(p.name, str(p))
            self.imaging_mouse_picker.setCurrentIndex(-1)
            self.imaging_mouse_picker.blockSignals(False)
            self.tabs.setCurrentWidget(self.imaging_page)
            if len(choices) == 1:
                self.imaging_mouse_picker.setCurrentIndex(0)
            else:
                self.imaging_messages.setText('Choose the mouse folder to log; each acquisition is a separate record.')
        except Exception as exc:
            self.error(str(exc))

    def choose_imaging_mouse(self):
        folder = self.imaging_mouse_picker.currentData()
        if not folder:
            return
        try:
            if not self._replace_imaging():
                self.imaging_mouse_picker.blockSignals(True)
                self.imaging_mouse_picker.setCurrentIndex(self.imaging_mouse_picker.findData(self.imaging_draft['source_folder']))
                self.imaging_mouse_picker.blockSignals(False)
                return
            self.imaging_draft = self.store.new_imaging(folder)
            self.fill_imaging_form()
        except Exception as exc:
            self.error(str(exc))

    def clear_imaging_fields(self):
        self.imaging_mouse.clear()
        self.imaging_date.clear()
        self.imaging_notes.clear()
        self.imaging_behavior.blockSignals(True)
        self.imaging_behavior.setCurrentIndex(0)
        self.imaging_behavior.blockSignals(False)
        self.imaging_behavior_picker.clear()
        self.imaging_behavior_picker.setEnabled(False)
        self.imaging_complete.setChecked(False)
        for side in SIDES:
            self.imaging_paths[side].clear()
            self.imaging_powers[side].clear()
            for box in self.imaging_colors[side].values():
                box.setCheckState(Qt.CheckState.PartiallyChecked)

    def fill_imaging_form(self):
        d = self.imaging_draft
        r = d['record']
        self.imaging_source.setText(d['source_folder'])
        self.imaging_mouse.setText(r.get('mouse_id') or '')
        self.imaging_date.setText(r.get('session_date') or '')
        self.imaging_notes.setPlainText(r.get('notes') or '')
        for side in SIDES:
            self.imaging_paths[side].setText(d['side_paths'][side] or '')
            self.imaging_powers[side].setText(r['metadata']['sides'][side]['laser_power'] or '')
            for color, box in self.imaging_colors[side].items():
                value = r['metadata']['sides'][side]['channels'][color]
                box.setCheckState(Qt.CheckState.PartiallyChecked if value is None else Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
        m = r['metadata']
        self.imaging_behavior.blockSignals(True)
        self.imaging_behavior.setCurrentIndex(0 if m['behavior_session'] is None else 2 if m['behavior_session'] else 1)
        self.imaging_behavior.blockSignals(False)
        self.imaging_behavior_picker.clear()
        self.imaging_behavior_picker.setEnabled(m['behavior_session'] is True)
        if m['behavior']:
            self.imaging_behavior_picker.addItem('Linked robot CSV · ' + m['behavior']['record_id'][:8], {'reference': m['behavior'], 'path': 'Refresh matching CSVs to show its current path.'})
        self.imaging_complete.setChecked(False)
        ambiguous = [s for s in SIDES if len(d.get('detected_paths', {}).get(s, [])) > 1]
        self.imaging_messages.setText('Review mouse/date, file paths, colors and behavior. Laser power may remain blank.' +
            (' Multiple TIFFs detected for ' + ', '.join(ambiguous) + '; choose the correct file explicitly.' if ambiguous else ''))
        self.tabs.setCurrentWidget(self.imaging_page)

    def collect_imaging(self):
        if not self.imaging_draft:
            raise LoggerError('Drop or choose an imaging folder first')
        d = deepcopy(self.imaging_draft)
        r = d['record']
        r.update(schema_version=SCHEMA_VERSION, mouse_id=self.imaging_mouse.text().strip() or None,
                 session_date=self.imaging_date.text().strip() or None,
                 operator=self.config.get('operator'), notes=self.imaging_notes.toPlainText().strip() or None)
        for side in SIDES:
            d['side_paths'][side] = self.imaging_paths[side].text().strip() or None
            r['metadata']['sides'][side]['laser_power'] = self.imaging_powers[side].text().strip() or None
            for color, box in self.imaging_colors[side].items():
                state = box.checkState()
                r['metadata']['sides'][side]['channels'][color] = None if state == Qt.CheckState.PartiallyChecked else state == Qt.CheckState.Checked
        yes = self.imaging_behavior.currentIndex()
        r['metadata']['behavior_session'] = None if yes == 0 else yes == 2
        choice = self.imaging_behavior_picker.currentData() if yes == 2 else None
        ref = deepcopy(choice['reference']) if choice else None
        r['metadata']['behavior'] = ref
        r['related_records'] = [{k: ref[k] for k in ('record_id', 'sha256')}] if ref else []
        return d

    def choose_imaging_tiff(self, side):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose ' + side + ' imaging TIFF', '', 'TIFF images (*.tif *.tiff *.TIF *.TIFF)')
        if path:
            self.imaging_paths[side].setText(path)

    def imaging_behavior_changed(self):
        self.imaging_behavior_picker.clear()
        self.imaging_behavior_picker.setEnabled(self.imaging_behavior.currentIndex() == 2)
        if self.imaging_behavior.currentIndex() == 2:
            self.refresh_imaging_behavior()

    def refresh_imaging_behavior(self):
        try:
            if self.imaging_behavior.currentIndex() != 2:
                return
            mouse, date = self.imaging_mouse.text().strip(), self.imaging_date.text().strip()
            self.run_job(lambda _p, _c: self.store.behavior_candidates(mouse, date), self.show_imaging_behavior, refresh_archive=False)
        except Exception as exc:
            self.error(str(exc))

    def show_imaging_behavior(self, choices):
        previous = self.imaging_behavior_picker.currentData()
        self.imaging_behavior_picker.clear()
        self.imaging_behavior_picker.addItem('Choose a robot CSV session', None)
        for choice in choices:
            self.imaging_behavior_picker.addItem(choice['label'], choice)
        if len(choices) == 1:
            self.imaging_behavior_picker.setCurrentIndex(1)
        elif previous:
            for i in range(1, self.imaging_behavior_picker.count()):
                if self.imaging_behavior_picker.itemData(i)['reference'] == previous['reference']:
                    self.imaging_behavior_picker.setCurrentIndex(i)
        if not choices:
            self.imaging_behavior_path.setText('No matching logged CSV. Log it using the button below, then refresh this list.')
        self.status.setText(f'{len(choices)} matching behavior session(s); choose explicitly if there is more than one.')

    def imaging_behavior_selected(self):
        choice = self.imaging_behavior_picker.currentData()
        self.imaging_behavior_path.setText(choice['path'] if choice else 'Select the matching robot CSV session.')

    def log_imaging_behavior(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose robot CSV to log in Behavior', '', 'Robot CSV (*.csv)')
        if path:
            self.load_source(path)

    def save_imaging_draft(self):
        try:
            self.imaging_draft = self.store.save_draft(self.collect_imaging())
            self.imaging_messages.setText(f"Imaging draft saved · revision {self.imaging_draft['revision']}")
            self.refresh_drafts()
        except Exception as exc:
            self.error(str(exc))

    def open_imaging_draft(self):
        try:
            rid = self.imaging_draft_picker.currentData()
            if rid and self._replace_imaging():
                self.imaging_draft = self.store.load_draft(rid)
                self.imaging_mouse_picker.blockSignals(True)
                self.imaging_mouse_picker.clear()
                folder = self.imaging_draft['source_folder']
                self.imaging_mouse_picker.addItem(Path(folder).name, folder)
                self.imaging_mouse_picker.blockSignals(False)
                self.fill_imaging_form()
        except Exception as exc:
            self.error(str(exc))

    def finalize_imaging_form(self):
        try:
            d = self.collect_imaging()
            if not self.imaging_complete.isChecked():
                raise LoggerError('Confirm acquisition is finished and the TIFFs are closed')
            plan = self.store.plan_imaging(d)
            lines = [f"{i['side'].capitalize()}: {i['source']}\n  → {Path(self.store.roots['archive']) / i['relative_path']}" for i in plan['items']]
            question = ('Move and register these TIFFs?\n\n' + '\n\n'.join(lines) +
                '\n\nA direct move is attempted first. If unavailable, a verified copy is made and the original is removed only after the metadata Git commit. '
                'Already moved files remain in the archive after cancellation; Recover resumes the operation.\n\n' +
                f"Metadata: {self.store.metadata / 'records' / plan['layout']['directory_name']}")
            if QMessageBox.question(self, 'Move imaging files', question) != QMessageBox.StandardButton.Yes:
                return
            self.imaging_draft = self.store.save_draft(d)
            frozen = deepcopy(self.imaging_draft)
            self.run_job(lambda p, c: self.store.finalize_imaging(frozen, move_confirmed=True,
                         source_complete=True, progress=p, cancel=c), self.imaging_finalized)
        except Exception as exc:
            self.error(str(exc))

    def imaging_finalized(self, receipt):
        self.imaging_draft = None
        self.clear_imaging_fields()
        self.imaging_source.clear()
        methods = ', '.join(f"{f['side']}: {f['method'].replace('_', ' ')}" for f in receipt['files'])
        self.imaging_messages.setText('Finalized locally · ' + ('Git committed' if receipt['git_committed'] else 'Git pending') +
            ' · ' + ('sources moved' if receipt['source_removed'] else 'source removal pending — use Recover') + '\n' + methods +
            f"\nMetadata: {self.store.metadata / receipt['record_directory']}")
        self.status.setText(self.imaging_messages.text())
