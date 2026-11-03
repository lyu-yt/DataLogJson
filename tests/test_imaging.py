"""Synthetic imaging/stimulation only: no real movies or NAS acquisitions."""
from copy import deepcopy
from pathlib import Path
import errno
import json

import pytest

from experiment_logger import imaging
from experiment_logger import transfers as transfer_engine
from experiment_logger.io import LoggerError, Cancelled, atomic_json, digest, read_json, uid
from experiment_logger.models import validate
from test_expression import write_tiff


@pytest.fixture
def imaging_folder(tmp_path):
    folder = tmp_path / 'movies/261015/007'
    write_tiff(folder / 'left_00001.tif', 17)
    write_tiff(folder / 'right_00002.tif', 23)
    return folder


@pytest.fixture
def imaging_draft(store, imaging_folder):
    d = store.new_imaging(imaging_folder)
    d['record']['operator'] = 'SYNTHETIC imaging tester'
    d['record']['metadata']['behavior_session'] = False
    for side in imaging.SIDES:
        d['record']['metadata']['sides'][side]['channels'] = {'red': False, 'green': True}
    d['record']['metadata']['sides']['left']['laser_power'] = '920 nm: 25 mW; adjusted manually'
    return store.save_draft(d)


def force_copy(monkeypatch, folder):
    original = transfer_engine.rename_path
    def cross_device(source, target):
        if Path(source).is_relative_to(folder):
            raise OSError(errno.EXDEV, 'Synthetic cross-device move')
        return original(source, target)
    monkeypatch.setattr(transfer_engine, 'rename_path', cross_device)


def finalize(store, d, **kwargs):
    return store.finalize_imaging(d, source_complete=True, move_confirmed=True, **kwargs)


def test_imaging_detection_unknown_fields_and_ambiguous_files(store, imaging_folder):
    d = store.new_imaging(imaging_folder)
    assert d['record']['mouse_id'] == '007' and d['record']['session_date'] == '2026-10-15'
    assert d['record']['metadata']['behavior_session'] is None
    assert all(s['laser_power'] is None and all(c is None for c in s['channels'].values())
               for s in d['record']['metadata']['sides'].values())
    assert not validate(d['record'])
    write_tiff(imaging_folder / 'left_second.tif')
    assert store.new_imaging(imaging_folder)['side_paths']['left'] is None


def test_direct_moves_hash_once_and_publish_readable_records(store, imaging_folder, imaging_draft, monkeypatch):
    original = {p.name: p.read_bytes() for p in imaging_folder.iterdir()}
    hashes, ticks = [], []
    real_hash = transfer_engine.hash_file
    def count(path, *args, **kwargs):
        hashes.append(Path(path))
        return real_hash(path, *args, **kwargs)
    monkeypatch.setattr(transfer_engine, 'hash_file', count)
    result = finalize(store, imaging_draft, progress=lambda n,t: ticks.append(n/t))
    assert result['git_committed'] and result['source_removed'] and not result['backed_up']
    assert len(hashes) == 2 and all(i['method'] == 'direct_move' for i in result['files'])
    assert ticks == sorted(ticks) and ticks[-1] == 1
    assert not list(imaging_folder.iterdir()) and not store.pending_imaging()
    folder = store.metadata / result['record_directory']
    assert result['record_directory'] == 'records/mouse_007/261015/in_vivo_imaging_1'
    assert {p.name for p in folder.iterdir()} == {'imaging.json', 'session.json', 'manifest.json'}
    r = store.read_record(imaging_draft['record']['record_id'])
    assert not validate(r, True) and len(r['files']) == 2
    assert r['metadata']['sides']['left']['laser_power'] == '920 nm: 25 mW; adjusted manually'
    assert r['metadata']['sides']['right']['laser_power'] is None
    for f in r['files']:
        loc = store.file_locations(f['file_id'])[0]
        assert loc['relative_path'] == f"imaging/mouse_007/261015/{f['role']}/{f['original_filename']}"
        assert Path(loc['path']).read_bytes() == original[f['original_filename']]
        assert f['sha256'] == digest(original[f['original_filename']])
    assert all(i['data_type'] == 'imaging' for i in store.index().values())
    assert not any(p.endswith('.tif') for p in store.git('ls-files').splitlines())
    with pytest.raises(LoggerError, match='Duplicate'):
        finalize(store, imaging_draft)


def test_cross_device_verified_copy_then_remove(store, imaging_folder, imaging_draft, monkeypatch):
    force_copy(monkeypatch, imaging_folder)
    result = finalize(store, imaging_draft)
    assert result['git_committed'] and result['source_removed']
    assert all(f['method'] == 'verified_copy' for f in result['files'])
    assert not list(imaging_folder.iterdir())


def test_failed_git_retains_copied_sources_until_recovery(store, imaging_folder, imaging_draft, monkeypatch):
    force_copy(monkeypatch, imaging_folder)
    git = store.git
    def fail(*args):
        if 'commit' in args:
            raise LoggerError('Synthetic Git failure')
        return git(*args)
    monkeypatch.setattr(store, 'git', fail)
    receipt = finalize(store, imaging_draft)
    assert receipt['finalized_locally'] and not receipt['git_committed'] and not receipt['source_removed']
    assert len(list(imaging_folder.iterdir())) == 2 and store.pending_imaging()
    monkeypatch.setattr(store, 'git', git)
    store.recover()
    assert not store.pending_imaging() and not store.pending() and not list(imaging_folder.iterdir())
    assert len(store.index()) == 2


