"""Histology registration with colony identity and optional experiment amendments."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import os
import re

from .histology_czi import inspect_czi, export_spec, export_stitched
from .io import (Cancelled, LoggerError, atomic_json, digest, encode, hash_file, lock,
                 loads, now, read_json, rename_path, resolve, signature, uid)
from .layout import day_folder, mouse_folder
from .models import envelope, new_task, require_valid, seal_record
from .transfers import finish_transfers, phase, _identity


def new_histology(paths, cancel=None):
    sources = set()
    for supplied in paths:
        root = Path(supplied).expanduser()
        if not root.is_absolute() or root.is_symlink() or root.is_junction() or not root.exists():
            raise LoggerError('Drop existing CZI files/folders; links are not followed')
        if root.is_dir():
            for parent, directories, files in os.walk(root, followlinks=False):
                if cancel and cancel():
                    raise Cancelled('Histology discovery cancelled')
                directories[:] = [n for n in directories if not (Path(parent)/n).is_symlink()
                                  and not (Path(parent)/n).is_junction()]
                sources.update((Path(parent)/n).resolve() for n in files
                               if n.lower().endswith('.czi') and not (Path(parent)/n).is_symlink())
        elif root.suffix.lower() == '.czi':
            sources.add(root.resolve())
    if not sources:
        raise LoggerError('No CZI files found in the selected files/folders')
    inputs, images, mice, slices, dates = [], [], set(), set(), set()
    for source in sorted(sources):
        before = signature(source)
        info = inspect_czi(source, cancel)
        if signature(source) != before:
            raise LoggerError('CZI changed during discovery')
        fid = uid()
        inputs.append({'source':str(source),'file_id':fid,'source_signature':list(before)})
        label = next((p.name for p in list(source.parents)[:8]
                      if re.fullmatch(r'[0-9]{3}-.+',p.name)), None)
        if label:
            mouse, slice_index = label.split('-',1)
            mice.add(mouse)
            slices.add(slice_index)
        else:
            for parent in list(source.parents)[:8]:
                match = re.fullmatch(r'(?:mouse_?)?([0-9]{3})',parent.name,re.I)
                if match:
                    mice.add(match.group(1))
        date = info['acquisition_time_local'][:10] if info['acquisition_time_local'] else None
        if not date:
            for parent in list(source.parents)[:8]:
                if re.fullmatch(r'[0-9]{6}',parent.name):
                    try:
                        date = datetime.strptime('20'+parent.name,'%Y%m%d').date().isoformat()
                        break
                    except ValueError:
                        pass
        if date:
            dates.add(date)
        images.append({'file_id':fid,'original_filename':source.name,'source_specimen_label':label,
                       'czi':info,'tiff_file_id':None,'tiff_export':None})
    if len(mice)>1 or len(slices)>1:
        raise LoggerError('Choose files from one mouse and one brain slice per histology registration')
    r = {**envelope('data'),'data_type':'histology','experiment_type':'histology','session_id':uid(),
         'mouse_id':next(iter(mice)) if len(mice)==1 else None,
         'session_date':next(iter(dates)) if len(dates)==1 else None,
         'acquisition':deepcopy(new_task()['acquisition']),'files':[],'related_records':[],'notes':None,
         'metadata':{'slice_index':next(iter(slices)) if len(slices)==1 else None,
                     'brain_region':None,'hemisphere':None,'marker_notes':None,
                     'linked_record':None,'colony_profile':None,
                     'export_tiff':False,'reviewed':False,'images':images}}
    starts = [i['czi']['acquisition_time_local'] for i in images if i['czi']['acquisition_time_local']]
    if starts:
        r['acquisition'].update(started_at=min(starts),clock_id='czi_acquisition_clock')
    r['acquisition']['software']={'metadata_reader':'pylibCZIrw','reader_version':images[0]['czi']['reader_version']}
    return {'revision':0,'record':r,'inputs':inputs}


def experiment_candidates(store, mouse_id):
    mouse_folder(mouse_id)
    index = store.index()
    return [{'label':f"{i['session_date']} | {i['experiment_type']} | {i['record_type']} | {i['record_id'][:8]}",
             'reference':{'record_id':i['record_id'],'sha256':i['sha256']}}
            for i in sorted(index.values(),key=lambda i:(i.get('session_date') or '',i['record_id']),reverse=True)
            if i['integrity']=='ok' and i['mouse_id']==mouse_id and i['record_type'] in ('data','session')
            and i.get('data_type')!='histology']


def colony_reference(store, mouse_id):
    """Capture the saved profile, without upgrading or modifying its original bytes."""
    from .profiles import relative_path
    from .colony import require_profile
    path = resolve(store.metadata, relative_path(mouse_id))
    if not path.exists():
        return None
    raw = path.read_bytes()
    profile = loads(raw)
    schema, _ = store._archived_schema(profile.get('schema_version'))
    require_profile(profile, schema)
    if profile['mouse_id'] != mouse_id or profile['revision'] < 1:
        raise LoggerError('The saved colony profile does not match this mouse')
    return {'profile_id':profile['profile_id'],'mouse_id':mouse_id,'revision':profile['revision'],
            'source_sha256':digest(raw),'snapshot':profile}


def validate_link(store, record):
    profile = record['metadata'].get('colony_profile')
    if profile:
        from .colony import require_profile
        schema, _ = store._archived_schema(profile['snapshot']['schema_version'])
        require_profile(profile['snapshot'], schema)
        # Recovery uses the captured revision, even if the editable profile changed.
        errors = _profile_errors(record)
        if errors:
            raise LoggerError('\n'.join(errors))
    ref = record['metadata']['linked_record']
    if not ref:
        if not profile:
            raise LoggerError('Save a Mouse colony profile for this mouse, or select an existing experiment')
        return
    index = store.index()
    info = index.get(ref['record_id'],{})
    if info.get('integrity')!='ok' or info.get('sha256')!=ref['sha256']:
        raise LoggerError('Linked experiment changed or is missing; refresh and select it again')
    target = store.read_record(ref['record_id'],index=index)
    if target['record_type'] not in ('data','session') or target.get('mouse_id')!=record['mouse_id'] or target.get('data_type')=='histology':
        raise LoggerError('The linked experiment must belong to the same mouse')


def _profile_errors(record):
    profile = record['metadata'].get('colony_profile')
    if not profile:
        return []
    snapshot = profile['snapshot']
    if (profile['mouse_id'] != record['mouse_id'] or
            any(profile[k] != snapshot[k] for k in ('profile_id','mouse_id','revision'))):
        return ['metadata.colony_profile: snapshot identity/revision does not match this mouse']
    return []


def record_errors(record, final):
    m, errors = record['metadata'], _profile_errors(record)
    if m['linked_record']:
        if record['related_records'] != [m['linked_record']]:
            errors.append('metadata.linked_record: inconsistent experiment reference')
    elif record['related_records']:
        errors.append('metadata.linked_record: missing referenced experiment')
    if final:
        if not m['reviewed'] or not m['slice_index']:
            errors.append('metadata: review the identity/settings and enter a slice index')
        if not m['linked_record'] and not m.get('colony_profile'):
            errors.append('metadata: a saved colony profile or an existing experiment is required')
        files = {f['file_id']:f for f in record['files']}
        expected = []
        for image in m['images']:
            expected.append(image['file_id'])
            if files.get(image['file_id'],{}).get('role')!='confocal_czi':
                errors.append('files: unresolved CZI file reference')
            if m['export_tiff']:
                expected.append(image['tiff_file_id'])
                if (not image['tiff_export'] or not image['tiff_file_id'] or
                        files.get(image['tiff_file_id'],{}).get('role')!='histology_tiff'):
                    errors.append('files: requested TIFF export is incomplete')
            elif image['tiff_file_id'] or image['tiff_export']:
                errors.append('metadata.images: unexpected TIFF export')
        if set(expected)!=set(files) or len(expected)!=len(set(expected)):
            errors.append('files: every CZI/TIFF must be referenced exactly once')
    return errors


def _journal(store, rid):
    return store._path(rid,store.local/'histology-transfers')


def _prepare(store, draft):
    r = deepcopy(draft['record'])
    require_valid(r)
    m = r['metadata']
    if r.get('data_type')!='histology' or r['record_state']!='draft':
        raise LoggerError('Only histology drafts can use this operation')
    if not all(r.get(k) and str(r[k]).strip() for k in ('mouse_id','session_date','operator')):
        raise LoggerError('Mouse, date, and operator are required')
    mouse_folder(r['mouse_id']); day_folder(r['session_date'])
    if not m['reviewed'] or not m['slice_index'] or not m['images']:
        raise LoggerError('Review the identity and metadata, and enter a slice index')
    if m['export_tiff']:
        for image in m['images']:
            export_spec(image['czi'])
    saved = store._path(r['record_id'],store.local/'drafts')
    if saved.exists() and read_json(saved)['revision']!=draft['revision']:
        raise LoggerError('Stale draft; reopen before moving')
    fingerprint = digest(encode({'record':r,'inputs':draft['inputs']}))
    journal = _journal(store,r['record_id'])
    if journal.exists():
        prior = read_json(journal)
        if prior['fingerprint']==fingerprint:
            if prior['phase']=='complete':
                raise LoggerError('Duplicate histology finalization')
            return journal,prior
        if prior['authorized']:
            raise LoggerError('Transfer choices changed after starting; use Recover')
    profile = colony_reference(store,r['mouse_id'])
    if m.get('colony_profile') and m['colony_profile'] != profile:
        raise LoggerError('Colony profile changed; use Find experiments to refresh it and review again')
    m['colony_profile'] = profile
    validate_link(store,r)
    index = store.index()
    if r['record_id'] in index or r['session_id'] in index:
        raise LoggerError('Duplicate record/session identity')
    inputs = draft['inputs']
    if (not inputs or len({i['source'] for i in inputs})!=len(inputs)
            or len({i['file_id'] for i in inputs})!=len(inputs)
            or {i['file_id'] for i in inputs}!={i['file_id'] for i in m['images']}):
        raise LoggerError('CZI selections and metadata do not agree')
    for item in inputs:
        if (not Path(item['source']).is_absolute() or Path(item['source']).suffix.lower()!='.czi'
                or list(signature(item['source']))!=item['source_signature']):
            raise LoggerError('CZI changed since inspection; drop it again and review the metadata')
    layout = store._reserve_layout(r)
    label = f"{r['mouse_id']}-{m['slice_index']}"
    base = f"histology/{mouse_folder(r['mouse_id'])}/{day_folder(r['session_date'])}/{label}/{layout['number']}"
    items, exports, names = [], [], set()
    for item in inputs:
        name, suffix = Path(item['source']).name, 1
        while name.casefold() in names or resolve(store.roots['archive'],f'{base}/czi/{name}').exists():
            suffix+=1
            p = Path(item['source'])
            name=f'{p.stem}__{suffix}{p.suffix}'
        names.add(name.casefold())
        items.append({**item,'side':'confocal_czi','relative_path':f'{base}/czi/{name}',
            'format':'czi','media_type':'application/octet-stream','phase':'planned','mode':None,
            'hashes':None,'source_removed':False})
        if m['export_tiff']:
            tiff=f'{base}/tif/{Path(name).stem}.ome.tif'
            if resolve(store.roots['archive'],tiff).exists():
                raise LoggerError('TIFF destination already exists; choose a new acquisition')
            exports.append({'file_id':uid(),'source_file_id':item['file_id'],'relative_path':tiff,
                            'phase':'planned','hashes':None,'provenance':None})
    plan={'record':deepcopy(r),'fingerprint':fingerprint,'items':items,'exports':exports,
          'archive':store.roots['archive'],'metadata':str(store.metadata),'layout':layout,
          'amendment_id':uid() if m['linked_record'] else None,
          'authorized':False,'phase':'planned','created_at':now()}
    atomic_json(journal,plan)
    return journal,plan


def plan_histology(store,draft):
    store._check_ready()
    with lock(store.metadata):
        return deepcopy(_prepare(store,draft)[1])


def finalize_histology(store,draft,*,move_confirmed=False,source_complete=False,progress=None,cancel=None,failpoint=None):
    if not move_confirmed or not source_complete:
        raise LoggerError('Confirm acquisition is finished and authorize moving the CZI files')
    store._check_ready()
    with lock(store.metadata):
        journal,plan=_prepare(store,draft)
        plan['authorized']=True
        atomic_json(journal,plan)
        return finish_transfers(store,journal,plan,progress=progress,cancel=cancel,failpoint=failpoint,fresh=True)


def prepare_exports(store,journal,plan,progress,cancel):
    """Only a verified export can enter the catalog and combined publication."""
    def save(): atomic_json(journal,plan)
    for export in plan['exports']:
        if export['phase']=='verified':
            continue
        source=next(i for i in plan['items'] if i['file_id']==export['source_file_id'])
        raw=resolve(store.roots['archive'],source['relative_path'])
        image=next(i for i in plan['record']['metadata']['images'] if i['file_id']==source['file_id'])
        target=resolve(store.roots['archive'],export['relative_path'])
        partial=target.with_name(f".{target.name}.{export['file_id']}.partial")
        if export['phase']=='planned':
            if target.exists():
                raise LoggerError('TIFF destination conflict; nothing overwritten')
            partial.unlink(missing_ok=True)
            try:
                phase(progress,'Exporting stitched OME-TIFF: '+raw.name,True)
                provenance=export_stitched(raw,partial,image['czi'],progress=progress,cancel=cancel)
                with partial.open('r+b') as f:
                    f.flush(); os.fsync(f.fileno())
                phase(progress,'Hashing verified TIFF export',True)
                hashes=hash_file(partial,progress,cancel)
                provenance.update(source_file_id=source['file_id'],source_sha256=source['hashes']['sha256'])
                export.update(phase='created',hashes=hashes,provenance=provenance,created_signature=list(signature(partial)))
                save()
            except Exception:
                if export['phase']=='planned':
                    partial.unlink(missing_ok=True)
                raise
        if target.exists():
            if (partial.exists() or _identity(signature(target))!=_identity(export['created_signature'])
                    or hash_file(target,progress,cancel)!=export['hashes']):
                raise LoggerError('TIFF publication conflict; nothing overwritten')
        else:
            if list(signature(partial))!=export['created_signature']:
                raise LoggerError('Verified TIFF partial changed')
            rename_path(partial,target)
        if not any(i['file_id']==export['file_id'] for i in plan['items']):
            plan['items'].append({'file_id':export['file_id'],'side':'histology_tiff','source':str(target),
                'relative_path':export['relative_path'],'format':'ome-tiff','media_type':'image/tiff',
                'source_signature':list(signature(target)),'target_signature':list(signature(target)),
                'phase':'verified','mode':'generated','hashes':export['hashes'],'source_removed':True})
        image.update(tiff_file_id=export['file_id'],tiff_export=export['provenance'])
        export['phase']='verified'
        save()


def linking_amendment(record, amendment_id):
    ref=record['metadata']['linked_record']
    amendment=envelope('amendment')
    checksum=digest(encode(record))
    amendment.update(record_id=amendment_id,operator=record['operator'],original=deepcopy(ref),
        reason=f"Register histology for slice {record['metadata']['slice_index']} acquired {record['session_date']}",
        related_records=[{'record_id':record['record_id'],'sha256':checksum}],
        corrected_values={'additional_histology':{'record_id':record['record_id'],'sha256':checksum,
            'mouse_id':record['mouse_id'],'slice_index':record['metadata']['slice_index'],
            'acquisition_date':record['session_date'],'comments':record['notes'],
            'file_ids':[f['file_id'] for f in record['files']]}})
    return seal_record(amendment,record['operator'])


def pending_histology(store):
    plans=[read_json(p) for p in sorted((store.local/'histology-transfers').glob('*.json'))]
    return [p for p in plans if p['authorized'] and p['phase']!='complete']


def recover_histology_locked(store,progress=None,cancel=None):
    return [finish_transfers(store,_journal(store,p['record']['record_id']),p,
                            progress=progress,cancel=cancel) for p in pending_histology(store)]
