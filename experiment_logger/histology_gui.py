"""Confocal file entry with colony identity and optional experiment amendments."""
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QScrollArea,QVBoxLayout,QHBoxLayout,QFormLayout,QLineEdit,
    QPlainTextEdit,QLabel,QComboBox,QCheckBox,QFileDialog,QMessageBox,QTableWidget,QTableWidgetItem,QHeaderView)

from .io import LoggerError
from .histology import new_histology, colony_reference
from .stim_gui import FilesDrop

_KEEP_SELECTION=object()

class HistologyTab:
    def _histology_tab(self):
        self.histology_draft=None
        outer=self._page('Histology')
        self.histology_page=outer.parentWidget()
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        content=QWidget()
        layout=QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        saved=QHBoxLayout()
        self.histology_draft_picker=QComboBox()
        saved.addWidget(self.histology_draft_picker,1)
        self._button('Open saved draft',self.open_histology_draft,saved)
        layout.addLayout(saved)
        self.histology_drop=FilesDrop('Drop CZI files or a brain-slice folder here\nMouse/slice come from the folder; acquisition date comes from CZI metadata when available')
        self.histology_drop.dropped.connect(self.load_histology_paths)
        layout.addWidget(self.histology_drop)
        browse=QHBoxLayout()
        self._button('Choose CZI files…',self.choose_histology_files,browse)
        self._button('Choose slice folder…',self.choose_histology_folder,browse)
        layout.addLayout(browse)
        self.histology_images=QTableWidget(0,6)
        self.histology_images.setHorizontalHeaderLabels(['CZI file','Z planes','Tiles','Channel presets','XY pixels','Pixel size / Z step'])
        self.histology_images.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.histology_images.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.histology_images.horizontalHeader().setStretchLastSection(True)
        self.histology_images.setMinimumHeight(130)
        layout.addWidget(self.histology_images)
        form=QFormLayout()
        self.histology_mouse,self.histology_date,self.histology_slice=QLineEdit(),QLineEdit(),QLineEdit()
        self.histology_mouse.setPlaceholderText('Three digits')
        self.histology_date.setPlaceholderText('YYYY-MM-DD')
        self.histology_slice.setPlaceholderText('Example: 3-1C')
        self.histology_region,self.histology_side=QLineEdit(),QLineEdit()
        self.histology_markers=QLineEdit()
        for label,field in [('Mouse ID',self.histology_mouse),('Acquisition date',self.histology_date),
                ('Brain slice index',self.histology_slice),('Brain region (optional)',self.histology_region),
                ('Hemisphere / side (optional)',self.histology_side),('Stains / markers (optional)',self.histology_markers)]:
            form.addRow(label,field)
        layout.addLayout(form)
        self.histology_filter=QLineEdit()
        self.histology_filter.setPlaceholderText('Filter experiments by date, protocol, data/session, or ID')
        self.histology_filter.textChanged.connect(self.filter_histology_experiments)
        self.histology_profile_status=QLabel('Mouse colony profile: drop files or enter a mouse and click Find experiments')
        self.histology_profile_status.setWordWrap(True)
        layout.addWidget(self.histology_profile_status)
        layout.addWidget(QLabel('Existing experiment (optional; adds an amendment to the selected same-mouse experiment)'))
        layout.addWidget(self.histology_filter)
        choices=QHBoxLayout()
        self.histology_target=QComboBox()
        self.histology_target.setMinimumContentsLength(30)
        self.histology_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.histology_target.currentIndexChanged.connect(lambda: self.histology_reviewed.setChecked(False))
        choices.addWidget(self.histology_target,1)
        self._button('Find experiments',self.refresh_histology_experiments,choices)
        layout.addLayout(choices)
        self.histology_export=QCheckBox('Also export a full-resolution stitched OME-TIFF stack')
        self.histology_export.setToolTip('Preserves grayscale bit depth and all Z/channel/time planes. Separate scenes become separate OME series. '
                                       'Tile overlaps use the highest tile index, without blending.')
        self.histology_export.toggled.connect(lambda: self.histology_reviewed.setChecked(False))
        layout.addWidget(self.histology_export)
        self.histology_notes=QPlainTextEdit()
        self.histology_notes.setPlaceholderText('Optional histology observations / comments')
        self.histology_notes.setFixedHeight(75)
        layout.addWidget(self.histology_notes)
        self.histology_reviewed=QCheckBox('I reviewed the mouse, slice, image metadata, and any selected experiment link')
        self.histology_complete=QCheckBox('Acquisition is finished and all selected CZI files are closed')
        layout.addWidget(self.histology_reviewed)
        layout.addWidget(self.histology_complete)
        for field in (self.histology_mouse,self.histology_date,self.histology_slice):
            field.textEdited.connect(lambda: self.histology_reviewed.setChecked(False))
        self.histology_mouse.editingFinished.connect(self.refresh_histology_experiments)
        help_=QLabel('Destination: <archive>/histology/mouse_<ID>/YYMMDD/<ID>-<slice>/<number>/czi/ and optional tif/.\n'
            'Log using a saved mouse colony profile. Selecting an experiment also creates a linking amendment. '
            'Channel names are microscope presets; describe actual stains/markers above.')
        help_.setWordWrap(True)
        layout.addWidget(help_)
        actions=QHBoxLayout()
        self._button('Save draft',self.save_histology_draft,actions)
        self._button('Review / move files…',self.finalize_histology_form,actions).setObjectName('primary')
        self._button('Resume pending transfers',self.recover,actions)
        outer.addLayout(actions)
        self.histology_messages=QLabel('Drop a CZI file or slice folder to begin. No files move on drop.')
        self.histology_messages.setWordWrap(True)
        outer.addWidget(self.histology_messages)

    def choose_histology_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'Choose completed confocal images','','CZI files (*.czi *.CZI)')
        if paths:self.load_histology_paths(paths)

    def choose_histology_folder(self):
        folder=QFileDialog.getExistingDirectory(self,'Choose a brain-slice folder')
        if folder:self.load_histology_paths([folder])

    def load_histology_paths(self,paths):
        if self.histology_draft and QMessageBox.question(self,'Replace histology form',
                'Replace this form? Save a draft first to retain edits.')!=QMessageBox.StandardButton.Yes:
            return
        self.tabs.setCurrentWidget(self.histology_page)
        def work(progress,cancel):
            progress.phase('Reading CZI metadata and tile layout')
            d=new_histology(paths,cancel)
            d['record']['operator']=self.config.get('operator')
            d['experiment_choices']=(self.store.histology_candidates(d['record']['mouse_id'])
                                      if self.store and d['record']['mouse_id'] else [])
            if self.store and d['record']['mouse_id']:
                d['record']['metadata']['colony_profile']=colony_reference(self.store,d['record']['mouse_id'])
            return d
        self.run_job(work,self.histology_loaded,refresh_archive=False)

    def histology_loaded(self,draft):
        self.histology_draft=draft
        self.fill_histology_form()
        self.histology_messages.setText(f"{len(draft['inputs'])} CZI file(s) selected. Review the mouse/slice/date; linking an experiment is optional.")

    def collect_histology(self):
        if self.histology_draft is None:
            raise LoggerError('Drop a CZI file or slice folder first')
        d=deepcopy(self.histology_draft)
        r,m=d['record'],d['record']['metadata']
        r.update(mouse_id=self.histology_mouse.text().strip() or None,
                 session_date=self.histology_date.text().strip() or None,
                 operator=self.config.get('operator'),notes=self.histology_notes.toPlainText().strip() or None)
        ref=self.histology_target.currentData()
        m.update(slice_index=self.histology_slice.text().strip() or None,brain_region=self.histology_region.text().strip() or None,
                 hemisphere=self.histology_side.text().strip() or None,marker_notes=self.histology_markers.text().strip() or None,
                 linked_record=ref,export_tiff=self.histology_export.isChecked(),reviewed=self.histology_reviewed.isChecked())
        r['related_records']=[ref] if ref else []
        return d

    def fill_histology_form(self):
        if self.histology_draft is None:return
        r=self.histology_draft['record'];m=r['metadata']
        for field,value in [(self.histology_mouse,r['mouse_id']),(self.histology_date,r['session_date']),
                (self.histology_slice,m['slice_index']),(self.histology_region,m['brain_region']),
                (self.histology_side,m['hemisphere']),(self.histology_markers,m['marker_notes'])]:
            field.setText(value or '')
        self.histology_export.setChecked(m['export_tiff'])
        self.histology_notes.setPlainText(r['notes'] or '')
        profile=m.get('colony_profile')
        self.histology_profile_status.setText(
            f"Mouse colony profile: mouse {profile['mouse_id']} · revision {profile['revision']} (snapshot will be saved)"
            if profile else 'No saved colony profile found. Save one in Mouse colony and click Find experiments, or select an experiment below.')
        self.histology_images.setRowCount(len(m['images']))
        for row,image in enumerate(m['images']):
            i=image['czi'];b=i['bounds'];scale=i['pixel_size_um']
            source=next((x['source'] for x in self.histology_draft['inputs'] if x['file_id']==image['file_id']),image['original_filename'])
            tooltip=(f"Source: {source}\nAcquired: {i['acquisition_time_local'] or 'Unknown; review folder date'}\n"
                     f"Objective: {(i['objective'] or {}).get('name') or 'Unknown'}\n"
                     f"Pixel types: {', '.join(sorted(set(i['pixel_types'].values())))}")
            zstep=f"{scale['z']:.4g} µm" if 'z' in scale else '?'
            xy=f"{scale['x']:.4g} µm" if 'x' in scale else '?'
            values=[image['original_filename'],b['Z'][1]-b['Z'][0],i['tile_count'] or 'Unknown',
                    ', '.join(c['name'] for c in i['channels']),
                    ' / '.join(f"{s['width']} × {s['height']}" for s in i['scenes']),f'XY {xy}; Z {zstep}']
            for col,value in enumerate(values):
                cell=QTableWidgetItem(str(value))
                cell.setToolTip(tooltip)
                self.histology_images.setItem(row,col,cell)
        self.filter_histology_experiments(preferred=m['linked_record'])
        self.histology_reviewed.setChecked(m['reviewed'])
        self.histology_complete.setChecked(False)

    def filter_histology_experiments(self,_text=None,*,preferred=_KEEP_SELECTION):
        if self.histology_draft is None:return
        ref=self.histology_target.currentData() if preferred is _KEEP_SELECTION else preferred
        self.histology_target.blockSignals(True)
        self.histology_target.clear()
        self.histology_target.addItem('No experiment link — log for this mouse',None)
        for choice in self.histology_draft.get('experiment_choices',[]):
            if self.histology_filter.text().lower() not in choice['label'].lower():continue
            self.histology_target.addItem(choice['label'],choice['reference'])
            if choice['reference']==ref:self.histology_target.setCurrentIndex(self.histology_target.count()-1)
        self.histology_target.blockSignals(False)
        self.histology_reviewed.setChecked(False)

    def refresh_histology_experiments(self):
        if self.histology_draft is None:return
        try:
            if not self.store:raise LoggerError('Configure storage in Settings first')
            d=self.collect_histology()
            def work(progress,cancel):
                progress.phase('Finding experiments for this mouse')
                d['experiment_choices']=self.store.histology_candidates(d['record']['mouse_id'])
                d['record']['metadata']['colony_profile']=colony_reference(self.store,d['record']['mouse_id'])
                if d['record']['metadata']['linked_record'] not in [c['reference'] for c in d['experiment_choices']]:
                    d['record']['metadata']['linked_record']=None
                    d['record']['related_records']=[]
                d['record']['metadata']['reviewed']=False
                return d
            self.run_job(work,self.histology_loaded,refresh_archive=False)
        except Exception as exc:self.error(str(exc))

    def save_histology_draft(self):
        try:
            if not self.store:raise LoggerError('Configure storage in Settings first')
            self.histology_draft=self.store.save_draft(self.collect_histology())
            self.refresh_drafts()
            self.histology_messages.setText(f"Histology draft saved · revision {self.histology_draft['revision']}")
        except Exception as exc:self.error(str(exc))

    def open_histology_draft(self):
        try:
            rid=self.histology_draft_picker.currentData()
            if not rid:return
            if self.histology_draft and QMessageBox.question(self,'Open histology draft',
                    'Replace this form? Save a draft first to retain edits.')!=QMessageBox.StandardButton.Yes:return
            self.histology_loaded(self.store.load_draft(rid))
        except Exception as exc:self.error(str(exc))

    def finalize_histology_form(self):
        try:
            if not self.store:raise LoggerError('Configure storage in Settings first')
            d=self.collect_histology()
            if not self.histology_complete.isChecked():raise LoggerError('Confirm acquisition is finished and the files are closed')
            plan=self.store.plan_histology(d)
            target=d['record']['metadata']['linked_record']
            lines=[f"{i['source']}\n  → {Path(self.store.roots['archive'])/i['relative_path']}" for i in plan['items']]
            lines += [f"TIFF export → {Path(self.store.roots['archive'])/e['relative_path']}" for e in plan['exports']]
            profile=plan['record']['metadata'].get('colony_profile')
            link_text=(f"An amendment will be added to experiment {target['record_id']}." if target else
                       f"Register for mouse {profile['mouse_id']} using colony profile revision {profile['revision']}. No experiment amendment will be created.")
            question=('Move and register these confocal files?\n\n'+'\n\n'.join(lines)+
                '\n\n'+link_text+'\n'+
                'Direct moves are attempted first; otherwise verified copies are made and originals are removed after the metadata Git commit. '
                'Already moved files stay in the archive if interrupted; Recover resumes registration/export.')
            if QMessageBox.question(self,'Move and register histology',question)!=QMessageBox.StandardButton.Yes:return
            self.histology_draft=self.store.save_draft(d)
            frozen=deepcopy(self.histology_draft)
            self.run_job(lambda p,c:self.store.finalize_histology(frozen,move_confirmed=True,source_complete=True,progress=p,cancel=c),self.histology_finalized)
        except Exception as exc:self.error(str(exc))

    def histology_finalized(self,receipt):
        self.histology_draft=None
        self.histology_images.setRowCount(0)
        self.histology_complete.setChecked(False)
        self.histology_reviewed.setChecked(False)
        self.histology_messages.setText(('Histology and amendment finalized locally · ' if receipt.get('amendment_id') else
                                       'Histology finalized locally · ')+
            ('Git committed' if receipt['git_committed'] else 'Git pending — use Recover')+
            (f"\nAmendment: {receipt['amendment_id']}" if receipt.get('amendment_id') else '')+
            f"\nMetadata: {self.store.metadata/receipt['record_directory']}")
        self.status.setText(self.histology_messages.text())
