"""Disposable synthetic CZIs/OME-TIFFs only; acquisition files are never moved."""
from copy import deepcopy
from pathlib import Path
import errno
import json

import numpy as np
import pytest
import tifffile
from pylibCZIrw import czi

from experiment_logger.histology import new_histology,colony_reference
from experiment_logger.colony import new_profile
from experiment_logger.histology_czi import inspect_czi,export_stitched
from experiment_logger.io import LoggerError,Cancelled,atomic_json,digest,read_json,uid
from experiment_logger.models import validate,RESOURCE,SCHEMA_VERSION


def synthetic_czi(path, width=32, height=24):
    path.parent.mkdir(parents=True,exist_ok=True)
    values=np.arange(width*height,dtype=np.uint16).reshape(height,width)
    with czi.create_czi(str(path)) as writer:
        for z in range(2):
            for channel in range(2):
                for tile,x in enumerate((0,width-8)):
                    data=(values+z*1000+channel*100+tile*500)[...,None]
                    writer.write(data,location=(x,0),plane={'Z':z,'C':channel},scene=0)
        writer.write_metadata(channel_names={0:'SYNTHETIC green',1:'SYNTHETIC red'},
                              scale_x=.6e-6,scale_y=.6e-6,scale_z=2e-6)
    return path


@pytest.fixture
def confocal(tmp_path):
    return synthetic_czi(tmp_path/'source/261120/007-3-1C/New-03.czi')


def ready_histology(store,confocal,draft,export=False):
    if draft['record']['record_id'] not in store.index():
        store.finalize(draft,source_complete=True)
    info=store.index()[draft['record']['record_id']]
    d=store.new_histology([confocal])
    r=d['record']
    ref={'record_id':info['record_id'],'sha256':info['sha256']}
    r.update(operator='SYNTHETIC histology',notes='SYNTHETIC slice observation')
    r['metadata'].update(linked_record=ref,export_tiff=export,reviewed=True,marker_notes='operator supplied marker')
    r['related_records']=[ref]
    return store.save_draft(d)


def test_confocal_metadata_identity_and_tile_count(confocal):
    info=inspect_czi(confocal)
    assert info['bounds']['C']==[0,2] and info['bounds']['Z']==[0,2]
    assert info['tile_count']==2 and info['full_resolution_subblocks']==8
    assert info['pixel_size_um']=={'x':.6,'y':.6,'z':2}
    d=new_histology([confocal.parent])
    assert d['record']['mouse_id']=='007' and d['record']['session_date']=='2026-11-20'
    assert d['record']['metadata']['slice_index']=='3-1C'
    assert d['record']['metadata']['images'][0]['source_specimen_label']=='007-3-1C'
    assert not d['record']['metadata']['export_tiff'] and not validate(d['record'])


def test_stitched_export_preserves_pixels_channels_z_calibration_and_overlaps(confocal,tmp_path):
    info=inspect_czi(confocal)
    destination=tmp_path/'derived.ome.tif'
    result=export_stitched(confocal,destination,info)
    with tifffile.TiffFile(destination) as f:
        assert f.is_ome and f.is_bigtiff and f.series[0].axes=='ZCYX'
        data=f.asarray()
        assert data.shape==(2,2,24,56) and data.dtype==np.uint16
        assert 'PhysicalSizeX="0.6"' in f.ome_metadata
    original=np.arange(32*24,dtype=np.uint16).reshape(24,32)
    for z in range(2):
        for channel in range(2):
            expected=np.zeros((24,56),dtype=np.uint16)
            expected[:,:32]=original+z*1000+channel*100
            expected[:,24:]=original+z*1000+channel*100+500
            np.testing.assert_array_equal(data[z,channel],expected)
    assert result['overlap_method']=='highest_m_index'
    assert result['pixel_verification']=='all exported pixels matched'