@pytest.mark.parametrize('stage', ['renamed', 'transferred', 'prepared', 'published'])
def test_interrupted_moves_and_publications_recover_without_duplicates(store, imaging_folder, imaging_draft, stage):
    with pytest.raises(LoggerError, match='Injected interruption'):
        finalize(store, imaging_draft, failpoint=stage)
    assert store.pending_imaging()
    store.recover()
    assert len(store.index()) == 2 and all(i['integrity'] == 'ok' for i in store.index().values())
    assert not store.pending_imaging() and not list(imaging_folder.iterdir())
    assert store.recover() == []


def test_cancelled_direct_move_can_resume_from_archived_file(store, imaging_folder, imaging_draft):
    left = imaging_folder / 'left_00001.tif'
    with pytest.raises(Cancelled):
        finalize(store, imaging_draft, cancel=lambda: not left.exists())
    assert not left.exists() and not store.index()
    store.recover()
    assert len(store.index()) == 2 and not store.pending_imaging()


def test_cancelled_copy_removes_only_partial_and_retains_originals(store, imaging_folder, imaging_draft, monkeypatch):
    force_copy(monkeypatch, imaging_folder)
    cancelled = [False]
    with pytest.raises(Cancelled):
        finalize(store, imaging_draft, progress=lambda n,t: cancelled.__setitem__(0, True), cancel=lambda: cancelled[0])
    assert len(list(imaging_folder.iterdir())) == 2
    assert not list(Path(store.roots['archive']).rglob('*.partial'))
    store.recover()
    assert not store.pending_imaging() and len(store.index()) == 2


def test_cleanup_interruption_after_unlink_is_recoverable(store, imaging_folder, imaging_draft, monkeypatch):
    force_copy(monkeypatch, imaging_folder)
    r = finalize(store, imaging_draft, failpoint='source_removed')
    assert r['cleanup_pending'] and not r['source_removed'] and r['git_committed']
    store.recover()
    assert not store.pending_imaging() and not list(imaging_folder.iterdir())


def test_changed_source_is_not_removed_during_recovery(store, imaging_folder, imaging_draft, monkeypatch):
    force_copy(monkeypatch, imaging_folder)
    git = store.git
    def fail(*args):
        if 'commit' in args:
            raise LoggerError('Synthetic Git failure')
        return git(*args)
    monkeypatch.setattr(store, 'git', fail)
    finalize(store, imaging_draft)
    original = imaging_folder / 'left_00001.tif'
    write_tiff(original, 199)
    monkeypatch.setattr(store, 'git', git)
    results = store.recover()
    assert original.exists() and store.pending_imaging()
    assert any(r.get('cleanup_pending') for r in results)


def test_permission_failure_does_not_fall_back_to_copy(store, imaging_folder, imaging_draft, monkeypatch):
    def deny(*args):
        raise PermissionError('Synthetic permission failure')
    monkeypatch.setattr(transfer_engine, 'rename_path', deny)
    with pytest.raises(PermissionError):
        finalize(store, imaging_draft)
    assert len(list(imaging_folder.iterdir())) == 2 and not store.index()


def test_collision_keeps_existing_file_and_assigns_suffix(store, imaging_draft):
    target = Path(store.roots['archive']) / 'imaging/mouse_007/261015/left/left_00001.tif'
    write_tiff(target, 201)
    before = target.read_bytes()
    result = finalize(store, imaging_draft)
    assert target.read_bytes() == before
    assert any(f['relative_path'].endswith('left_00001__2.tif') for f in result['files'])


@pytest.mark.parametrize('problem', ['colors', 'behavior', 'duplicate', 'relative', 'closed', 'move', 'changed', 'stale'])
def test_invalid_imaging_finalization_never_moves_sources(store, imaging_folder, imaging_draft, problem):
    d = deepcopy(imaging_draft)
    if problem == 'colors': d['record']['metadata']['sides']['left']['channels']['red'] = None
    if problem == 'behavior': d['record']['metadata']['behavior_session'] = True
    if problem == 'duplicate': d['side_paths']['right'] = d['side_paths']['left']
    if problem == 'relative': d['side_paths']['left'] = 'left.tif'
    if problem == 'changed':
        store.plan_imaging(d)
        write_tiff(imaging_folder/'left_00001.tif', 188)
    if problem == 'stale': store.save_draft(imaging_draft)
    with pytest.raises((LoggerError, FileNotFoundError)):
        store.finalize_imaging(d, source_complete=problem != 'closed', move_confirmed=problem != 'move')
    assert len(list(imaging_folder.iterdir())) == 2 and not store.index()


def test_one_side_only_and_blank_power_are_supported(store, imaging_folder, imaging_draft):
    d = deepcopy(imaging_draft)
    d['side_paths']['right'] = None
    d['record']['metadata']['sides']['right']['channels'] = {'red': None, 'green': None}
    d['record']['metadata']['sides']['left']['laser_power'] = None
    result = finalize(store, d)
    assert len(result['files']) == 1 and (imaging_folder/'right_00002.tif').exists()


