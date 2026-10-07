import os

import numpy as np
import pytest
from PIL import Image

from conftest import build, write_bif
from frameseek.search import fold


def test_two_roots_same_name_and_neighbors(runtime):
    for root in runtime.settings.sources.values(): write_bif(root / 'same.bif')
    build(runtime)
    assert runtime.db.stats()['frames'] == 6
    assert runtime.db.stats()['ready_files'] == 2
    rows = runtime.db.rows('SELECT * FROM files')
    assert len({row['id'] for row in rows}) == 2
    result = runtime.query(Image.new('RGB', (64, 36), 'red'), 20, collapse=False)
    assert {row['source'] for row in result['results'][:2]} == {'sda', 'sdc'}
    assert all(row['frame_no'] == 0 for row in result['results'][:2])


def test_resume_after_vector_store_failure_without_reembedding(runtime, monkeypatch):
    write_bif(runtime.settings.sources['sda'] / 'resume.bif')
    indexer = runtime.get_indexer(); indexer.scan(True)
    job = indexer.pending()[0]
    upsert = indexer.store.upsert
    monkeypatch.setattr(indexer.store, 'upsert', lambda *_: (_ for _ in ()).throw(ConnectionError('interrupted')))
    with pytest.raises(ConnectionError): indexer.process_version(job['id'])
    assert runtime.db.one('SELECT cursor FROM versions WHERE id=?', (job['id'],))['cursor'] == 2
    assert runtime.db.stats()['frames'] == 0
    calls = runtime.embedder.calls
    monkeypatch.setattr(indexer.store, 'upsert', upsert)
    indexer.process_version(job['id'])
    assert runtime.embedder.calls == calls + 1  # Only the unfinished last frame.
    assert runtime.db.stats()['frames'] == 3
    assert indexer.store.count() == 3


def test_bad_jpeg_skipped_and_corrupt_bif_logged(runtime):
    write_bif(runtime.settings.sources['sda'] / 'jpeg.bif', corrupt=1)
    (runtime.settings.sources['sda'] / 'broken.bif').write_bytes(b'broken')
    build(runtime)
    assert runtime.db.stats()['frames'] == 2
    assert runtime.db.stats()['errors'] == 1
    assert runtime.db.one('SELECT COUNT(*) AS n FROM frames WHERE valid=0')['n'] == 1


def test_parallel_decode_preserves_mapping_and_resume(runtime, monkeypatch):
    runtime.settings.decode_workers = 4
    runtime.settings.chunk_frames = 4
    write_bif(runtime.settings.sources['sda'] / 'prefetch.bif',
              ('red', 'green', 'blue', 'yellow', 'purple'), corrupt=1)
    indexer = runtime.get_indexer()
    indexer.scan(True)
    job = indexer.pending()[0]
    upsert = indexer.store.upsert
    monkeypatch.setattr(indexer.store, 'upsert', lambda *_: (_ for _ in ()).throw(ConnectionError('interrupted')))
    with pytest.raises(ConnectionError):
        indexer.process_version(job['id'])
    chunk = runtime.db.one('SELECT * FROM chunks')
    with np.load(runtime.settings.data / chunk['path']) as data:
        assert data['frames'].tolist() == [0, 2, 3]
        assert np.allclose(np.linalg.norm(data['vectors'], axis=1), 1)
    calls = runtime.embedder.calls
    monkeypatch.setattr(indexer.store, 'upsert', upsert)
    indexer.process_version(job['id'])
    assert runtime.embedder.calls == calls + 1
    assert runtime.db.stats()['frames'] == 4
    assert indexer.store.count() == 4