def test_export_streams_multiple_output_tiles_with_edge_cropping(tmp_path):
    source=synthetic_czi(tmp_path/'large-synthetic.czi',width=300,height=520)
    output=tmp_path/'large-synthetic.ome.tif'
    result=export_stitched(source,output,inspect_czi(source))
    assert result['uncompressed_pixel_bytes']==2*2*520*592*2
    with tifffile.TiffFile(output) as f:
        assert f.series[0].shape==(2,2,520,592)
        assert len(f.pages[0].dataoffsets)==4


@pytest.mark.parametrize('export',[False,True])
def test_histology_move_and_linked_amendment_are_atomic(store,confocal,draft,export):
    d=ready_histology(store,confocal,draft,export)
    original=confocal.read_bytes()
    target=draft['record']['record_id']
    target_path=store.metadata/store.index()[target]['relative_path']
    frozen=target_path.read_bytes()
    result=store.finalize(d,move=True,source_complete=True)
    assert result['git_committed'] and result['source_removed'] and not confocal.exists()
    assert result['session_number']==1 and not store.pending_histology()
    folder=store.metadata/result['record_directory']
    assert {p.name for p in folder.iterdir()}=={'histology.json','session.json','amendment.json','manifest.json'}
    record=store.read_record(d['record']['record_id'])
    assert not validate(record,True) and record['notes']=='SYNTHETIC slice observation'
    expected_root='histology/mouse_007/261120/007-3-1C/1/'
    raw=next(f for f in record['files'] if f['role']=='confocal_czi')
    loc=store.file_locations(raw['file_id'])[0]
    assert loc['relative_path']==expected_root+'czi/New-03.czi'
    assert Path(loc['path']).read_bytes()==original
    assert target_path.read_bytes()==frozen
    amendments=store.amendments(target)
    assert len(amendments)==1 and amendments[0]['record_id']==result['amendment_id']
    link=amendments[0]['corrected_values']['additional_histology']
    assert link['record_id']==record['record_id'] and link['sha256']==store.index()[record['record_id']]['sha256']
    assert link['file_ids']==[f['file_id'] for f in record['files']]
    assert not any(n.endswith(('.czi','.tif')) for n in store.git('ls-files').splitlines())
    if export:
        tiff=next(f for f in record['files'] if f['role']=='histology_tiff')
        assert store.file_locations(tiff['file_id'])[0]['relative_path']==expected_root+'tif/New-03.ome.tif'
        provenance=record['metadata']['images'][0]['tiff_export']
        assert provenance['source_file_id']==raw['file_id'] and provenance['source_sha256']==raw['sha256']


@pytest.mark.parametrize('stage',['renamed','transferred','prepared','published'])
def test_histology_publication_and_export_recover_without_duplicate_amendments(store,confocal,draft,stage):
    d=ready_histology(store,confocal,draft,True)
    with pytest.raises(LoggerError,match='Injected'):
        store.finalize(d,move=True,source_complete=True,failpoint=stage)
    assert store.pending_histology()
    store.recover()
    assert not store.pending_histology() and len(store.amendments(draft['record']['record_id']))==1
    assert len(store.index())==5 and store.recover()==[]
    r=store.read_record(d['record']['record_id'])
    assert len(r['files'])==2 and not validate(r,True)


def force_histology_copy(monkeypatch,confocal):
    from experiment_logger import transfers
    original=transfers.rename_path
    def cross_device(source,target):
        if Path(source)==confocal.resolve():
            raise OSError(errno.EXDEV,'SYNTHETIC cross-device move')
        return original(source,target)
    monkeypatch.setattr(transfers,'rename_path',cross_device)


def test_histology_git_failure_retains_copied_source_until_complete_link_commit(store,confocal,draft,monkeypatch):
    d=ready_histology(store,confocal,draft,True)
    force_histology_copy(monkeypatch,confocal)
    git=store.git
    def broken(*args):
        if 'commit' in args:raise LoggerError('SYNTHETIC Git failure')
        return git(*args)
    monkeypatch.setattr(store,'git',broken)
    result=store.finalize(d,move=True,source_complete=True)
    assert not result['git_committed'] and not result['source_removed'] and confocal.exists()
    monkeypatch.setattr(store,'git',git)
    store.recover()
    assert not confocal.exists() and not store.pending_histology()
    assert len(store.amendments(draft['record']['record_id']))==1