def link_behavior(d, candidate):
    ref = deepcopy(candidate['reference'])
    d['record']['metadata'].update(behavior_session=True, behavior=ref)
    d['record']['related_records'] = [{k: ref[k] for k in ('record_id', 'sha256')}]


def test_behavior_link_pins_task_record_session_and_csv_without_editing_task(store, draft, imaging_draft):
    receipt = store.finalize(draft, source_complete=True)
    old = {p: p.read_bytes() for p in (store.metadata/receipt['record_directory']).glob('*.json')}
    choices = store.behavior_candidates('007', '2026-10-15')
    assert len(choices) == 1
    d = deepcopy(imaging_draft)
    link_behavior(d, choices[0])
    result = finalize(store, d)
    r = store.read_record(d['record']['record_id'])
    assert r['session_id'] != r['metadata']['behavior']['session_id']
    assert r['metadata']['behavior']['csv_sha256'] == store.read_record(draft['record']['record_id'])['files'][0]['sha256']
    manifest = read_json(store.metadata/result['record_directory']/'manifest.json')
    seal = next(s for s in manifest['records'] if s['record_id'] == r['record_id'])
    assert r['related_records'][0] in seal['references']
    assert all(p.read_bytes() == raw for p,raw in old.items())


def test_changed_behavior_reference_blocks_transfer(store, draft, imaging_draft, imaging_folder):
    store.finalize(draft, source_complete=True)
    d = deepcopy(imaging_draft)
    link_behavior(d, store.behavior_candidates('007', '2026-10-15')[0])
    d['record']['metadata']['behavior']['csv_sha256'] = '0'*64
    with pytest.raises(LoggerError, match='CSV identity'):
        finalize(store, d)
    assert len(list(imaging_folder.iterdir())) == 2


def test_behavior_ambiguity_is_left_for_the_user(store, draft, imaging_folder, tmp_path):
    from PySide6.QtWidgets import QApplication
    from experiment_logger.gui import Window
    from test_gui import wait_job
    store.finalize(draft, source_complete=True)
    another = deepcopy(draft)
    another['revision'] = 0
    another['record']['record_id'] = uid()
    another['record']['session_id'] = uid()
    store.finalize(another, source_complete=True)
    assert len(store.behavior_candidates('007','2026-10-15')) == 2
    assert store.behavior_candidates('008','2026-10-15') == []
    app = QApplication.instance() or QApplication([])
    config = tmp_path/'ambiguity/config.json'
    atomic_json(config, {'roots':store.roots,'operator':'Synthetic'})
    w = Window(config)
    try:
        w.load_imaging_folder(imaging_folder)
        w.imaging_behavior.setCurrentIndex(2)
        wait_job(app,w)
        assert w.imaging_behavior_picker.count() == 3
        assert w.imaging_behavior_picker.currentData() is None
        w.save_imaging_draft()  # An unresolved link may still be an editable draft.
        assert w.imaging_draft['record']['metadata']['behavior'] is None
    finally:
        w.close()


def test_transfer_choices_cannot_change_after_move_started(store, imaging_folder, imaging_draft):
    with pytest.raises(LoggerError,match='Injected interruption'):
        finalize(store,imaging_draft,failpoint='renamed')
    edited = deepcopy(imaging_draft)
    edited['record']['metadata']['sides']['left']['laser_power'] = 'changed'
    with pytest.raises(LoggerError,match='choices changed'):
        finalize(store,edited)
    store.recover()
    r = store.read_record(imaging_draft['record']['record_id'])
    assert r['metadata']['sides']['left']['laser_power'] == '920 nm: 25 mW; adjusted manually'


def test_archived_target_tampering_blocks_recovery(store, imaging_draft):
    with pytest.raises(LoggerError,match='Injected interruption'):
        finalize(store,imaging_draft,failpoint='transferred')
    plan = store.pending_imaging()[0]
    target = Path(store.roots['archive'])/plan['items'][0]['relative_path']
    write_tiff(target,200)
    with pytest.raises(LoggerError,match='checksum mismatch'):
        store.recover()
    assert not store.index()


