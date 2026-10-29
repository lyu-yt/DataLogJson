"""Shared journaled file moves for task, expression and imaging data."""
from copy import deepcopy
from pathlib import Path
import errno
import hashlib
import os

from .io import (LoggerError, Cancelled, atomic_json, digest, encode, hash_file,
                 lock, now, read_json, rename_path, resolve, signature, uid)
from .models import require_valid, seal_record, session_for, inspect_csv


def phase(progress, text, measurable=False):
    callback = getattr(progress, 'phase', None)
    if callback:
        callback(text, measurable)


class TransferProgress:
    """Count actual read/copy passes, including verification, across all files."""
    def __init__(self, callback, items):
        self.callback = callback
        self.passes = {i['file_id']: 2 if i['phase'] in ('planned', 'renaming', 'copying')
                       else 1 if i['phase'] == 'moved' else 0 for i in items}
        self.total = sum(self.passes[i['file_id']] * i['source_signature'][0] for i in items)
        self.completed = 0

    def passes_for(self, item, count):
        self.total += (count - self.passes[item['file_id']]) * item['source_signature'][0]
        self.passes[item['file_id']] = count

    def begin(self, item, label):
        phase(self.callback, label + ': ' + Path(item['source']).name, True)
        def report(n, _total):
            if self.callback:
                self.callback(self.completed + min(n, item['source_signature'][0]), max(1, self.total))
        return report

    def end(self, item):
        self.completed += item['source_signature'][0]
        self.passes[item['file_id']] -= 1
        if self.callback:
            self.callback(self.completed, max(1, self.total))

    def hash(self, item, path, label, cancel):
        result = hash_file(path, self.begin(item, label), cancel)
        self.end(item)
        return result


def _journal_path(store, rid):
    return store._path(rid, store.local / 'file-transfers')


def finalize_move(store, draft, *, source_complete=False, progress=None, cancel=None, failpoint=None):
    """Explicit move request for task or expression; copy-only uses existing APIs."""
    if not source_complete:
        raise LoggerError('Confirm acquisition is finished and source files are closed')
    store._check_ready()
    r = draft['record']
    require_valid(r)
    if r['record_state'] != 'draft' or r.get('data_type') not in ('task', 'expression'):
        raise LoggerError('Only task and expression drafts can use this transfer')
    paths = draft['channel_paths'] if r['data_type'] == 'expression' else {'task_events': draft['source_path']}
    fingerprint = digest(encode({'record': r, 'paths': paths}))
    journal = _journal_path(store, r['record_id'])
    with lock(store.metadata):
        saved = store._path(r['record_id'], store.local / 'drafts')
        if saved.exists() and read_json(saved)['revision'] != draft['revision']:
            raise LoggerError('Stale draft; reload before finalizing')
        if journal.exists():
            plan = read_json(journal)
            if plan['fingerprint'] != fingerprint:
                raise LoggerError('Transfer choices changed after starting; use Recover or restore the original draft')
            if plan['phase'] == 'complete':
                raise LoggerError('Duplicate finalization; transfer already completed')
        else:
            if any(r['record_id'] in op['record_ids'] for op in store.pending()):
                raise LoggerError('This acquisition has a pending metadata operation. Use Recover before starting another transfer.')
            index = store.index()
            if r['record_id'] in index or r['session_id'] in index:
                raise LoggerError('Duplicate finalization; record or session already exists')
            if r['data_type'] == 'expression':
                from .expression import _prepare
                _, old_plan = _prepare(store, draft)
                layout = old_plan['layout']
                items = [{**i, 'side': i['channel'], 'phase': 'planned', 'mode': None,
                          'source_removed': False} for i in old_plan['items']]
                for item in items:
                    if item['hashes'] and resolve(store.roots['archive'], item['relative_path']).exists():
                        item.update(phase='copying', mode='verified_copy')
            else:
                from .models import validate
                errors = [e for e in validate(r, True) if not e.startswith('files:')]
                if errors:
                    raise LoggerError('\n'.join(errors))
                source = Path(draft['source_path']).resolve()
                before = signature(source)
                phase(progress, 'Inspecting behavior CSV — estimating time…')
                mapping, _ = inspect_csv(source, cancel)
                if signature(source) != before:
                    raise LoggerError('Source changed during CSV inspection')
                layout = store._reserve_layout(r)
                relative, _ = store.destination(r, source, layout=layout)
                items = [{'side': 'task_events', 'source': str(source), 'relative_path': relative,
                    'file_id': uid(), 'source_signature': list(before), 'phase': 'planned',
                    'mode': None, 'hashes': None, 'source_removed': False, 'csv': mapping}]
            plan = {'record': deepcopy(r), 'fingerprint': fingerprint, 'items': items,
                    'archive': store.roots['archive'], 'metadata': str(store.metadata),
                    'layout': layout, 'authorized': True, 'phase': 'planned', 'created_at': now()}
            atomic_json(journal, plan)
        return finish_transfers(store, journal, plan, progress=progress, cancel=cancel, failpoint=failpoint, fresh=True)


