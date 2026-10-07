"""Remove embeddings only for CSV-listed, confirmed missing local BIF files."""
from __future__ import annotations

import csv
import json
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from qdrant_client import models


def prune_deleted(runtime, manifest: Path, destination: Path) -> dict:
    indexer = runtime.get_indexer()
    destination.mkdir(parents=True, exist_ok=False)
    with manifest.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    skipped = Counter()
    with indexer.operation_lock:
        runtime.db.backup(destination / 'metadata-before.sqlite3')
        files = {(f['source'], f['relpath']): f for f in runtime.db.rows('SELECT * FROM files')}
        versions = runtime.db.rows('SELECT * FROM versions')
        versions_by_file = {}
        for version in versions:
            versions_by_file.setdefault(version['file_id'], []).append(version)
        selected = {}
        for row in rows:
            if row['本地已删除'].lower() != 'true':
                skipped['not_confirmed_deleted'] += 1
                continue
            source, relative = row['磁盘'], row['BIF相对路径']
            if source not in runtime.settings.sources:
                skipped['unknown_source'] += 1
                continue
            if indexer.path(source, relative).exists():
                skipped['file_still_exists'] += 1
                continue
            file = files.get((source, relative))
            if not file:
                skipped['not_in_database'] += 1
                continue
            associated = versions_by_file.get(file['id'], [])
            current = next((v for v in associated if v['id'] == file['desired_version']), None)
            if current and row['SHA256'] and row['SHA256'] != current['sha256']:
                skipped['sha_mismatch'] += 1
                continue
            selected[file['id']] = file
        removed = [v for v in versions if v['file_id'] in selected]
        plan = {'files': list(selected.values()), 'versions': removed, 'skipped': dict(skipped)}
        (destination / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        # Hide results and persist the deletion queue before touching Qdrant.
        with runtime.db.connect() as c:
            c.executemany("UPDATE files SET active_version=NULL,desired_version=NULL,status='deleted',error=NULL WHERE id=?",
                          [(ident,) for ident in selected])
            c.executemany('INSERT OR IGNORE INTO deletions VALUES(?,?)', [(v['id'], time.time()) for v in removed])
        root = (runtime.settings.data / 'vectors').resolve()
        for start in range(0, len(removed), 128):
            identifiers = [v['id'] for v in removed[start:start + 128]]
            selector = models.Filter(must=[models.FieldCondition(key='version', match=models.MatchAny(any=identifiers))])
            store = runtime.get_store()
            store.client.delete(store.collection, models.FilterSelector(filter=selector), wait=True)
            if store.client.count(store.collection, count_filter=selector, exact=True).count:
                raise RuntimeError('Vector deletion verification failed; persisted queue retained')
            with runtime.db.connect() as c:
                for ident in identifiers:
                    c.execute('DELETE FROM frames WHERE version=?', (ident,))
                    c.execute('DELETE FROM chunks WHERE version=?', (ident,))
                    c.execute('DELETE FROM versions WHERE id=?', (ident,))
                    c.execute('DELETE FROM deletions WHERE version=?', (ident,))
            for ident in identifiers:
                if str(uuid.UUID(ident)) != ident:
                    raise RuntimeError('Invalid vector directory ID')
                folder = (root / ident).resolve()
                if not folder.is_relative_to(root) or folder.parent != root:
                    raise RuntimeError('Vector cleanup path escapes storage')
                if folder.exists():
                    shutil.rmtree(folder)
            print(json.dumps({'deleted_versions': min(start + 128, len(removed)), 'total_versions': len(removed)}), flush=True)
        result = {'listed_files': len(rows), 'removed_files': len(selected), 'removed_versions': len(removed),
                  'removed_total_frames': sum(v['total'] for v in removed),
                  'removed_processed_frames': sum(v['cursor'] for v in removed), 'skipped': dict(skipped),
                  'remaining_scope': indexer.scope_stats()}
        (destination / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        runtime.db.event('prune_deleted', json.dumps({k: v for k, v in result.items() if k != 'remaining_scope'}))
        return result


def main():
    import argparse
    from dotenv import load_dotenv
    from .console import configure_console
    from .config import Settings
    from .search import Runtime
    configure_console()
    load_dotenv('.env')
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    runtime = Runtime(Settings())
    try:
        print(json.dumps(prune_deleted(runtime, args.manifest, args.output), ensure_ascii=False), flush=True)
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