def test_imaging_cli_requires_explicit_move_and_keeps_draft_unknowns(store, imaging_folder, tmp_path, capsys):
    from experiment_logger.cli import main
    config = store.local/'config.json'
    atomic_json(config,{'roots':store.roots,'operator':'Synthetic CLI'})
    args = ['--config',str(config)]
    assert main([*args,'imaging-draft',str(imaging_folder)]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d['record']['metadata']['behavior_session'] is None
    d['record']['metadata']['behavior_session'] = False
    for s in imaging.SIDES:
        d['record']['metadata']['sides'][s]['channels'] = {'red':False,'green':True}
    store.save_draft(d)
    with pytest.raises(SystemExit):
        main([*args,'finalize',d['record']['record_id'],'--source-complete'])
    assert main([*args,'finalize',d['record']['record_id'],'--source-complete','--move']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['source_removed'] and result['git_committed']


def test_imaging_ui_drop_link_save_finalize_and_open_behavior(store, draft, imaging_folder, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt, QMimeData, QUrl, QPointF
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    store.finalize(draft, source_complete=True)
    app = QApplication.instance() or QApplication([])
    config = tmp_path/'ui/config.json'
    atomic_json(config, {'roots':store.roots,'operator':'Synthetic UI'})
    w = Window(config)
    errors, confirmations = [], []
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:errors.append(a[-1]))
    def yes(*args):
        confirmations.append(args[-1])
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox,'question',yes)
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(imaging_folder))])
        event = QDropEvent(QPointF(10,10),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        w.imaging_drop.dropEvent(event)
        assert w.tabs.currentWidget() == w.imaging_page
        assert w.imaging_mouse.text() == '007' and w.imaging_date.text() == '2026-10-15'
        assert all(not e.text() for e in w.imaging_powers.values())
        for side in imaging.SIDES:
            w.imaging_colors[side]['red'].setCheckState(Qt.CheckState.Unchecked)
            w.imaging_colors[side]['green'].setCheckState(Qt.CheckState.Checked)
        w.imaging_powers['left'].setText('25 mW, manual note')
        w.imaging_behavior.setCurrentIndex(2)
        wait_job(app,w)
        assert w.imaging_behavior_picker.currentIndex() == 1
        w.save_imaging_draft()
        assert w.imaging_draft_picker.count() == 1
        w.open_imaging_draft()
        assert w.imaging_powers['left'].text() == '25 mW, manual note'
        w.imaging_complete.setChecked(True)
        rid = w.imaging_draft['record']['record_id']
        w.finalize_imaging_form()
        wait_job(app,w)
        assert not errors and 'direct move' in w.imaging_messages.text()
        assert any('original is removed only after' in q for q in confirmations)
        w.filter_data_type.setCurrentText('imaging')
        assert w.records.rowCount() == 2
        w.filter_kind.setCurrentText('data')
        w.records.selectRow(0)
        app.processEvents()
        assert json.loads(w.raw.toPlainText())['record']['record_id'] == rid
        assert w.expression_files.rowCount() == 2 and w.linked_behavior_button.isEnabled()
        w.open_linked_behavior()
        app.processEvents()
        assert json.loads(w.raw.toPlainText())['record']['record_id'] == draft['record']['record_id']
        assert w.csv_table.columnCount() == 13
    finally:
        w.close()


def write_stim_mat(path, mouse='007'):
    """Small SYNTHETIC MATLAB-v7.3-shaped HDF5 with a huge unread mask."""
    import h5py
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, 'w', userblock_size=512) as f:
        def value(name, v, matlab_class='double'):
            if isinstance(v, str):
                raw = np.frombuffer(v.encode('utf-16-le'), dtype='<u2').reshape(-1, 1)
                matlab_class = 'char'
            else:
                raw = np.atleast_2d(v).T
            d = f.create_dataset('session/'+name, data=raw)
            d.attrs['MATLAB_class'] = matlab_class.encode()
        for key, v in {'nParamSets':1, 'power':70, 'spiralSize':[3,3], 'durationStim':.005,
                       'durationPause':.035, 'pulsen':10, 'continuousSpiral':0, 'spiraln':5}.items():
            value('stimPulseParams/'+key, v)
        for key,v in {'nPatterns':4, 'nStimLoc':20, 'nStimLocIsArray':False}.items():
            value('stimPatternParams/'+key,v)
        for key,v in {'nSeqRepeat':40,'nPadInit':1,'StimSeq':[1,2,2,2,2],
                       'StimSeqRaspPi':[1,2,3,4,5]*40}.items():
            value('stimSequenceParams/'+key,v)
        value('experimentParams/mouseNumber',mouse)
        value('experimentParams/stimLaserPowerMw','800 mW')
        value('experimentParams/comments','SYNTHETIC')
        value('paths/saveDir','unused/261015/999')
        value('outputs/parametersCsvPath','unused/261015/999/stim_parameters.csv')
        value('version','SYNTHETIC pipeline')
        value('data/nNeuronCorrected',97)
        value('data/stimSeqSet',[1,2,3,4,5]*40)
        f.create_dataset('session/data/aoMasks',shape=(500000,500000),chunks=(1,1),dtype='f4')
    return path


@pytest.fixture
def stim_folder(tmp_path):
    folder = tmp_path/'stim-source/261015/007'
    write_tiff(folder/'imaging/green_001.tif')
    write_tiff(folder/'imaging/green_002.tif')
    (folder/'imaging/data_2.dat').write_bytes(b'SYNTHETIC DAT\x00\x01')
    write_stim_mat(folder/'stimPara004.mat')
    (folder/'stim_parameters.csv').write_text('stim laser power (mW),mouse #,comments\n800 mW,007,SYNTHETIC\n')
    (folder/'checkpoint.mat').write_bytes(b'SYNTHETIC ancillary MAT; never parsed')
    return folder


def stim_draft_for(store, folder):
    from experiment_logger.stim import add_inputs
    d = add_inputs(store.new_stim(), [folder/'imaging'], 'imaging')
    d = add_inputs(d, [folder/'stimPara004.mat',folder/'stim_parameters.csv',folder/'checkpoint.mat'], 'stimpara')
    d['record']['operator'] = 'SYNTHETIC stim tester'
    d['record']['metadata']['reviewed'] = True
    return store.save_draft(d)