def test_cancelled_tiff_export_can_resume_the_already_moved_czi(store,confocal,draft):
    d=ready_histology(store,confocal,draft,True)
    exporting=[False]
    def progress(n,total):
        if exporting[0]:exporting[0]='cancel'
    def stage(label,measurable=False):
        if label.startswith('Exporting stitched'):exporting[0]=True
    progress.phase=stage
    with pytest.raises(Cancelled):
        store.finalize(d,move=True,source_complete=True,progress=progress,cancel=lambda:exporting[0]=='cancel')
    assert not confocal.exists() and store.pending_histology()
    assert not list((Path(store.roots['archive'])/'histology').rglob('*.partial'))
    assert not store.amendments(draft['record']['record_id'])
    store.recover()
    assert len(store.amendments(draft['record']['record_id']))==1 and not store.pending_histology()


@pytest.mark.parametrize('problem',['no_link','wrong_mouse','changed_parent','unreviewed','unsafe_slice','changed_czi'])
def test_histology_preflight_blocks_invalid_registrations_before_moving(store,confocal,draft,problem):
    d=ready_histology(store,confocal,draft)
    if problem=='no_link':
        d['record']['metadata']['linked_record']=None;d['record']['related_records']=[]
    elif problem=='wrong_mouse':d['record']['mouse_id']='008'
    elif problem=='changed_parent':
        d['record']['metadata']['linked_record']['sha256']='0'*64
        d['record']['related_records']=deepcopy([d['record']['metadata']['linked_record']])
    elif problem=='unreviewed':d['record']['metadata']['reviewed']=False
    elif problem=='unsafe_slice':d['record']['metadata']['slice_index']='../other'
    else:
        with confocal.open('ab') as f:f.write(b'SYNTHETIC change')
    with pytest.raises(LoggerError):store.finalize(d,move=True,source_complete=True)
    assert confocal.exists() and not store.pending_histology() and len(store.index())==2