def test_directory_scope_filters_before_limit_and_preserves_other_jobs(runtime):
    for number in range(101):
        write_bif(runtime.settings.sources['sda'] / 'excluded' / f'{number}.bif', ('red',))
    write_bif(runtime.settings.sources['sda'] / 'mv' / 'one.bif')
    write_bif(runtime.settings.sources['sda'] / 'mv-other' / 'one.bif')
    write_bif(runtime.settings.sources['sdc'] / 'asmr' / 'nested' / 'two.bif')
    write_bif(runtime.settings.sources['sdc'] / 'mv' / 'three.bif')
    indexer = runtime.get_indexer()
    indexer.scan(True)
    runtime.settings.index_scope = {'sda': ['mv'], 'sdc': ['asmr']}
    assert len(indexer.pending()) == 2
    assert len(indexer.pending('sda')) == 1
    assert indexer.scope_stats()['total_frames'] == 6
    for job in indexer.pending():
        indexer.process_version(job['id'])
    assert not indexer.pending()
    assert indexer.scope_stats()['ready_files'] == 2
    assert indexer.scope_stats()['pending_files'] == 0
    assert runtime.db.stats()['pending'] == 103
    runtime.settings.index_scope = {}
    assert len(indexer.pending()) == 100


def test_modify_delete_and_reappear(runtime):
    path = write_bif(runtime.settings.sources['sda'] / 'change.bif')
    build(runtime)
    old_ids = {row['id'] for row in runtime.db.rows('SELECT * FROM frames')}
    write_bif(path, ('yellow', 'purple'))
    build(runtime)
    assert runtime.db.stats()['frames'] == 2
    assert runtime.get_store().count() == 2
    assert all(runtime.db.published_frame(ident) is None for ident in old_ids)
    path.unlink(); build(runtime)
    assert runtime.db.stats()['frames'] == 0
    assert runtime.get_store().count() == 0
    write_bif(path, ('yellow', 'purple')); build(runtime)
    assert runtime.db.stats()['frames'] == 2
    assert runtime.get_store().count() == 2


def test_modified_bif_waits_then_reembeds_and_cleans_old_vectors(runtime):
    path = write_bif(runtime.settings.sources['sda'] / 'watched' / 'change.bif')
    build(runtime)
    indexer = runtime.get_indexer()
    old_version = runtime.db.one('SELECT active_version FROM files')['active_version']
    old_chunk = runtime.db.one('SELECT * FROM chunks WHERE version=? ORDER BY start', (old_version,))
    with np.load(runtime.settings.data / old_chunk['path']) as data:
        old_vectors = data['vectors'].copy()
    calls = runtime.embedder.calls
    runtime.settings.stable_seconds = 60
    write_bif(path, ('yellow', 'purple'))
    scan = indexer.scan()
    assert scan['modified'] == 1 and scan['queued'] == 0
    assert not indexer.pending()
    assert runtime.db.stats()['frames'] == 0  # Old offsets no longer represent this file.
    runtime.db.execute('UPDATE files SET stable_since=stable_since-61')
    assert indexer.scan()['queued'] == 1
    job = indexer.pending()[0]
    assert job['id'] != old_version
    assert indexer.process_version(job['id']) == 2
    assert runtime.embedder.calls > calls
    new_chunk = runtime.db.one('SELECT * FROM chunks WHERE version=? ORDER BY start', (job['id'],))
    with np.load(runtime.settings.data / new_chunk['path']) as data:
        assert not np.allclose(data['vectors'], old_vectors)
    indexer.cleanup()
    assert runtime.get_store().count() == 2
    assert runtime.db.stats()['frames'] == 2
    assert indexer.scan()['modified'] == 0
    assert runtime.db.one("SELECT kind FROM events WHERE kind='bif_modified'")


def test_modified_while_encoding_discards_superseded_progress(runtime):
    path = write_bif(runtime.settings.sources['sda'] / 'change-during-build.bif')
    indexer = runtime.get_indexer()
    indexer.scan(True)
    old = indexer.pending()[0]
    assert indexer.process_version(old['id'], max_frames=1) == 1
    write_bif(path, ('yellow', 'purple'))
    with pytest.raises(RuntimeError, match='awaiting rescan'):
        indexer.process_version(old['id'])
    assert indexer.scan(True)['modified'] == 1
    new = indexer.pending()[0]
    assert new['id'] != old['id'] and new['cursor'] == 0
    assert indexer.process_version(old['id']) == 0
    indexer.process_version(new['id'], max_frames=1)
    # Recreate the indexer, as after restarting an interrupted update.
    from frameseek.indexer import Indexer
    resumed = Indexer(runtime.settings, runtime.db, runtime.embedder, runtime.get_store())
    resumed.process_version(new['id'])
    resumed.cleanup()
    assert runtime.db.stats()['frames'] == 2
    assert runtime.get_store().count() == 2