def test_stim_extraction_units_identity_and_bounded_reads(stim_folder, monkeypatch):
    import h5py
    from experiment_logger.stim_parameters import extract_stim_parameters
    original = h5py.Dataset.__getitem__
    reads = []
    def bounded(self, key):
        assert 'aoMasks' not in self.name, 'Large masks must never be read'
        reads.append(self.name)
        return original(self,key)
    monkeypatch.setattr(h5py.Dataset,'__getitem__',bounded)
    result = extract_stim_parameters(stim_folder/'stim_parameters.csv',stim_folder/'stimPara004.mat')
    p = {v['name']:v for v in result['report']['parameters']}
    assert p['laser_power_mw']['value']==800 and p['laser_power_mw']['unit']=='mW'
    assert p['instrument_power_percent']['value']==70 and p['instrument_power_percent']['unit']=='percent'
    assert p['pulse_duration_s']['value']==.005 and p['pause_duration_s']['value']==.035
    assert p['spiral_size']['value']==[3,3] and p['spiral_size']['unit']=='scanimage_unit'
    assert len(p['generated_sequence']['value'])==200
    assert result['report']['identity_candidates'][0]['mouse_id']=='007'
    assert any('999' in w for w in result['report']['warnings']) and reads


def test_stim_routes_multiple_tiffs_dat_parameters_and_other(store, stim_folder):
    d = stim_draft_for(store,stim_folder)
    assert not validate(d['record'])
    assert d['record']['mouse_id']=='007' and d['record']['session_date']=='2026-10-15'
    before = {i['file_id']:Path(i['source']).read_bytes() for i in d['inputs']}
    result = store.finalize(d,move=True,source_complete=True)
    assert result['git_committed'] and result['source_removed']
    assert {f['method'] for f in result['files']}=={'direct_move'}
    assert not store.pending_stim()
    r = store.read_record(d['record']['record_id'])
    assert not validate(r,True) and len(r['files'])==6
    assert {p.name for p in (store.metadata/result['record_directory']).iterdir()}=={'stim.json','session.json','manifest.json'}
    assert result['record_directory']=='records/mouse_007/261015/stimulation_1'
    for f in r['files']:
        location = store.file_locations(f['file_id'])[0]
        bucket = 'stimPara' if f['role'].startswith('parameters_') else 'other' if f['role']=='other' else 'imaging'
        assert location['relative_path']==f"stim/mouse_007/261015/1/{bucket}/{f['original_filename']}"
        assert Path(location['path']).read_bytes()==before[f['file_id']]
        assert f['sha256']==digest(before[f['file_id']])
    assert {i['data_type'] for i in store.index().values()}=={'stim'}
    assert not any(p.endswith(('.tif','.dat','.mat','.csv')) for p in store.git('ls-files').splitlines())


@pytest.mark.parametrize('blank', [False,True])
def test_stim_identity_conflicts_or_folder_fallback(store,stim_folder,blank):
    from experiment_logger.stim import add_inputs
    mouse = '' if blank else '008'
    write_stim_mat(stim_folder/'stimPara004.mat',mouse=mouse)
    csv = stim_folder/'stim_parameters.csv'
    csv.write_text('stim laser power (mW),mouse #,comments\n800 mW,'+('' if blank else '007')+',\n')
    d=add_inputs(store.new_stim(),[csv,stim_folder/'stimPara004.mat'],'stimpara')
    assert d['record']['mouse_id']==('007' if blank else None)
    if not blank:
        assert any('differs' in w for w in d['record']['metadata']['extraction']['warnings'])


def test_stim_parameter_ambiguity_requires_choice_and_changed_file_blocks(store,stim_folder):
    from experiment_logger.stim import add_inputs, refresh_parameters
    d=stim_draft_for(store,stim_folder)
    another=write_stim_mat(stim_folder/'stimPara005.mat')
    # A retained explicit choice is stable; a fresh ambiguous drop has no choice.
    fresh=add_inputs(store.new_stim(),[stim_folder/'stimPara004.mat',another],'stimpara')
    assert fresh['parameter_choices']['parameters_mat'] is None
    before=Path(d['inputs'][0]['source'])
    (stim_folder/'stim_parameters.csv').write_text('stim laser power (mW),mouse #,comments\n900 mW,007,changed\n')
    with pytest.raises(LoggerError,match='changed since extraction'):
        store.finalize(d,move=True,source_complete=True)
    assert before.exists() and not store.index()


@pytest.mark.parametrize('stage',['renamed','prepared','published'])
def test_stim_interrupted_transfer_recovers_without_duplicate_records(store,stim_folder,stage):
    d=stim_draft_for(store,stim_folder)
    with pytest.raises(LoggerError,match='Injected'):
        store.finalize(d,move=True,source_complete=True,failpoint=stage)
    assert store.pending_stim()
    store.recover()
    assert not store.pending_stim() and len(store.index())==2
    assert not any(Path(i['source']).exists() for i in d['inputs'])
    assert store.recover()==[]