def test_histology_gui_drop_review_save_finalize_and_open_link(store,confocal,draft,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication,QMessageBox
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
    try:
        w.load_histology_paths([str(confocal.parent)])
        wait_job(app,w)
        assert not errors and w.histology_mouse.text()=='007' and w.histology_slice.text()=='3-1C'
        assert w.histology_date.text()=='2026-11-20' and w.histology_images.rowCount()==1
        assert w.histology_target.currentData() is None and not w.histology_export.isChecked()
        for i in range(1,w.histology_target.count()):
            if w.histology_target.itemData(i)['record_id']==draft['record']['record_id']:
                w.histology_target.setCurrentIndex(i);break
        w.histology_notes.setPlainText('SYNTHETIC observation')
        w.histology_export.setChecked(True)
        w.save_histology_draft()
        rid=w.histology_draft['record']['record_id']
        w.histology_draft=None
        w.open_histology_draft()
        assert w.histology_draft['record']['record_id']==rid and w.histology_export.isChecked()
        w.histology_reviewed.setChecked(True);w.histology_complete.setChecked(True)
        w.finalize_histology_form();wait_job(app,w)
        assert not errors and 'amendment finalized' in w.histology_messages.text()
        w.filter_data_type.setCurrentText('histology');w.filter_kind.setCurrentText('data')
        assert w.records.rowCount()==1
        w.records.selectRow(0);app.processEvents()
        assert w.expression_files.rowCount()==2 and w.linked_behavior_button.isEnabled()
        w.open_linked_behavior();app.processEvents()
        assert json.loads(w.raw.toPlainText())['record']['record_id']==draft['record']['record_id']
    finally:w.close()


def test_export_keeps_separate_scene_sizes_and_rejects_unknown_varying_axes(tmp_path):
    source=tmp_path/'multiple-scenes.czi'
    with czi.create_czi(str(source)) as writer:
        for scene,height,width,x in [(0,16,24,0),(1,20,32,100)]:
            for z in range(2):
                for channel in range(2):
                    writer.write(np.full((height,width,1),scene*100+z*10+channel,dtype=np.uint16),
                                 location=(x,0),plane={'Z':z,'C':channel},scene=scene)
        writer.write_metadata(channel_names={0:'green',1:'red'})
    info=inspect_czi(source)
    output=tmp_path/'scenes.ome.tif'
    export_stitched(source,output,info)
    with tifffile.TiffFile(output) as f:
        assert len(f.series)==2
        assert f.series[0].shape==(2,2,16,24) and f.series[1].shape==(2,2,20,32)
        assert f.series[1].asarray()[1,1,0,0]==111
    info['bounds']['H']=[0,2]
    with pytest.raises(LoggerError,match='another varying axis'):
        export_stitched(source,tmp_path/'unsupported.ome.tif',info)
    assert not (tmp_path/'unsupported.ome.tif').exists()


def test_histology_rejects_mixed_slices_and_stale_draft(store,confocal,draft,tmp_path):
    second=synthetic_czi(tmp_path/'source/261120/007-4-1A/second.czi')
    with pytest.raises(LoggerError,match='one brain slice'):
        store.new_histology([confocal,second])
    d=ready_histology(store,confocal,draft)
    store.save_draft(d)
    with pytest.raises(LoggerError,match='Stale'):
        store.finalize(d,move=True,source_complete=True)
    assert confocal.exists()


def test_histology_cli_requires_review_link_and_explicit_move(store,confocal,draft,tmp_path,capsys):
    from experiment_logger.cli import main
    from experiment_logger.store import Store
    store.finalize(draft,source_complete=True)
    config=tmp_path/'cli/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC CLI'})
    assert main(['--config',str(config),'histology-draft',str(confocal),
                 '--target-record',draft['record']['record_id'],'--export-tiff'])==0
    d=json.loads(capsys.readouterr().out)
    assert not d['record']['metadata']['reviewed']
    cli_store=Store(store.metadata,store.roots,config.parent)
    d['record']['metadata']['reviewed']=True
    d=cli_store.save_draft(d)
    with pytest.raises(SystemExit):
        main(['--config',str(config),'finalize',d['record']['record_id'],'--source-complete'])
    capsys.readouterr()
    assert confocal.exists()
    assert main(['--config',str(config),'finalize',d['record']['record_id'],'--source-complete','--move'])==0
    assert json.loads(capsys.readouterr().out)['amendment_id']


def colony_histology(store,confocal,export=False):
    store.save_mouse_profile(new_profile('007','SYNTHETIC colony'),'SYNTHETIC colony')
    d=store.new_histology([confocal])
    d['record']['operator']='SYNTHETIC histology'
    d['record']['metadata'].update(reviewed=True,export_tiff=export,
                                   colony_profile=colony_reference(store,'007'))
    return store.save_draft(d)


@pytest.mark.parametrize('export',[False,True])
def test_colony_only_histology_registers_without_amendment(store,confocal,export):
    d=colony_histology(store,confocal,export)
    profile_path=store.metadata/'records/mouse_007/mouse_info.json'
    original=profile_path.read_bytes()
    result=store.finalize(d,move=True,source_complete=True)
    assert result['git_committed'] and result['source_removed'] and not result.get('amendment_id')
    assert not confocal.exists() and not store.pending_histology()
    assert {p.name for p in (store.metadata/result['record_directory']).iterdir()}=={'histology.json','session.json','manifest.json'}
    r=store.read_record(d['record']['record_id'])
    assert not validate(r,True) and not r['related_records'] and r['metadata']['linked_record'] is None
    profile=r['metadata']['colony_profile']
    assert profile['source_sha256']==digest(original) and profile['snapshot']==json.loads(original)
    assert profile['profile_id']==json.loads(original)['profile_id'] and profile['revision']==1
    assert profile_path.read_bytes()==original and len(store.index())==2
    assert len(r['files'])==(2 if export else 1)


