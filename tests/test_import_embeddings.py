import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image

from conftest import build, write_bif
from frameseek import bif
from frameseek.import_embeddings import import_export, import_file
from frameseek.model import digest


def export_file(runtime, tmp_path, source='sda', relative='foreign/same.bif'):
    path = write_bif(runtime.settings.sources[source] / relative)
    frames = bif.parse(path)
    local_relative = source+'/'+relative
    item = dict(id=hashlib.sha256(local_relative.encode()).hexdigest(), source=source, relpath=relative,
                local_relpath=local_relative, size=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns,
                sha256=digest(path), frames=len(frames))
    folder = tmp_path / 'export' / 'output' / item['id']
    folder.mkdir(parents=True)
    blocks = []
    for start in range(0, len(frames), 2):
        batch = frames[start:start+2]
        images = [Image.open(io.BytesIO(bif.read_frame(path, f.offset, f.length))) for f in batch]
        values = runtime.embedder.embed(images)
        for image in images:
            image.close()
        dest = folder / f'{start:08d}.npz'
        np.savez(dest, vectors=values, frames=np.array([f.index for f in batch], np.int32),
                 times_ms=np.array([f.time_ms for f in batch], np.int64),
                 offsets=np.array([f.offset for f in batch], np.int64),
                 lengths=np.array([f.length for f in batch], np.int64))
        blocks.append(dict(file=dest.name, sha256=digest(dest), start=start,
                           end=start+len(batch), valid=len(batch), errors=[]))
    result = dict(input=item, model_fingerprint=runtime.settings.manifest['fingerprint'],
                  total_frames=len(frames), blocks=blocks)
    (folder / 'complete.json').write_text(json.dumps(result))
    export = tmp_path / 'export'
    (export / 'input.json').write_text(json.dumps([item]))
    (export / 'verification.json').write_text(json.dumps(dict(verified=True, failed_frames=0,
        model_fingerprint=result['model_fingerprint'], files=1, valid_frames=len(frames))))
    return export, result


def test_import_preserves_existing_search_and_is_idempotent(runtime, tmp_path):
    write_bif(runtime.settings.sources['sdc'] / 'same.bif')
    build(runtime)
    old_ids = {r['id'] for r in runtime.db.rows('SELECT id FROM frames')}
    export, result = export_file(runtime, tmp_path)
    calls = runtime.embedder.calls
    report = import_export(runtime, export, tmp_path / 'report')
    assert runtime.embedder.calls == calls
    assert report['imported_frames'] == 3
    assert report['unrelated_ready_files_preserved'] == 1
    assert old_ids <= {r['id'] for r in runtime.db.rows('SELECT id FROM frames')}
    assert runtime.db.stats()['frames'] == runtime.get_store().count() == 6
    found = runtime.query(Image.new('RGB', (64, 36), 'red'), 20, collapse=False)['results']
    assert {r['source'] for r in found[:2]} == {'sda', 'sdc'}
    repeated = import_export(runtime, export, tmp_path / 'report2')
    assert repeated['reused_files'] == 1
    assert runtime.get_store().count() == 6


def test_failed_upsert_remains_unpublished_and_retries_without_inference(runtime, tmp_path, monkeypatch):
    export, result = export_file(runtime, tmp_path)
    indexer = runtime.get_indexer()
    original = indexer.store.upsert
    calls = runtime.embedder.calls
    attempt = [0]
    def fail_second(rows, vectors):
        attempt[0] += 1
        if attempt[0] == 2:
            raise ConnectionError('interrupted')
        return original(rows, vectors)
    monkeypatch.setattr(indexer.store, 'upsert', fail_second)
    with pytest.raises(ConnectionError):
        import_file(runtime, export, result)
    assert runtime.db.stats()['frames'] == 0
    assert runtime.get_store().count() == 2
    monkeypatch.setattr(indexer.store, 'upsert', original)
    import_file(runtime, export, result)
    assert runtime.embedder.calls == calls
    assert runtime.db.stats()['frames'] == runtime.get_store().count() == 3


def test_corrupt_export_and_model_mismatch_rejected(runtime, tmp_path):
    export, result = export_file(runtime, tmp_path)
    bad = {**result, 'model_fingerprint': 'wrong'}
    with pytest.raises(ValueError, match='fingerprint'):
        import_file(runtime, export, bad)
    block = export / 'output' / result['input']['id'] / result['blocks'][0]['file']
    block.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        import_file(runtime, export, result)
    assert runtime.db.stats()['files'] == 0