def test_stim_copy_fallback_waits_for_git_and_respects_cancellation(store,stim_folder,monkeypatch):
    d=stim_draft_for(store,stim_folder)
    force_copy(monkeypatch,stim_folder)
    with pytest.raises(Cancelled):
        store.finalize(d,move=True,source_complete=True,cancel=lambda:True)
    assert all(Path(i['source']).exists() for i in d['inputs'])
    git=store.git
    def broken(*args):
        if 'commit' in args: raise LoggerError('SYNTHETIC Git failure')
        return git(*args)
    monkeypatch.setattr(store,'git',broken)
    receipt=store.finalize(d,move=True,source_complete=True)
    assert not receipt['git_committed'] and not receipt['source_removed']
    assert all(Path(i['source']).exists() for i in d['inputs'])
    monkeypatch.setattr(store,'git',git)
    store.recover()
    assert not store.pending_stim() and not any(Path(i['source']).exists() for i in d['inputs'])


def test_stim_validation_overrides_and_existing_numbered_directory(store,stim_folder):
    from experiment_logger.stim import add_inputs
    d=stim_draft_for(store,stim_folder)
    with pytest.raises(LoggerError,match='authorize'):
        store.finalize(d,source_complete=True)
    d['record']['metadata']['parameter_overrides']=[{'name':'instrument_power_percent','value':101}]
    with pytest.raises(LoggerError,match='invalid numeric'):
        store.plan_stim(d)
    d['record']['metadata']['parameter_overrides']=[{'name':'instrument_power_percent','value':65}]
    existing=Path(store.roots['archive'])/'stim/mouse_007/261015/1'
    existing.mkdir(parents=True)
    (existing/'keep.txt').write_bytes(b'keep')
    plan=store.plan_stim(d)
    assert plan['layout']['number']==2 and (existing/'keep.txt').read_bytes()==b'keep'
    receipt=store.finalize(d,move=True,source_complete=True)
    r=store.read_record(d['record']['record_id'])
    assert r['metadata']['parameter_overrides'][0]['value']==65
    assert next(p['value'] for p in r['metadata']['extraction']['parameters'] if p['name']=='instrument_power_percent')==70


def test_stim_ui_two_drop_areas_draft_move_and_browse(store,stim_folder,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication,QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    app=QApplication.instance() or QApplication([])
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    w=Window(config)
    errors=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:errors.append(args[-1]))
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    try:
        assert set(w.stim_drops)=={'imaging','stimpara'}
        w.load_stim_files([str(stim_folder/'imaging')],'imaging')
        wait_job(app,w)
        w.load_stim_files([str(stim_folder/n) for n in ('stimPara004.mat','stim_parameters.csv','checkpoint.mat')],'stimpara')
        wait_job(app,w)
        assert not errors and w.stim_files.rowCount()==6 and w.stim_mouse.text()=='007'
        assert '999' in w.stim_warnings.text()
        w.save_stim_draft()
        rid=w.stim_draft['record']['record_id']
        w.stim_draft=None
        w.open_stim_draft()
        assert w.stim_draft['record']['record_id']==rid and w.stim_parameters.rowCount()>15
        w.stim_reviewed.setChecked(True)
        w.stim_complete.setChecked(True)
        w.finalize_stim_form()
        wait_job(app,w)
        assert not errors and 'Sources moved' in w.stim_messages.text()
        w.filter_data_type.setCurrentText('stim')
        w.filter_kind.setCurrentText('data')
        assert w.records.rowCount()==1
        w.records.selectRow(0)
        app.processEvents()
        assert w.expression_files.rowCount()==6
        assert json.loads(w.raw.toPlainText())['record']['data_type']=='stim'
    finally:
        w.close()


@pytest.mark.parametrize('problem',['unreviewed','missing_dat','invalid_tiff','unsafe_path','unread_choice'])
def test_stim_invalid_selection_never_moves_files(store,stim_folder,problem):
    d=stim_draft_for(store,stim_folder)
    if problem=='unreviewed':
        d['record']['metadata']['reviewed']=False
    elif problem=='missing_dat':
        d['inputs']=[i for i in d['inputs'] if i['role']!='stim_recording']
    elif problem=='invalid_tiff':
        next(stim_folder.glob('imaging/*.tif')).write_bytes(b'not a TIFF')
    elif problem=='unsafe_path':
        d['inputs'][0]['relative_name']='../outside.tif'
    else:
        d['parameter_choices']['parameters_mat']=uid()
    with pytest.raises(LoggerError):
        store.finalize(d,move=True,source_complete=True)
    assert not store.index() and not store.pending_stim()
    assert all(Path(i['source']).exists() for i in d['inputs'])