@pytest.mark.parametrize('stage',['renamed','prepared','published'])
def test_colony_only_histology_recovers_without_amendment(store,confocal,stage):
    d=colony_histology(store,confocal,True)
    with pytest.raises(LoggerError,match='Injected'):
        store.finalize(d,move=True,source_complete=True,failpoint=stage)
    # Changing the editable profile cannot change the captured scientific context.
    profile=store.load_mouse_profile('007')
    profile['notes']='SYNTHETIC later update'
    store.save_mouse_profile(profile,'SYNTHETIC colony')
    store.recover()
    r=store.read_record(d['record']['record_id'])
    assert r['metadata']['colony_profile']['revision']==1
    assert r['metadata']['colony_profile']['snapshot']['notes'] is None
    assert len(store.index())==2 and not store.pending_histology() and store.recover()==[]


def test_colony_only_copy_retains_source_until_git_commit(store,confocal,monkeypatch):
    d=colony_histology(store,confocal)
    force_histology_copy(monkeypatch,confocal)
    git=store.git
    def broken(*args):
        if 'commit' in args:raise LoggerError('SYNTHETIC Git failure')
        return git(*args)
    monkeypatch.setattr(store,'git',broken)
    result=store.finalize(d,move=True,source_complete=True)
    assert not result['git_committed'] and confocal.exists() and not result.get('amendment_id')
    monkeypatch.setattr(store,'git',git)
    store.recover()
    assert not confocal.exists() and len(store.index())==2 and not store.pending_histology()


@pytest.mark.parametrize('problem',['profile_changed','mouse_changed','snapshot_mismatch','invalid_experiment'])
def test_colony_only_preflight_rejects_invalid_identity_before_moving(store,confocal,problem):
    d=colony_histology(store,confocal)
    if problem=='profile_changed':
        profile=store.load_mouse_profile('007')
        profile['notes']='SYNTHETIC changed before review'
        store.save_mouse_profile(profile,'SYNTHETIC colony')
    elif problem=='mouse_changed':d['record']['mouse_id']='008'
    elif problem=='snapshot_mismatch':d['record']['metadata']['colony_profile']['snapshot']['revision']=2
    else:
        ref={'record_id':uid(),'sha256':'0'*64}
        d['record']['metadata']['linked_record']=ref
        d['record']['related_records']=[ref]
    with pytest.raises(LoggerError):store.finalize(d,move=True,source_complete=True)
    assert confocal.exists() and not store.pending_histology() and not store.index()


def test_histology_gui_logs_with_only_colony_profile(store,confocal,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication,QMessageBox
    from experiment_logger.gui import Window
    from test_gui import wait_job
    store.save_mouse_profile(new_profile('007','SYNTHETIC colony'),'SYNTHETIC colony')
    config=tmp_path/'ui/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC UI'})
    app=QApplication.instance() or QApplication([])
    w=Window(config)
    errors=[];confirmations=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:errors.append(args[-1]))
    def confirm(*args):
        confirmations.append(args[-1]);return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox,'question',confirm)
    try:
        w.load_histology_paths([str(confocal)]);wait_job(app,w)
        assert not errors and 'revision 1' in w.histology_profile_status.text()
        assert w.histology_target.count()==1 and w.histology_target.currentData() is None
        w.save_histology_draft()
        w.histology_draft=None;w.open_histology_draft()
        w.histology_reviewed.setChecked(True);w.histology_complete.setChecked(True)
        w.finalize_histology_form();wait_job(app,w)
        assert not errors and 'Histology finalized locally' in w.histology_messages.text()
        assert any('No experiment amendment will be created' in text for text in confirmations)
        w.filter_data_type.setCurrentText('histology');w.filter_kind.setCurrentText('data')
        assert w.records.rowCount()==1
        w.records.selectRow(0);app.processEvents()
        assert w.expression_files.rowCount()==1 and not w.linked_behavior_button.isEnabled()
        assert json.loads(w.raw.toPlainText())['record']['metadata']['colony_profile']['mouse_id']=='007'
    finally:w.close()


