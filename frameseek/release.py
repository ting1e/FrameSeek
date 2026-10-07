"""Create and verify portable releases; does not transfer or deploy to a NAS."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .config import Settings
from .model import digest
from .search import Runtime


def verify(folder: Path) -> dict:
    manifest = json.loads((folder/'release.json').read_text(encoding='utf-8'))
    for relative,expected in manifest['files'].items():
        path = (folder/relative).resolve()
        if not path.is_relative_to(folder.resolve()) or digest(path) != expected:
            raise RuntimeError('Release checksum mismatch: '+relative)
    with sqlite3.connect(folder/'data/metadata.sqlite3') as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed')
        fingerprint = db.execute("SELECT value FROM meta WHERE key='model_fingerprint'").fetchone()
        if not fingerprint or fingerprint[0] != manifest['model_fingerprint']:
            raise RuntimeError('SQLite/model fingerprint mismatch')
    return {'verified_files':len(manifest['files']),'model_fingerprint':manifest['model_fingerprint'],
            'stats':manifest['stats'],'qdrant_version':manifest['qdrant_version']}


def build(settings: Settings, destination: Path, allow_pending: bool = False) -> dict:
    if destination.exists():
        raise ValueError('Release destination must be new')
    runtime = Runtime(settings)
    try:
        indexer = runtime.get_indexer()
        with indexer.operation_lock:
            stats = runtime.db.stats()
            if not stats['frames'] or (stats['pending'] and not allow_pending):
                raise RuntimeError('Release requires a nonempty, complete index; --allow-pending is for preflight backups only')
            destination.mkdir(parents=True)
            (destination/'data').mkdir()
            runtime.db.backup(destination/'data/metadata.sqlite3')
            store = runtime.get_store()
            snapshot = store.create_snapshot()
            url = settings.qdrant_url.rstrip('/')+f'/collections/{store.collection}/snapshots/{snapshot.name}'
            with httpx.stream('GET',url,headers={'api-key':settings.qdrant_key or ''},timeout=3600) as response:
                response.raise_for_status()
                with (destination/'qdrant.snapshot').open('wb') as out:
                    for block in response.iter_bytes(1024*1024):
                        out.write(block)
                    out.flush(); os.fsync(out.fileno())
            for row in runtime.db.rows('SELECT path,sha256 FROM chunks'):
                src = settings.data/row['path']
                if digest(src) != row['sha256']:
                    raise RuntimeError('Corrupt vector block: '+row['path'])
                dst = destination/'data'/row['path']
                dst.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(src,dst)
            model_dir = destination/'models/dinov3-vitl16'
            model_dir.mkdir(parents=True)
            for name in [*settings.manifest['weights'],'preprocessor_config.json','provenance.json','LICENSE.md']:
                src = settings.model/name
                if src.is_file():
                    shutil.copy2(src,model_dir/name)
            (model_dir/'manifest.json').write_text(json.dumps(settings.manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            for name in ['compose.ghcr.yml','compose.yml','compose.local.yml','Dockerfile','pyproject.toml','README.md']:
                shutil.copy2(Path(__file__).parent.parent/name,destination/name)
            shutil.copytree(Path(__file__).parent,destination/'frameseek',ignore=shutil.ignore_patterns('__pycache__'))
            manifest = {'created':time.time(),'model_fingerprint':settings.manifest['fingerprint'],
                        'collection':store.collection,'qdrant_version':'1.19.2','stats':stats,
                        'preflight_only':bool(stats['pending']),'deployment_requires_user_approval':True,
                        'files':{p.relative_to(destination).as_posix():digest(p) for p in destination.rglob('*') if p.is_file()}}
            (destination/'release.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        return verify(destination)
    finally:
        runtime.close()


def restore(settings: Settings, folder: Path):
    checked = verify(folder)
    manifest = json.loads((folder/'release.json').read_text(encoding='utf-8'))
    if settings.db_path.exists() or any((settings.data/'vectors').iterdir()):
        raise ValueError('Restore requires a fresh application data directory')
    if settings.manifest['fingerprint'] != checked['model_fingerprint']:
        raise ValueError('Install the matching release model before restore')
    from .vectors import VectorStore
    store = VectorStore(settings,checked['model_fingerprint'])
    try:
        if store.client.collection_exists(store.collection):
            raise ValueError('Restore requires an absent Qdrant collection; preserve the current release separately')
        with (folder/'qdrant.snapshot').open('rb') as snapshot:
            response = httpx.post(settings.qdrant_url.rstrip('/')+f'/collections/{store.collection}/snapshots/upload',
                                  headers={'api-key':settings.qdrant_key or ''},
                                  params={'priority':'snapshot','wait':'true'},
                                  files={'snapshot':('qdrant.snapshot',snapshot,'application/octet-stream')},timeout=3600)
            response.raise_for_status()
        if store.count() != manifest['stats']['frames'] and not manifest['preflight_only']:
            raise RuntimeError('Restored vector count differs from published frame count')
        shutil.copy2(folder/'data/metadata.sqlite3',settings.db_path)
        shutil.copytree(folder/'data/vectors',settings.data/'vectors',dirs_exist_ok=True)
        store.ensure()
        print(json.dumps({'restored_vectors':store.count(),**checked},ensure_ascii=False,indent=2))
    finally:
        store.close()


def main():
    from .console import configure_console
    configure_console()
    load_dotenv(Path.cwd() / '.env')
    if os.getenv('IMGS_AUTH_FILE'):
        load_dotenv(os.environ['IMGS_AUTH_FILE'], override=False)
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['build','verify','restore'])
    parser.add_argument('folder',type=Path)
    parser.add_argument('--allow-pending',action='store_true')
    args = parser.parse_args()
    if args.action == 'verify':
        result = verify(args.folder)
    elif args.action == 'build':
        result = build(Settings(),args.folder,args.allow_pending)
    else:
        restore(Settings(),args.folder)
        return
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
