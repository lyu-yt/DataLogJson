"""Synthetic task/expression moves and deterministic ETA behavior."""
from copy import deepcopy
from pathlib import Path
import errno

import pytest

from experiment_logger import transfers
from experiment_logger.io import LoggerError, Cancelled, atomic_json
from experiment_logger.time_estimate import TimeEstimate, duration
from test_expression import write_tiff


@pytest.fixture(params=['task', 'expression'])
def move_draft(request, store, draft, tmp_path):
    if request.param == 'task':
        return draft, [Path(draft['source_path']).resolve()]
    folder = tmp_path/'expression-source/261028/007'
    sources = [write_tiff(folder/name).resolve() for name in ('green_001.tif', 'red_001.tif', 'right_001.tif')]
    d = store.new_expression(folder)
    d['record']['operator'] = 'SYNTHETIC move tester'
    return store.save_draft(d), sources


def cross_device(monkeypatch, sources, windows=False):
    rename = transfers.rename_path
    def move(src,dst):
        if Path(src) in sources:
            error = OSError(errno.EINVAL if windows else errno.EXDEV, 'Synthetic cross-device move')
            if windows:
                error.winerror = 17
            raise error
        return rename(src,dst)
    monkeypatch.setattr(transfers,'rename_path',move)


def finish(store,d,**kwargs):
    return store.finalize(d,source_complete=True,move=True,**kwargs)


def test_task_and_expression_direct_moves_share_recovery_and_progress(store,move_draft):
    d, sources = move_draft
    ticks, phases = [], []
    def progress(n,t): ticks.append(n/t)
    progress.phase = lambda label,measurable=True: phases.append((label,measurable))
    result = finish(store,d,progress=progress)
    assert result['git_committed'] and result['source_removed']
    assert all(f['method']=='direct_move' for f in result['files'])
    assert not any(p.exists() for p in sources) and not store.pending_transfers()
    assert ticks == sorted(ticks) and ticks[-1] == 1
    assert any('Verifying moved file' in label and measured for label,measured in phases)
    assert any('Saving metadata' in label and not measured for label,measured in phases)
    r = store.read_record(d['record']['record_id'])
    if r['data_type']=='task':
        assert r['files'][0]['csv']['row_count']==40 and result['csv_relative_path'].endswith('.csv')
    else:
        assert len(r['files'])==3
        for key,channel in r['metadata']['channels'].items():
            assert channel['file_ids']==[f['file_id'] for f in r['files'] if f['role']==key]


@pytest.mark.parametrize('windows',[False,True])
def test_task_and_expression_copy_fallback_verifies_before_removal(store,move_draft,monkeypatch,windows):
    d,sources=move_draft
    before={p:p.read_bytes() for p in sources}
    cross_device(monkeypatch,sources,windows)
    phases,ticks=[],[]
    def progress(n,t): ticks.append(n/t)
    progress.phase=lambda label,measurable=True: phases.append((label,ticks[-1] if ticks else 0))
    result=finish(store,d,progress=progress)
    assert result['source_removed'] and all(f['method']=='verified_copy' for f in result['files'])
    assert not any(p.exists() for p in sources)
    assert ticks==sorted(ticks) and ticks[-1]==1
    assert all(percent<1 for label,percent in phases if label.startswith('Verifying archived copy'))
    r=store.read_record(d['record']['record_id'])
    for f in r['files']:
        target=Path(store.file_locations(f['file_id'])[0]['path'])
        source=next(p for p in sources if p.name==f['original_filename'])
        assert target.read_bytes()==before[source]


@pytest.mark.parametrize('stage',['renamed','prepared','published'])
def test_move_recovery_does_not_duplicate_task_or_expression_records(store,move_draft,stage):
    d,sources=move_draft
    with pytest.raises(LoggerError,match='Injected interruption'):
        finish(store,d,failpoint=stage)
    assert store.pending_transfers()
    store.recover()
    assert len(store.index())==2 and not store.pending_transfers()
    assert not any(p.exists() for p in sources)
    assert store.recover()==[]


def test_copy_fallback_sources_survive_failed_commit_until_explicit_recovery(store,move_draft,monkeypatch):
    d,sources=move_draft
    cross_device(monkeypatch,sources)
    git=store.git
    def fail(*args):
        if 'commit' in args: raise LoggerError('Synthetic commit failure')
        return git(*args)
    monkeypatch.setattr(store,'git',fail)
    receipt=finish(store,d)
    assert not receipt['git_committed'] and not receipt['source_removed']
    assert all(p.exists() for p in sources)
    monkeypatch.setattr(store,'git',git)
    store.recover()
    assert not store.pending_transfers() and not any(p.exists() for p in sources)


def test_move_cancelled_after_rename_resumes_the_archived_file(store,move_draft):
    d,sources=move_draft
    with pytest.raises(Cancelled):
        finish(store,d,cancel=lambda:not sources[0].exists())
    assert not store.index() and store.pending_transfers()
    store.recover()
    assert len(store.index())==2 and not store.pending_transfers()