def test_stim_multi_file_drop_is_copy_only_until_explicit_move(tmp_path):
    from PySide6.QtCore import QMimeData,QPointF,QUrl,Qt
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication
    from experiment_logger.stim_gui import FilesDrop
    app=QApplication.instance() or QApplication([])
    paths=[write_tiff(tmp_path/n) for n in ['first.tif','second.tif']]
    drop=FilesDrop('Synthetic drop')
    emitted=[]
    drop.dropped.connect(emitted.append)
    mime=QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    event=QDropEvent(QPointF(5,5),Qt.DropAction.CopyAction|Qt.DropAction.MoveAction,
                     mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    drop.dropEvent(event)
    assert [[Path(p) for p in group] for group in emitted]==[paths]
    assert event.dropAction()==Qt.DropAction.CopyAction
    assert all(p.exists() for p in paths)
    move_only=QDropEvent(QPointF(5,5),Qt.DropAction.MoveAction,mime,
                        Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    drop.dropEvent(move_only)
    assert not move_only.isAccepted() and len(emitted)==1
    drop.close()


def test_stim_cli_draft_and_explicit_move(store,stim_folder,tmp_path,capsys):
    from experiment_logger.cli import main
    config=tmp_path/'cli/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC CLI'})
    assert main(['--config',str(config),'stim-draft','--imaging',str(stim_folder/'imaging'),
                 '--stimpara',str(stim_folder/'stimPara004.mat'),str(stim_folder/'stim_parameters.csv')])==0
    d=json.loads(capsys.readouterr().out)
    assert d['record']['data_type']=='stim' and not d['record']['metadata']['reviewed']
    d['record']['metadata']['reviewed']=True
    from experiment_logger.store import Store
    cli_store=Store(store.metadata,store.roots,config.parent)
    d=cli_store.save_draft(d)
    with pytest.raises(SystemExit) as failed:
        main(['--config',str(config),'finalize',d['record']['record_id'],'--source-complete'])
    assert failed.value.code==1
    assert 'authorize' in capsys.readouterr().err
    assert main(['--config',str(config),'finalize',d['record']['record_id'],'--source-complete','--move'])==0
    assert json.loads(capsys.readouterr().out)['source_removed']


def test_stim_behavior_unique_link_pins_record_and_keeps_task_unchanged(store,draft,stim_folder):
    from experiment_logger.stim import refresh_behavior
    store.finalize(draft,source_complete=True)
    task_id=draft['record']['record_id']
    info=store.index()[task_id]
    task_path=store.metadata/info['relative_path']
    original=task_path.read_bytes()
    d=stim_draft_for(store,stim_folder)
    d['record']['metadata']['behavior_session']=True
    d=refresh_behavior(store,d)
    ref=d['record']['metadata']['behavior']
    assert ref['record_id']==task_id and ref['sha256']==info['sha256']
    d['record']['metadata']['reviewed']=True
    receipt=store.finalize(d,move=True,source_complete=True)
    r=store.read_record(d['record']['record_id'])
    assert r['metadata']['behavior']==ref and task_path.read_bytes()==original
    assert receipt['session_number']==1, 'The linked behavior record does not consume a Stim number'
    assert r['related_records']==[{'record_id':task_id,'sha256':info['sha256']}]
    assert r['metadata']['behavior']['csv_sha256']==store.read_record(task_id)['files'][0]['sha256']


@pytest.mark.parametrize('problem',['missing','wrong_mouse','wrong_date'])
def test_stim_behavior_required_and_context_checked_before_move(store,draft,stim_folder,problem):
    from experiment_logger.stim import refresh_behavior
    store.finalize(draft,source_complete=True)
    d=stim_draft_for(store,stim_folder)
    d['record']['metadata']['behavior_session']=True
    if problem!='missing':
        d=refresh_behavior(store,d)
        d['record']['mouse_id' if problem=='wrong_mouse' else 'session_date']=('008' if problem=='wrong_mouse' else '2026-10-16')
    d['record']['metadata']['reviewed']=True
    with pytest.raises(LoggerError,match='robot CSV|same mouse/date'):
        store.finalize(d,move=True,source_complete=True)
    assert all(Path(i['source']).exists() for i in d['inputs']) and not store.pending_stim()


def test_stim_behavior_ambiguity_and_content_matching(store,draft,sample,stim_folder):
    from experiment_logger.stim import choose_behavior_csv, refresh_behavior
    store.finalize(draft,source_complete=True)
    # Two real registrations with different synthetic content on the same day.
    with sample.open('a') as f:
        f.write('\t'.join(['0']*13)+'\n')
    second=store.new(sample)
    second['record'].update(operator='SYNTHETIC',experiment_type='water linear push')
    second['record']['metadata'].update(mouse_weight_g=25,training_day=13,is_imaging_session=True)
    second=store.save_draft(second)
    store.finalize(second,source_complete=True)
    d=stim_draft_for(store,stim_folder)
    d['record']['metadata']['behavior_session']=True
    d=refresh_behavior(store,d)
    assert len(d['behavior_choices'])==2 and d['record']['metadata']['behavior'] is None
    d=choose_behavior_csv(store,d,sample)
    assert d['record']['metadata']['behavior']['record_id']==second['record']['record_id']
    assert len(d['behavior_choices'])==1
    d['record']['metadata']['behavior_session']=False
    d=refresh_behavior(store,d)
    assert d['record']['metadata']['behavior'] is None and not d['record']['related_records']


