"""Import verified offline BIF vectors into the existing live index, without inference."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import numpy as np
from qdrant_client import models

from . import bif
from .indexer import file_id, frame_id
from .model import digest
from .paths import canonical_mtime


def load_export(export: Path, fingerprint: str):
    report = json.loads((export / 'verification.json').read_text(encoding='utf-8'))
    items = json.loads((export / 'input.json').read_text(encoding='utf-8'))
    if (report.get('verified') is not True or report.get('failed_frames') != 0
            or report.get('model_fingerprint') != fingerprint
            or report.get('files') != len(items)
            or report.get('valid_frames') != sum(item['frames'] for item in items)):
        raise ValueError('Export verification/model/count mismatch')
    seen, results = set(), []
    for item in items:
        if (len(item['id']) != 64 or any(c not in '0123456789abcdef' for c in item['id'])
                or (item['source'], item['relpath']) in seen):
            raise ValueError('Invalid/duplicate export identity')
        seen.add((item['source'], item['relpath']))
        result = json.loads((export / 'output' / item['id'] / 'complete.json').read_text(encoding='utf-8'))
        if result['input'] != item or result['model_fingerprint'] != fingerprint or result['total_frames'] != item['frames']:
            raise ValueError('BIF result identity mismatch')
        cursor = 0
        for block in result['blocks']:
            if (block['start'] != cursor or not cursor < block['end'] <= item['frames']
                    or block['valid'] != block['end']-cursor or block['errors']
                    or block['file'] != f'{cursor:08d}.npz'):
                raise ValueError('Incomplete/invalid export block coverage')
            cursor = block['end']
        if cursor != item['frames']:
            raise ValueError('Incomplete BIF export')
        results.append(result)
    return report, items, results


def version_id(ident, checksum, fingerprint):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'{ident}:{checksum}:{fingerprint}'))


def import_file(runtime, export: Path, result: dict) -> dict:
    item = result['input']
    indexer = runtime.get_indexer()
    db, store = runtime.db, runtime.get_store()
    fingerprint = runtime.settings.manifest['fingerprint']
    if result['model_fingerprint'] != fingerprint:
        raise ValueError('Model fingerprint mismatch')
    path = indexer.path(item['source'], item['relpath'])
    stamp = path.stat()
    if stamp.st_size != item['size'] or digest(path) != item['sha256']:
        raise ValueError('Local BIF differs from exported input')
    parsed = bif.parse(path)
    if len(parsed) != item['frames']:
        raise ValueError('Local BIF frame count mismatch')
    indexer.verify_stamp(path, dict(size=stamp.st_size, mtime_ns=canonical_mtime(stamp)))
    ident = file_id(item['source'], item['relpath'])
    version = version_id(ident, item['sha256'], fingerprint)
    selector = models.Filter(must=[models.FieldCondition(key='version', match=models.MatchValue(value=version))])

    # Validate outside the shared writer lock, so the local encoder can continue.
    for block in result['blocks']:
        saved_path = export / 'output' / item['id'] / block['file']
        if digest(saved_path) != block['sha256']:
            raise ValueError('Export block checksum mismatch')
        with np.load(saved_path, allow_pickle=False) as saved:
            numbers, values = saved['frames'], saved['vectors']
            expected = np.arange(block['start'], block['end'], dtype=np.int32)
            if not np.array_equal(numbers, expected) or values.dtype != np.float32 or values.shape != (len(expected), 1024):
                raise ValueError('Invalid exported vector/frame array')
            if not np.isfinite(values).all() or not np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-4):
                raise ValueError('Invalid exported embedding values')
            for key, attribute in (('times_ms', 'time_ms'), ('offsets', 'offset'), ('lengths', 'length')):
                expected_values = np.array([getattr(parsed[int(number)], attribute) for number in numbers])
                if not np.array_equal(saved[key], expected_values):
                    raise ValueError('Exported frame metadata differs from BIF')

    with indexer.operation_lock:
        indexer.verify_stamp(path, dict(size=stamp.st_size, mtime_ns=canonical_mtime(stamp)))
        old = db.one('SELECT * FROM files WHERE id=?', (ident,))
        existing = db.one('SELECT * FROM versions WHERE id=?', (version,))
        if old and old['active_version'] == version and existing and existing['status'] == 'ready':
            if store.client.count(store.collection, count_filter=selector, exact=True).count != len(parsed):
                raise RuntimeError('Already-published version has incomplete vector points')
            with db.connect() as c:
                c.execute('UPDATE files SET size=?,mtime_ns=? WHERE id=?', (stamp.st_size, canonical_mtime(stamp), ident))
                c.execute('UPDATE versions SET size=?,mtime_ns=? WHERE id=?', (stamp.st_size, canonical_mtime(stamp), version))
            return dict(file_id=ident, version=version, frames=len(parsed), reused=True)
        now = time.time()
        with db.connect() as c:
            c.execute('''INSERT INTO files(id,source,relpath,size,mtime_ns,stable_since,seen,desired_version,status)
                VALUES(?,?,?,?,?,?,?,?,'queued') ON CONFLICT(id) DO UPDATE SET
                size=excluded.size,mtime_ns=excluded.mtime_ns,desired_version=excluded.desired_version,
                active_version=NULL,status='queued',error=NULL''',
                (ident, item['source'], item['relpath'], stamp.st_size, canonical_mtime(stamp), now,
                 'offline-import', version))
            c.execute('''INSERT OR IGNORE INTO versions(id,file_id,sha256,size,mtime_ns,total,created)
                VALUES(?,?,?,?,?,?,?)''', (version, ident, item['sha256'], stamp.st_size, canonical_mtime(stamp), len(parsed), now))
            c.execute("UPDATE versions SET size=?,mtime_ns=?,total=?,status='processing',cursor=0,error=NULL WHERE id=?",
                      (stamp.st_size, canonical_mtime(stamp), len(parsed), version))
            c.executemany('''INSERT INTO frames(id,version,frame_no,time_ms,offset,length)
                VALUES(?,?,?,?,?,?) ON CONFLICT(version,frame_no) DO UPDATE SET
                time_ms=excluded.time_ms,offset=excluded.offset,length=excluded.length,valid=1,error=NULL''',
                [(frame_id(version, fr.index), version, fr.index, fr.time_ms, fr.offset, fr.length) for fr in parsed])
            # Replace any partial chunks from the original local scan/build.
            c.execute('DELETE FROM chunks WHERE version=?', (version,))
            c.execute('DELETE FROM deletions WHERE version=?', (version,))
        try:
            folder = runtime.settings.data / 'vectors' / version
            folder.mkdir(exist_ok=True)
            for block in result['blocks']:
                source = export / 'output' / item['id'] / block['file']
                dest = folder / block['file']
                temp = dest.with_suffix('.import.tmp')
                with source.open('rb') as inp, temp.open('wb') as out:
                    shutil.copyfileobj(inp, out, length=1024*1024)
                    out.flush()
                    os.fsync(out.fileno())
                if digest(temp) != block['sha256']:
                    raise ValueError('Export changed during copy')
                os.replace(temp, dest)
                with db.connect() as c:
                    c.execute('INSERT INTO chunks(version,start,end,path,sha256,uploaded) VALUES(?,?,?,?,?,0)',
                              (version, block['start'], block['end'], dest.relative_to(runtime.settings.data).as_posix(), block['sha256']))
                    c.execute('UPDATE versions SET cursor=? WHERE id=?', (block['end'], version))
                indexer.flush_chunk(db.one('SELECT * FROM chunks WHERE version=? AND start=?', (version, block['start'])))
            indexer.verify_stamp(path, dict(size=stamp.st_size, mtime_ns=canonical_mtime(stamp)))
            count = store.client.count(store.collection, count_filter=selector, exact=True).count
            if count != len(parsed):
                raise RuntimeError(f'Imported vector count mismatch: {count} != {len(parsed)}')
            with db.connect() as c:
                if old:
                    for superseded in {old['active_version'], old['desired_version']} - {None, version}:
                        c.execute('INSERT OR IGNORE INTO deletions VALUES(?,?)', (superseded, time.time()))
                c.execute("UPDATE versions SET status='ready',cursor=total,attempts=0,retry_at=0,error=NULL WHERE id=?", (version,))
                c.execute("UPDATE files SET active_version=?,desired_version=?,status='ready',error=NULL WHERE id=?", (version, version, ident))
            return dict(file_id=ident, version=version, frames=len(parsed), reused=False)
        except Exception as error:
            with db.connect() as c:
                c.execute("UPDATE versions SET status='failed',error=? WHERE id=?", (str(error)[:2000], version))
                c.execute('UPDATE files SET error=? WHERE id=?', (str(error)[:2000], ident))
            raise


def import_export(runtime, export: Path, output: Path):
    manifest = runtime.settings.manifest
    report, items, results = load_export(export, manifest['fingerprint'])
    output.mkdir(parents=True, exist_ok=True)
    indexer = runtime.get_indexer()
    indexer.operation_lock.check_interval = 0.01
    with indexer.operation_lock:
        backup = output / 'metadata-before.sqlite3'
        if not backup.exists():
            runtime.db.backup(backup)
        if (output / 'before.json').exists():
            before = json.loads((output / 'before.json').read_text())
            unrelated = json.loads((output / 'published-before.json').read_text())
        else:
            before = runtime.db.stats()
            before.pop('events')
            (output / 'before.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
            unrelated = runtime.db.rows('SELECT id,active_version FROM files WHERE active_version IS NOT NULL')
            (output / 'published-before.json').write_text(json.dumps(unrelated), encoding='utf-8')
    records, frames, reused = [], 0, 0
    started = time.time()
    with (output / 'progress.jsonl').open('a', encoding='utf-8') as progress:
        for number, result in enumerate(results, 1):
            record = import_file(runtime, export, result)
            records.append(record)
            frames += record['frames']
            reused += int(record['reused'])
            row = {**record, 'completed_files': number, 'total_files': len(items), 'frames': frames,
                   'reused_files': reused, 'elapsed_seconds': round(time.time()-started, 2)}
            progress.write(json.dumps(row) + '\n')
            progress.flush()
            if number % 25 == 0 or number == len(items):
                print(json.dumps(row), flush=True)
            time.sleep(0.01)  # Give the existing local encoder a chance to acquire its writer lock.
    selected = {record['file_id'] for record in records}
    for previous in unrelated:
        if previous['id'] in selected:
            continue
        current = runtime.db.one('SELECT active_version FROM files WHERE id=?', (previous['id'],))
        if not current or current['active_version'] != previous['active_version']:
            raise RuntimeError('Previously published unrelated version changed during import')
    after = runtime.db.stats()
    after.pop('events')
    result = dict(imported_files=len(items), imported_frames=frames, reused_files=reused,
                  model_fingerprint=manifest['fingerprint'], before=before, after=after,
                  unrelated_ready_files_preserved=len(unrelated)-sum(r['id'] in selected for r in unrelated),
                  collection=runtime.get_store().collection, qdrant_points=runtime.get_store().count(),
                  elapsed_seconds=round(time.time()-started, 2), complete=True)
    (output / 'imported-versions.json').write_text(json.dumps(records), encoding='utf-8')
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    runtime.db.event('embedding_import', json.dumps({k: result[k] for k in ('imported_files', 'imported_frames', 'collection')}))
    print(json.dumps(result), flush=True)
    return result


def main():
    from dotenv import load_dotenv
    from .config import Settings
    from .console import configure_console
    from .search import Runtime
    configure_console()
    load_dotenv('.env')
    parser = argparse.ArgumentParser()
    parser.add_argument('export', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--grpc-port', type=int)
    args = parser.parse_args()
    settings = Settings()
    if args.grpc_port:
        from .vectors import VectorStore
        store = VectorStore(settings, settings.manifest['fingerprint'], grpc_port=args.grpc_port)
        store.ensure(apply_mode=False)
        runtime = Runtime(settings, store=store)
    else:
        runtime = Runtime(settings)
    try:
        import_export(runtime, args.export.resolve(), args.output.resolve())
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
