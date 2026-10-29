"""Synthetic imaging only: no real movies or NAS acquisition files."""
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