def test_histology_cli_without_experiment_uses_colony_profile(store,confocal,tmp_path,capsys):
    from experiment_logger.cli import main
    from experiment_logger.store import Store
    store.save_mouse_profile(new_profile('007','SYNTHETIC colony'),'SYNTHETIC colony')
    config=tmp_path/'cli/config.json'
    atomic_json(config,{'roots':store.roots,'operator':'SYNTHETIC CLI'})
    assert main(['--config',str(config),'histology-draft',str(confocal)])==0
    d=json.loads(capsys.readouterr().out)
    assert d['record']['metadata']['colony_profile']['revision']==1
    cli_store=Store(store.metadata,store.roots,config.parent)
    d['record']['metadata']['reviewed']=True
    d=cli_store.save_draft(d)
    assert main(['--config',str(config),'finalize',d['record']['record_id'],'--source-complete','--move'])==0
    assert not json.loads(capsys.readouterr().out).get('amendment_id')


def legacy_histology_schema(store):
    schema=read_json(RESOURCE/'schemas/record.json')
    schema['properties']['schema_version']['const']='1.6.0'
    schema['$id']='urn:mouse-experiment-logger:record:1.6.0'
    schema['definitions']['mouse_profile']['properties']['schema_version']['const']='1.6.0'
    metadata=schema['definitions']['histology_metadata']
    del metadata['properties']['colony_profile']
    metadata['required'].remove('colony_profile')
    for rule in schema['allOf']:
        condition=rule.get('if',{}).get('properties',{})
        if condition.get('data_type',{}).get('const')=='histology' and condition.get('record_state',{}).get('const')=='finalized':
            final=rule['then']['properties']['metadata']
            del final['anyOf']
            final['properties']['linked_record']={'type':'object'}
    path=store.metadata/'schemas/record_v1.6.0.json'
    atomic_json(path,schema)
    return path


def test_histology_opens_old_draft_and_snapshots_old_profile_without_rewriting(store,confocal):
    d=colony_histology(store,confocal)
    schema=legacy_histology_schema(store)
    original_rules=schema.read_bytes()
    path=store.metadata/'records/mouse_007/mouse_info.json'
    profile=read_json(path)
    profile['schema_version']='1.6.0'
    atomic_json(path,profile)
    original=path.read_bytes()
    d['record']['schema_version']='1.6.0'
    del d['record']['metadata']['colony_profile']
    atomic_json(store.local/'drafts'/f"{d['record']['record_id']}.json",d)
    loaded=store.load_draft(d['record']['record_id'])
    assert loaded['record']['schema_version']==SCHEMA_VERSION
    assert loaded['record']['metadata']['colony_profile'] is None
    store.finalize(loaded,move=True,source_complete=True)
    r=store.read_record(d['record']['record_id'])
    assert r['metadata']['colony_profile']['snapshot']['schema_version']=='1.6.0'
    assert r['metadata']['colony_profile']['source_sha256']==digest(original)
    assert path.read_bytes()==original and schema.read_bytes()==original_rules


def test_histology_recovers_old_unpublished_transfer_journal(store,confocal,draft):
    d=ready_histology(store,confocal,draft)
    schema=legacy_histology_schema(store)
    original_rules=schema.read_bytes()
    plan=store.plan_histology(d)
    plan['record']['schema_version']='1.6.0'
    del plan['record']['metadata']['colony_profile']
    plan['authorized']=True
    atomic_json(store.local/'histology-transfers'/f"{d['record']['record_id']}.json",plan)
    store.recover()
    r=store.read_record(d['record']['record_id'])
    assert r['schema_version']==SCHEMA_VERSION and r['metadata']['colony_profile'] is None
    assert len(store.amendments(draft['record']['record_id']))==1
    assert schema.read_bytes()==original_rules and not store.pending_histology()