def test_permission_errors_never_trigger_copy_fallback(store,move_draft,monkeypatch):
    d,sources=move_draft
    def deny(*args): raise PermissionError('Synthetic locked source')
    monkeypatch.setattr(transfers,'rename_path',deny)
    with pytest.raises(PermissionError): finish(store,d)
    assert all(p.exists() for p in sources) and not store.index()


def test_eta_measures_rate_resets_for_stages_and_handles_stalls():
    clock=[0.0]
    eta=TimeEstimate(lambda:clock[0])
    eta.phase('Copying',True)
    assert 'Estimating' in eta.text()
    clock[0]=1
    eta.observe(10,100)
    assert 'About 9 s remaining' in eta.text()
    clock[0]=7
    assert 'Waiting for storage' in eta.text()
    eta.phase('Verifying archived copy',True)
    assert 'Estimating' in eta.text()
    clock[0]=8
    eta.observe(20,100)
    assert 'About 8 s remaining' in eta.text()
    eta.phase('Saving metadata and Git history',False)
    assert 'Elapsed' in eta.text() and 'remaining' not in eta.text()


def test_eta_accepts_changed_work_budget_without_stale_negative_estimates():
    clock=[0.0]
    eta=TimeEstimate(lambda:clock[0])
    clock[0]=1
    eta.observe(20,200)
    assert '9 s remaining' in eta.text()
    clock[0]=2
    eta.observe(40,100)
    assert '3 s remaining' in eta.text()
    eta.observe(1,10)  # Legacy callback begins another pass: start measuring again.
    assert 'Estimating' in eta.text()
    eta.observe(10,10)
    assert 'Finishing' in eta.text() and 'remaining' not in eta.text()
    assert duration(61)=='1 min 01 s' and duration(3661)=='1 h 01 min'


def test_expression_gui_explicit_move_option_and_eta_cleanup(store,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication,QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    app=QApplication.instance() or QApplication([])
    folder=tmp_path/'source/261028/007'
    write_tiff(folder/'green_001.tif')
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    w=Window(config)
    errors=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:errors.append(a[-1]))
    try:
        w.load_expression_folder(folder)
        assert not w.expression_move.isChecked()
        w.expression_move.setChecked(True)
        w.expression_complete.setChecked(True)
        w.finalize_expression_form()
        assert w.progress_eta.isVisibleTo(w)
        wait_job(app,w)
        assert not errors and 'Moved 1 TIFF(s)' in w.expression_messages.text()
        assert not (folder/'green_001.tif').exists()
        assert w.progress_eta.isHidden() and not w.progress_timer.isActive()
    finally:
        w.close()


def test_worker_estimate_is_displayed_and_hidden_when_finished(tmp_path,monkeypatch):
    import threading
    import time
    from PySide6.QtWidgets import QApplication
    from experiment_logger import gui
    from test_gui import wait_job
    app=QApplication.instance() or QApplication([])
    clock=[0.0]
    monkeypatch.setattr(gui,'TimeEstimate',lambda:TimeEstimate(lambda:clock[0]))
    w=gui.Window(tmp_path/'missing-config.json')
    started,release=threading.Event(),threading.Event()
    def work(progress,cancel):
        progress.phase('Copying synthetic movie',True)
        clock[0]=1
        progress(10,100)
        started.set()
        release.wait(5)
        progress.phase('Saving metadata and Git history',False)
        return 'Synthetic result'
    try:
        w.run_job(work,lambda result:None,refresh_archive=False)
        deadline=time.monotonic()+5
        while not started.is_set() and time.monotonic()<deadline:
            app.processEvents()
            time.sleep(0.001)
        assert started.is_set()
        w.update_transfer_estimate()
        assert 'Copying synthetic movie' in w.progress_eta.text()
        assert 'About 9 s remaining' in w.progress_eta.text()
        release.set()
        wait_job(app,w)
        assert w.progress_eta.isHidden() and not w.progress_timer.isActive()
    finally:
        release.set()
        if w.job: w.job.wait(5000)
        w.close()


def test_expression_move_can_reuse_a_verified_copy_from_interrupted_copy_mode(store,tmp_path):
    folder=tmp_path/'originals/261028/007'
    source=write_tiff(folder/'green_001.tif')
    d=store.new_expression(folder)
    d['record']['operator']='SYNTHETIC'
    d=store.save_draft(d)
    with pytest.raises(LoggerError,match='Injected interruption'):
        store.finalize_expression(d,source_complete=True,failpoint='copied')
    before=list((Path(store.roots['archive'])/'expression').rglob('*.tif'))
    assert len(before)==1 and source.exists()
    receipt=store.finalize_expression(d,source_complete=True,move=True)
    assert receipt['source_removed'] and not source.exists()
    assert list((Path(store.roots['archive'])/'expression').rglob('*.tif'))==before


def test_new_move_does_not_replace_pending_metadata_from_an_earlier_copy(store,draft,sample):
    with pytest.raises(LoggerError,match='Injected interruption'):
        store.finalize(draft,source_complete=True,failpoint='prepared')
    with pytest.raises(LoggerError,match='Use Recover'):
        store.finalize(draft,source_complete=True,move=True)
    assert sample.exists() and not store.pending_transfers()
    store.recover()
    assert sample.exists()  # Earlier copy-only authorization is preserved.
    assert len(store.index())==2