def pending_transfers(store):
    result = []
    for path in sorted((store.local / 'file-transfers').glob('*.json')):
        plan = read_json(path)
        if plan['authorized'] and plan['phase'] != 'complete':
            result.append(plan)
    return result


def recover_transfers_locked(store, progress=None, cancel=None):
    return [finish_transfers(store, _journal_path(store, p['record']['record_id']), p,
                            progress=progress, cancel=cancel) for p in pending_transfers(store)]


def _validate_behavior(store, record):
    if record.get('data_type') == 'imaging':
        from .imaging import _validate_behavior as check
        check(store, record)


def _identity(sig):
    # A rename may change ctime; size, mtime and filesystem file identity persist.
    return sig[0], sig[1], sig[3], sig[4]


def _transfer_item(store, item, save, meter, cancel, failpoint):
    source = Path(item['source'])
    target = resolve(store.roots['archive'], item['relative_path'])
    original = tuple(item['source_signature'])
    def stop():
        if cancel and cancel():
            raise Cancelled('File transfer cancelled. Already moved files stay in the archive; use Recover to resume.')
    stop()
    if item['phase'] == 'planned':
        if signature(source) != original:
            raise LoggerError('Source changed since planning; transfer blocked')
        item.update(mode='direct_move', phase='renaming')
        save()
    if item['phase'] == 'renaming':
        phase(meter.callback, 'Attempting direct move: ' + source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            if signature(source) != original:
                raise LoggerError('Source changed before move')
            try:
                rename_path(source, target)
            except OSError as exc:
                if exc.errno != errno.EXDEV and getattr(exc, 'winerror', None) != 17:
                    raise
                item.update(mode='verified_copy', phase='copying')
                save()
        elif not target.exists() or _identity(signature(target)) != _identity(original):
            raise LoggerError('Interrupted move cannot be matched to the original file identity')
        if item['phase'] == 'renaming':
            meter.passes_for(item, 1)
            if failpoint == 'renamed':
                raise LoggerError('Injected interruption after direct rename')
            item.update(phase='moved', source_removed=True)
            save()
    if item['phase'] == 'copying':
        if signature(source) != original:
            raise LoggerError('Source changed before verified copy')
        if target.exists():
            hashes = meter.hash(item, source, 'Checking original', cancel)
            if meter.hash(item, target, 'Verifying archived copy', cancel) != hashes:
                raise LoggerError('Destination conflict; nothing overwritten')
        else:
            partial = target.with_name(f".{target.name}.{item['file_id']}.partial")
            try:
                # Only this journal's bounded, unpublished partial is discarded.
                partial.unlink(missing_ok=True)
                h, size = hashlib.sha256(), 0
                report = meter.begin(item, 'Copying')
                with source.open('rb') as inp, partial.open('xb') as out:
                    while chunk := inp.read(4 * 1024 * 1024):
                        stop()
                        out.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
                        if size > original[0]:
                            raise LoggerError('Source grew during copying')
                        report(size, original[0])
                    out.flush()
                    os.fsync(out.fileno())
                hashes = {'sha256': h.hexdigest(), 'size_bytes': size}
                if size != original[0] or signature(source) != original:
                    raise LoggerError('Source changed during copying; source retained')
                meter.end(item)
                if meter.hash(item, partial, 'Verifying archived copy', cancel) != hashes:
                    raise LoggerError('Source changed or copied bytes differ; source retained')
                rename_path(partial, target)
            finally:
                partial.unlink(missing_ok=True)
        if signature(source) != original:
            raise LoggerError('Source changed during copying; source retained')
        item.update(hashes=hashes, phase='verified', target_signature=list(signature(target)))
        save()
    if item['phase'] == 'moved':
        item.update(hashes=meter.hash(item, target, 'Verifying moved file', cancel), phase='verified', target_signature=list(signature(target)))
        save()
    if item['phase'] == 'verified' and signature(target) != tuple(item['target_signature']):
        meter.passes_for(item, 1)
        if meter.hash(item, target, 'Rechecking archived file', cancel) != item['hashes']:
            raise LoggerError('Archived file checksum mismatch')
        item['target_signature'] = list(signature(target))
        save()
    if failpoint == 'transferred':
        raise LoggerError('Injected interruption after verified transfer')


def finish_transfers(store, journal, plan, *, progress=None, cancel=None, failpoint=None, fresh=False):
    if plan['archive'] != store.roots['archive'] or plan['metadata'] != str(store.metadata):
        raise LoggerError('Restore the imaging transfer root configuration before recovery')
    save = lambda: atomic_json(journal, plan)
    r = deepcopy(plan['record'])
    _validate_behavior(store, r)
    meter = TransferProgress(progress, plan['items'])
    if plan['phase'] in ('publishing', 'published'):
        phase(progress, 'Saving metadata and Git history')
        operations = [read_json(p) for p in (store.metadata / '.operations').glob('*.json')]
        operation = next((op for op in operations if set(op['record_ids']) == {r['record_id'], r['session_id']}), None)
        if operation:
            plan['receipt'] = store._finish(operation)
            plan['phase'] = 'published'
            save()
    if plan['phase'] != 'published':
        for item in plan['items']:
            _transfer_item(store, item, save, meter, cancel, failpoint)
        if cancel and cancel():
            raise Cancelled('Cancelled before publication; use Recover to resume the moved files')
        entries = []
        r['files'] = []
        for item in plan['items']:
            target = resolve(store.roots['archive'], item['relative_path'])
            if signature(target) != tuple(item['target_signature']):
                raise LoggerError('Archived file changed before publication')
            registration = {'file_id': item['file_id'], 'role': item['side'],
                'original_filename': Path(item['source']).name, **item['hashes']}
            if r['data_type'] == 'task':
                registration.update(format='csv', media_type='text/tab-separated-values', csv=item['csv'])
            else:
                registration.update(format='tiff', media_type='image/tiff')
            r['files'].append(registration)
            entries.append({'file_id': item['file_id'], 'original_filename': Path(item['source']).name,
                **item['hashes'], 'locations': [{'root_id': 'archive', 'relative_path': item['relative_path'],
                'verified_at': now(), 'verification_status': 'verified'}]})
        if r['data_type'] == 'expression':
            for key, channel in r['metadata']['channels'].items():
                channel['file_ids'] = [f['file_id'] for f in r['files'] if f['role'] == key]
        r = seal_record(r, r['operator'])
        session = session_for(r, digest(encode(r)))
        plan['phase'] = 'publishing'
        save()
        phase(progress, 'Saving metadata and Git history')
        receipt = store._publish([r, session], entries, r['operator'], failpoint, plan['layout'])
        plan.update(receipt=receipt, phase='published')
        save()
    receipt = deepcopy(plan['receipt'])
    receipt['cleanup_pending'] = []
    if receipt['git_committed']:
        for item in plan['items']:
            if item['source_removed']:
                continue
            source = Path(item['source'])
            target = resolve(store.roots['archive'], item['relative_path'])
            try:
                if not source.exists():
                    if item.get('cleanup_state') == 'removing':
                        meter.passes_for(item, 1)
                        if meter.hash(item, target, 'Verifying completed source removal', cancel) == item['hashes']:
                            item['source_removed'] = True
                            save()
                            continue
                    raise LoggerError('Source disappeared outside this transfer; confirm manually')
                if signature(source) != tuple(item['source_signature']):
                    raise LoggerError('Source changed; its current contents will not be removed')
                if not fresh or signature(target) != tuple(item['target_signature']):
                    meter.passes_for(item, 2)
                    if (meter.hash(item, target, 'Rechecking archived copy', cancel) != item['hashes']
                            or meter.hash(item, source, 'Rechecking original before removal', cancel) != item['hashes']):
                        raise LoggerError('Cleanup checksum mismatch; source retained')
                if cancel and cancel():
                    raise Cancelled('Source removal cancelled')
                if signature(source) != tuple(item['source_signature']):
                    raise LoggerError('Source changed during cleanup')
                item['cleanup_state'] = 'removing'
                save()
                phase(progress, 'Removing verified original: ' + source.name)
                source.unlink()
                if failpoint == 'source_removed':
                    raise LoggerError('Injected interruption after source removal')
                item['source_removed'] = True
                save()
            except (OSError, LoggerError) as exc:
                receipt['cleanup_pending'].append(f"{item['side']}: {exc}")
    receipt['source_removed'] = all(i['source_removed'] for i in plan['items'])
    receipt['files'] = [{'side': i['side'], 'relative_path': i['relative_path'], 'method': i['mode'],
                         'source_removed': i['source_removed']} for i in plan['items']]
    if r['data_type'] == 'task':
        receipt['csv_relative_path'] = plan['items'][0]['relative_path']
    if r['data_type'] == 'expression':
        for entry in receipt['files']:
            entry['channel'] = entry['side']
    receipt['session_number'] = plan['layout']['number']
    plan['receipt'] = receipt
    if receipt['git_committed'] and receipt['source_removed']:
        plan['phase'] = 'complete'
    save()
    atomic_json(store.metadata / '.receipts' / f"{receipt['transaction_id']}.json", receipt)
    if progress:
        progress(1, 1)
    return receipt