def test_stim_behavior_ui_autofill_and_new_csv_registration(store,draft,sample,stim_folder,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    store.finalize(draft,source_complete=True)
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    app=QApplication.instance() or QApplication([])
    w=Window(config)
    errors=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:errors.append(args[-1]))
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *args:(str(sample),''))
    try:
        w.stim_draft=stim_draft_for(w.store,stim_folder)
        w.fill_stim_form()
        w.stim_behavior.setChecked(True)
        wait_job(app,w)
        assert w.stim_behavior_picker.currentData()['record_id']==draft['record']['record_id']
        assert 'robot-csv' in w.stim_behavior_path.text()
        w.choose_stim_robot_csv()
        wait_job(app,w)
        assert w.stim_behavior_picker.currentData()['record_id']==draft['record']['record_id']
        # Selecting unregistered bytes retains Stim and opens normal Behavior entry.
        with sample.open('a') as f:
            f.write('\t'.join(['0']*13)+'\n')
        stim_id=w.stim_draft['record']['record_id']
        w.choose_stim_robot_csv()
        wait_job(app,w)
        assert w.tabs.currentIndex()==0 and Path(w.draft['source_path'])==sample
        assert w.stim_draft['record']['record_id']==stim_id
        w.weight.setValue(25)
        w.task.setCurrentIndex(1)
        w.training_day.setValue(13)
        w.imaging.setCurrentIndex(2)
        w.complete.setChecked(True)
        w.finalize()
        wait_job(app,w)
        w.tabs.setCurrentWidget(w.stim_page)
        w.refresh_stim_behavior()
        wait_job(app,w)
        assert not errors
        selected=w.stim_behavior_picker.currentData()
        assert selected and selected['record_id']!=draft['record']['record_id']
        assert w.stim_draft['record']['record_id']==stim_id and w.stim_behavior.isChecked()
    finally:
        w.close()


@pytest.mark.parametrize('name',['comments','mat_comments'])
def test_stim_empty_comment_accepts_plain_text(store,stim_folder,tmp_path,name):
    import h5py
    from PySide6.QtWidgets import QApplication
    from experiment_logger.gui import Window
    # Reproduce real parameter files whose optional comments were not supplied.
    (stim_folder/'stim_parameters.csv').write_text('stim laser power (mW),mouse #,comments\n800 mW,007,\n')
    with h5py.File(stim_folder/'stimPara004.mat','a') as f:
        del f['session/experimentParams/comments']
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    app=QApplication.instance() or QApplication([])
    w=Window(config)
    try:
        w.stim_draft=stim_draft_for(w.store,stim_folder)
        w.fill_stim_form()
        parameters=w.stim_draft['record']['metadata']['extraction']['parameters']
        row=next(i for i,p in enumerate(parameters) if p['name']==name)
        assert parameters[row]['value'] is None
        w.stim_parameters.item(row,2).setText('Good response; repeat at lower power')
        d=w.collect_stim()
        assert {'name':name,'value':'Good response; repeat at lower power'} in d['record']['metadata']['parameter_overrides']
        saved=w.store.save_draft(d)
        assert w.store.load_draft(saved['record']['record_id'])['record']['metadata']==saved['record']['metadata']
    finally:
        w.close()


def test_stim_cleared_text_remains_unknown_after_reopen(store,stim_folder,tmp_path):
    from PySide6.QtWidgets import QApplication
    from experiment_logger.gui import Window
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    app=QApplication.instance() or QApplication([])
    w=Window(config)
    try:
        w.stim_draft=stim_draft_for(w.store,stim_folder)
        w.fill_stim_form()
        parameters=w.stim_draft['record']['metadata']['extraction']['parameters']
        row=next(i for i,p in enumerate(parameters) if p['name']=='comments')
        w.stim_parameters.item(row,2).setText('')
        saved=w.store.save_draft(w.collect_stim())
        w.stim_draft=w.store.load_draft(saved['record']['record_id'])
        w.fill_stim_form()
        assert {'name':'comments','value':None} in w.collect_stim()['record']['metadata']['parameter_overrides']
    finally:
        w.close()


def test_stim_invalid_number_identifies_field_and_preserves_sources(store,stim_folder,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication,QMessageBox
    from experiment_logger.gui import Window
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    app=QApplication.instance() or QApplication([])
    w=Window(config)
    errors=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:errors.append(args[-1]))
    try:
        w.stim_draft=stim_draft_for(w.store,stim_folder)
        w.fill_stim_form()
        parameters=w.stim_draft['record']['metadata']['extraction']['parameters']
        row=next(i for i,p in enumerate(parameters) if p['name']=='pulse_duration_s')
        w.stim_parameters.item(row,2).setText('five milliseconds')
        w.stim_complete.setChecked(True)
        w.stim_reviewed.setChecked(True)
        w.finalize_stim_form()
        assert errors and 'pulse duration' in errors[-1].lower()
        assert 'Expecting value' not in errors[-1] and 's' in errors[-1]
        assert w.stim_parameters.currentRow()==row
        picker=w.stim_choices['parameters_mat']
        prior=picker.currentData()
        picker.setCurrentIndex(0)
        assert picker.currentData()==prior, 'Rejected refresh keeps the source and its preview consistent'
        assert w.stim_parameters.item(row,2).text()=='five milliseconds'
        assert all(Path(i['source']).exists() for i in w.stim_draft['inputs'])
        assert not w.store.pending_stim() and not w.store.index()
    finally:
        w.close()