def test_timestamp_only_change_reuses_matching_embeddings(runtime):
    path = write_bif(runtime.settings.sources['sda'] / 'touch.bif')
    build(runtime)
    calls = runtime.embedder.calls
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    build(runtime)
    assert runtime.embedder.calls == calls
    assert runtime.db.stats()['frames'] == 3
    assert runtime.get_store().count() == 3


def test_missing_source_does_not_delete_existing_index(runtime):
    path = write_bif(runtime.settings.sources['sda'] / 'preserve.bif'); build(runtime)
    root = runtime.settings.sources['sda']; moved = root.with_name('offline')
    root.rename(moved)
    try:
        runtime.get_indexer().scan(True)
        assert runtime.db.stats()['ready_files'] == 1
    finally: moved.rename(root)


def test_stability_wait(runtime):
    write_bif(runtime.settings.sources['sda'] / 'still-writing.bif')
    runtime.settings.stable_seconds = 60
    result = runtime.get_indexer().scan()
    assert result['queued'] == 0
    assert not runtime.get_indexer().pending()


def test_partial_limit_does_not_publish(runtime):
    write_bif(runtime.settings.sources['sda'] / 'partial.bif')
    indexer = runtime.get_indexer(); indexer.scan(True)
    job = indexer.pending()[0]
    assert indexer.process_version(job['id'], max_frames=1) == 1
    assert runtime.db.stats()['frames'] == 0
    indexer.process_version(job['id'])
    assert runtime.db.stats()['frames'] == 3


def test_chunk_integrity_and_model_identity(runtime):
    write_bif(runtime.settings.sources['sda'] / 'integrity.bif'); build(runtime)
    chunk = runtime.db.one('SELECT * FROM chunks')
    (runtime.settings.data / chunk['path']).write_bytes(b'corrupted')
    with pytest.raises(RuntimeError, match='checksum'): runtime.get_indexer().flush_chunk(chunk)
    with pytest.raises(RuntimeError, match='fingerprint'):
        runtime.db.bind_model({'fingerprint':'different-model'})


def test_folding_combines_nonadjacent_hits_by_file():
    rows = [{'id':str(n), 'version':'one', 'frame_no':n, 'score':s, 'time_ms':n*1000}
            for n,s in [(0,.8),(1,.9),(3,.7),(4,.6),(10,.95)]]
    folded = fold(rows)
    assert [row['frame_no'] for row in folded] == [10]
    assert [row['group_count'] for row in folded] == [5]
    assert [hit['time_ms'] for hit in folded[0]['group']] == [0,1000,3000,4000,10000]
    separate = [{**rows[0], 'source':source, 'relpath':'same.bif'} for source in ['sda','sdc']]
    assert len(fold(separate)) == 2


def test_pending_source_filter(runtime):
    write_bif(runtime.settings.sources['sda']/'a.bif')
    write_bif(runtime.settings.sources['sdc']/'b.bif')
    indexer = runtime.get_indexer()
    indexer.scan(True)
    assert len(indexer.pending()) == 2
    jobs = indexer.pending('sdc')
    assert len(jobs) == 1
    file = runtime.db.one('SELECT source FROM files WHERE id=?',(jobs[0]['file_id'],))
    assert file['source'] == 'sdc'


def test_scan_deadline_yields_without_losing_progress(runtime):
    write_bif(runtime.settings.sources['sda']/'yield.bif')
    indexer = runtime.get_indexer()
    indexer.scan(True)
    job = indexer.pending()[0]
    assert indexer.process_version(job['id'],yield_at=0)==0
    assert runtime.db.stats()['frames']==0
    assert runtime.embedder.calls==0
    assert indexer.process_version(job['id'])==3
    assert runtime.db.stats()['frames']==3


