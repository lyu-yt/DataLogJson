"""In-vivo registrations and resumable moves. No movie decoding or analysis."""
from copy import deepcopy
from pathlib import Path
import os

from .expression import inspect_tiff, new_expression
from .io import (LoggerError, atomic_json, digest, encode,
                 lock, now, read_json, resolve, signature, uid)
from .layout import day_folder, mouse_folder
from .models import require_valid
from .transfers import finish_transfers as _finish_transfers

SIDES = ('left', 'right')


def new_imaging(folder):
    d = new_expression(folder)
    r = d['record']
    r.update(data_type='imaging', experiment_type='in_vivo_imaging', metadata={
        'behavior_session': None, 'behavior': None,
        'sides': {s: {'laser_power': None, 'channels': {'red': None, 'green': None}} for s in SIDES}})
    paths = {s: sorted(str(p.resolve()) for p in Path(folder).iterdir()
                      if p.is_file() and p.suffix.lower() in ('.tif', '.tiff')
                      and p.name.lower().startswith(s + '_')) for s in SIDES}
    return {'revision': 0, 'record': r, 'source_folder': d['source_folder'],
            'side_paths': {s: paths[s][0] if len(paths[s]) == 1 else None for s in SIDES},
            'detected_paths': paths}


def _settings_errors(record, present, final):
    m = record['metadata']
    errors = []
    if final and not isinstance(m['behavior_session'], bool):
        errors.append('metadata.behavior_session: choose Yes or No')
    if m['behavior_session'] is True and (m['behavior'] or final) and (not m['behavior'] or record['related_records'] !=
            [{k: m['behavior'][k] for k in ('record_id', 'sha256')}]):
        errors.append('metadata.behavior: choose a registered robot CSV session')
    if m['behavior_session'] is not True and (m['behavior'] or record['related_records']):
        errors.append('metadata.behavior: clear the link when behavior is not Yes')
    for side in SIDES:
        settings = m['sides'][side]
        colors = settings['channels']
        if final and side in present:
            if any(not isinstance(v, bool) for v in colors.values()):
                errors.append(f'metadata.sides.{side}.channels: specify red and green presence')
            elif not any(colors.values()):
                errors.append(f'metadata.sides.{side}.channels: select at least one saved color')
        if final and side not in present and (any(v is True for v in colors.values()) or settings['laser_power']):
            errors.append(f'metadata.sides.{side}: settings need a TIFF for this side')
    return errors


def record_errors(record, final):
    roles = [f['role'] for f in record['files']]
    errors = _settings_errors(record, set(roles), final)
    if len(roles) != len(set(roles)):
        errors.append('files: at most one TIFF per side; log another acquisition separately')
    return errors


def behavior_candidates(store, mouse_id, session_date):
    """One scan per explicit lookup; callers select an ambiguous session."""
    mouse_folder(mouse_id)
    day_folder(session_date)
    index, catalog = store.index(), store._catalog()
    result = []
    for info in index.values():
        if (info['integrity'] != 'ok' or info.get('data_type') != 'task'
                or info.get('record_type') != 'data' or info['mouse_id'] != mouse_id
                or info['session_date'] != session_date):
            continue
        r = store.read_record(info['record_id'], index=index)
        f = r['files'][0]
        locations = store.file_locations(f['file_id'], catalog=catalog)
        result.append({'label': f"{r['experiment_type']} Â· day {r['metadata']['training_day']} Â· {f['original_filename']} Â· {r['record_id'][:8]}",
            'path': next((p['path'] for p in locations if p['available']), '(CSV missing; use Locations)'),
            'reference': {'record_id': r['record_id'], 'sha256': info['sha256'],
                          'session_id': r['session_id'], 'csv_file_id': f['file_id'], 'csv_sha256': f['sha256']}})
    return result


def _validate_behavior(store, record):
    ref = record['metadata']['behavior']
    if record['metadata']['behavior_session'] is not True:
        return
    index = store.index()
    info = index.get(ref['record_id'], {})
    if info.get('sha256') != ref['sha256'] or info.get('integrity') != 'ok':
        raise LoggerError('Linked behavior record changed or is missing')
    task = store.read_record(ref['record_id'], index=index)
    if (task.get('data_type') != 'task' or task.get('record_type') != 'data'
            or any(task[k] != record[k] for k in ('mouse_id', 'session_date'))
            or task['session_id'] != ref['session_id']):
        raise LoggerError('Behavior link must be a task dataset for the same mouse/date')
    files = [f for f in task['files'] if f['file_id'] == ref['csv_file_id'] and f['sha256'] == ref['csv_sha256']]
    catalog = store._catalog()['files'].get(ref['csv_file_id'], {})
    if len(files) != 1 or any(catalog.get(k) != files[0][k] for k in ('sha256', 'size_bytes')):
        raise LoggerError('Linked robot CSV identity is missing or inconsistent')


def _journal_path(store, rid):
    return store._path(rid, store.local / 'imaging-transfers')


def _prepare(store, draft):
    r = draft['record']
    require_valid(r)
    if r.get('data_type') != 'imaging' or r['record_state'] != 'draft':
        raise LoggerError('Only imaging drafts can be finalized')
    for key in ('mouse_id', 'session_date', 'operator'):
        if not r.get(key) or not str(r[key]).strip():
            raise LoggerError(f'{key}: required')
    mouse_folder(r['mouse_id'])
    day_folder(r['session_date'])
    paths = {s: str(Path(p).expanduser().resolve()) for s, p in draft['side_paths'].items() if p and s in SIDES}
    if not paths or len(set(os.path.normcase(p) for p in paths.values())) != len(paths):
        raise LoggerError('Choose one or two different TIFFs, one per side')
    if any(not Path(p).is_absolute() for p in draft['side_paths'].values() if p):
        raise LoggerError('TIFF paths must be absolute')
    errors = _settings_errors(r, set(paths), True)
    if errors:
        raise LoggerError('\n'.join(errors))
    _validate_behavior(store, r)
    saved = store._path(r['record_id'], store.local / 'drafts')
    if saved.exists() and read_json(saved)['revision'] != draft['revision']:
        raise LoggerError('Stale draft; reload before finalizing')
    fingerprint = digest(encode({'record': r, 'paths': paths}))
    journal = _journal_path(store, r['record_id'])
    if journal.exists():
        prior = read_json(journal)
        if prior['fingerprint'] == fingerprint:
            if prior['phase'] == 'complete':
                raise LoggerError('Duplicate finalization; imaging registration already completed')
            return journal, prior
        if prior['authorized']:
            raise LoggerError('Transfer choices changed after starting. Restore the draft choices or use Recover.')
    index = store.index()
    if r['record_id'] in index or r['session_id'] in index:
        raise LoggerError('Duplicate imaging record or session identity')
    items = []
    for side, source in paths.items():
        inspect_tiff(source)
        base = f"imaging/{mouse_folder(r['mouse_id'])}/{day_folder(r['session_date'])}/{side}"
        parent = resolve(store.roots['archive'], base)
        names = {p.name.casefold() for p in parent.iterdir()} if parent.exists() else set()
        name, number = Path(source).name, 1
        while name.casefold() in names:
            number += 1
            name = f'{Path(source).stem}__{number}{Path(source).suffix}'
        items.append({'side': side, 'source': source, 'relative_path': f'{base}/{name}',
            'file_id': uid(), 'source_signature': list(signature(source)), 'phase': 'planned',
            'mode': None, 'hashes': None, 'source_removed': False})
    plan = {'record': deepcopy(r), 'fingerprint': fingerprint, 'items': items,
            'archive': store.roots['archive'], 'metadata': str(store.metadata),
            'layout': store._reserve_layout(r), 'authorized': False, 'phase': 'planned', 'created_at': now()}
    atomic_json(journal, plan)
    return journal, plan


def plan_imaging(store, draft):
    store._check_ready()
    with lock(store.metadata):
        return deepcopy(_prepare(store, draft)[1])


def finalize_imaging(store, draft, *, source_complete=False, move_confirmed=False,
                     progress=None, cancel=None, failpoint=None):
    if not source_complete or not move_confirmed:
        raise LoggerError('Confirm acquisition is finished and authorize moving the imaging files')
    store._check_ready()
    with lock(store.metadata):
        journal, plan = _prepare(store, draft)
        plan['authorized'] = True
        atomic_json(journal, plan)
        return _finish_transfers(store, journal, plan, progress=progress, cancel=cancel, failpoint=failpoint, fresh=True)


def pending_imaging(store):
    return [read_json(p) for p in sorted((store.local / 'imaging-transfers').glob('*.json'))
            if read_json(p)['authorized'] and read_json(p)['phase'] != 'complete']


def recover_imaging_locked(store, progress=None, cancel=None):
    results = []
    for plan in pending_imaging(store):
        journal = _journal_path(store, plan['record']['record_id'])
        results.append(_finish_transfers(store, journal, plan, progress=progress, cancel=cancel))
    return results