def test_monitor_folders_prunes_scan_and_preserves_existing(runtime):
    from conftest import write_bif, build
    from frameseek.preferences import current, Preferences
    import json
    write_bif(runtime.settings.sources['sda']/'keep'/'one.bif')
    write_bif(runtime.settings.sources['sda']/'keeper'/'outside.bif')
    write_bif(runtime.settings.sources['sdc']/'keep'/'other.bif')
    build(runtime)
    before = runtime.db.stats()['frames']
    values = current(runtime.settings)
    values['monitor_folders'] = [{'source':'sda','path':'keep'}]
    runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)", (Preferences.model_validate(values).model_dump_json(),))
    write_bif(runtime.settings.sources['sda']/'keep'/'nested'/'new.bif')
    write_bif(runtime.settings.sources['sda']/'keeper'/'unseen.bif')
    scan = runtime.get_indexer().scan(trust_stable=True)
    assert scan['observed'] == 2 and scan['deleted'] == 0
    assert runtime.db.stats()['frames'] == before
    assert not runtime.db.one("SELECT id FROM files WHERE relpath='keeper/unseen.bif'")
    assert len(runtime.get_indexer().pending()) == 1
    assert runtime.get_indexer().pending()[0]['file_id'] == runtime.db.one("SELECT id FROM files WHERE relpath='keep/nested/new.bif'")['id']
    (runtime.settings.sources['sda']/'keep'/'one.bif').unlink()
    assert runtime.get_indexer().scan(trust_stable=True)['deleted'] == 1
    assert runtime.db.one("SELECT active_version FROM files WHERE relpath='keeper/outside.bif'")['active_version']
    values['monitor_folders'] = []
    runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)", (Preferences.model_validate(values).model_dump_json(),))
    assert runtime.get_indexer().scan(trust_stable=True)['observed'] == 0
    assert runtime.get_indexer().pending() == []


def test_missing_monitored_directory_does_not_delete_index(runtime):
    from conftest import write_bif, build
    from frameseek.preferences import current, Preferences
    write_bif(runtime.settings.sources['sda']/'watched'/'one.bif'); build(runtime)
    values = current(runtime.settings)
    values['monitor_folders'] = [{'source':'sda','path':'watched'}]
    runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)", (Preferences.model_validate(values).model_dump_json(),))
    (runtime.settings.sources['sda']/'watched').rename(runtime.settings.sources['sda']/'offline')
    counts = runtime.get_indexer().scan(trust_stable=True)
    assert counts['scan_errors'] and counts['deleted'] == 0
    assert runtime.db.stats()['frames'] == 3


def test_worker_configuration_event_does_not_start_manual_run(runtime):
    import time
    runtime.worker.start()
    try:
        runtime.worker.configuration_changed()
        deadline = time.monotonic()+3
        while runtime.worker.config_event.is_set() and time.monotonic()<deadline:
            time.sleep(.02)
        assert runtime.worker.config_event.is_set() is False
        assert runtime.worker.manual_active is False
        assert runtime.worker.activity == 'disabled'
    finally:
        runtime.worker.stop()


def test_configuration_change_yields_after_saved_chunk(runtime):
    import threading
    write_bif(runtime.settings.sources['sda']/'change-settings.bif')
    indexer = runtime.get_indexer()
    indexer.scan(True)
    job = indexer.pending()[0]
    change = threading.Event()
    embed = runtime.embedder.embed
    def changed(images, search=False):
        result = embed(images, search=search)
        change.set()
        return result
    runtime.embedder.embed = changed
    assert indexer.process_version(job['id'], yield_requested=change) == 2
    saved = runtime.db.one('SELECT cursor,status FROM versions WHERE id=?',(job['id'],))
    assert saved == {'cursor':2,'status':'pending'}
