import json
import time

from fastapi.testclient import TestClient
from PIL import Image

from conftest import build, write_bif
from frameseek.engine.library import enqueue, inventory, process_removal, fill_sizes
from frameseek.engine.monitoring import folders, includes
from frameseek.core.preferences import requested
from frameseek.web import create_app


def seed(runtime):
    first = write_bif(runtime.settings.sources['sda']/'剧集'/'one.bif')
    write_bif(runtime.settings.sources['sda']/'剧集'/'子目录'/'two.bif', colors=('blue',))
    write_bif(runtime.settings.sources['sda']/'剧集备份'/'one.bif', colors=('green',))
    write_bif(runtime.settings.sources['sdc']/'剧集'/'one.bif', colors=('red',))
    image = first.parent/'photo.png'
    Image.new('RGB',(16,16),'red').save(image)
    build(runtime)
    return first, image


def test_inventory_and_directory_removal_preserve_media_and_other_scopes(runtime):
    first, image = seed(runtime)
    overview = inventory(runtime)
    assert len(overview['directories']) == 2
    assert sum(g['vectors'] for g in overview['directories']) == 7
    data = inventory(runtime,'sda','剧集')
    selected = next(g for g in data['directories'] if g['directory']=='剧集')
    assert (selected['files'],selected['bif'],selected['images'],selected['vectors']) == (3,2,1,5)
    assert selected['bytes'] > 0
    assert len(data['files']) == 3
    # Old index data remains visible after removing this directory from monitoring.
    runtime.settings.monitor_folders = [{'source':'sdc','path':''}]
    assert inventory(runtime,'sda','剧集')['total_files'] == 3
    job = enqueue(runtime,'sda','剧集')
    assert not includes(folders(runtime.settings,runtime.db),'sda','剧集/one.bif')
    while runtime.db.one('SELECT status FROM index_removals WHERE id=?',(job['id'],))['status'] != 'done':
        assert process_removal(runtime)
    assert first.exists() and image.exists()
    assert runtime.get_store().count() == 2
    assert runtime.db.stats()['frames'] == 2
    assert len(list((runtime.settings.data/'vectors').iterdir())) == 2
    assert inventory(runtime,'sda','剧集')['total_files'] == 0
    assert inventory(runtime,'sda','剧集备份')['total_files'] == 1
    assert str(first.parent.as_posix()) in requested(runtime.settings,runtime.db)['excluded_directories']


def test_inventory_uses_saved_sizes_and_backfills_legacy_chunks(runtime, monkeypatch):
    write_bif(runtime.settings.sources['sda']/'folder'/'one.bif')
    build(runtime)
    expected = inventory(runtime)['directories'][0]['bytes']
    runtime.db.execute('UPDATE chunks SET byte_size=NULL')
    from pathlib import Path
    original_stat = Path.stat
    def refuse_vector_stat(path, *args, **kwargs):
        if path.suffix == '.npz':
            raise AssertionError('Directory API must not inspect vector chunk files')
        return original_stat(path,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',refuse_vector_stat)
    root = inventory(runtime)['directories'][0]
    assert root['size_pending'] and root['vectors']==3
    monkeypatch.setattr(Path,'stat',original_stat)
    assert fill_sizes(runtime)==2
    root = inventory(runtime)['directories'][0]
    assert not root['size_pending'] and root['bytes']==expected


def test_failed_delete_retries_and_explicit_rebuild(runtime, monkeypatch):
    first = write_bif(runtime.settings.sources['sda']/'folder'/'one.bif')
    build(runtime)
    old = runtime.db.one('SELECT active_version FROM files')['active_version']
    job = enqueue(runtime,'sda','folder',exclude=False)
    store = runtime.get_store()
    real_delete = store.delete_version
    def fail(version):
        raise RuntimeError('temporary storage failure')
    monkeypatch.setattr(store,'delete_version',fail)
    assert process_removal(runtime)
    state = runtime.db.one('SELECT * FROM index_removals WHERE id=?',(job['id'],))
    assert state['error'] and state['status']=='running' and state['completed']==0
    assert runtime.db.stats()['frames'] == 0
    assert not includes(folders(runtime.settings,runtime.db),'sda','folder/one.bif')
    # Reopen SQLite, as on application restart, and retry the persisted work.
    from frameseek.core.db import Database
    runtime.db = Database(runtime.settings.db_path)
    runtime.db.execute('UPDATE index_removals SET retry_at=0 WHERE id=?',(job['id'],))
    monkeypatch.setattr(store,'delete_version',real_delete)
    assert process_removal(runtime)
    assert runtime.get_store().count()==0 and first.exists()
    assert not runtime.db.one('SELECT * FROM versions WHERE id=?',(old,))
    assert includes(folders(runtime.settings,runtime.db),'sda','folder/one.bif')
    build(runtime)
    assert runtime.get_store().count()==3


def test_delete_interrupts_encoding_at_saved_chunk_and_resumes_cleanup(runtime, monkeypatch):
    first = write_bif(runtime.settings.sources['sda']/'folder'/'one.bif')
    second = write_bif(first.parent/'two.bif')
    indexer = runtime.get_indexer()
    indexer.scan(trust_stable=True)
    embed = runtime.embedder.embed
    job = []
    def enqueue_during_encoding(images, search=False):
        if not job:
            job.append(enqueue(runtime,'sda','folder'))
        return embed(images,search=search)
    monkeypatch.setattr(runtime.embedder,'embed',enqueue_during_encoding)
    indexer.process_version(indexer.pending()[0]['id'])
    assert runtime.get_store().count()==2  # Only the first saved chunk was written.
    assert runtime.db.stats()['frames']==0  # Incomplete version never published.
    assert not indexer.pending()
    version = runtime.db.one('SELECT id FROM versions WHERE cursor=2')['id']
    runtime.worker.suspended_task = {'activity':'indexing:test','version':version,'targets':None}
    runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('suspended_task',?)", (json.dumps(runtime.worker.suspended_task),))
    real_delete = runtime.get_store().delete_version
    def stop_after_one(version):
        real_delete(version)
        runtime.worker.stop_event.set()
    monkeypatch.setattr(runtime.get_store(),'delete_version',stop_after_one)
    process_removal(runtime)
    state = runtime.db.one('SELECT * FROM index_removals WHERE id=?',(job[0]['id'],))
    assert state['completed']==1 and state['status']=='running'
    runtime.worker.stop_event.clear()
    monkeypatch.setattr(runtime.get_store(),'delete_version',real_delete)
    process_removal(runtime)
    assert runtime.db.one('SELECT status FROM index_removals')['status']=='done'
    assert runtime.worker.suspended_task is None
    assert not runtime.db.one("SELECT 1 FROM meta WHERE key='suspended_task'")
    assert runtime.get_store().count()==0 and first.exists() and second.exists()


def test_library_api_auth_csrf_validation_history_and_background_processing(runtime):
    first = write_bif(runtime.settings.sources['sda']/'folder'/'one.bif')
    build(runtime)
    frame = runtime.db.one('SELECT * FROM frames LIMIT 1')
    runtime.db.save_search('history','query.jpg',b'jpeg',{'results':[{'id':frame['id'],'version':frame['version'],'source':'sda','relpath':'folder/one.bif'}]})
    with TestClient(create_app(runtime.settings,runtime)) as client:
        assert client.get('/api/index/library').status_code==401
        assert client.get('/api/index/removals').status_code==401
        login = client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers = {'X-CSRF-Token':login.json()['csrf']}
        payload = {'source':'sda','directory':'folder'}
        assert client.post('/api/index/removals',json=payload).status_code==403
        assert client.post('/api/index/removals',json={**payload,'directory':'../folder'},headers=headers).status_code==422
        assert client.post('/api/index/removals',json={**payload,'source':'other'},headers=headers).status_code==422
        assert client.get('/api/index/library',params={'keyword':'folder'}).json()['directories']
        result = client.post('/api/index/removals',json=payload,headers=headers)
        assert result.status_code==202
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            jobs = client.get('/api/index/removals').json()['jobs']
            if jobs[0]['status']=='done':
                break
            time.sleep(.03)
        assert jobs[0]['status']=='done'
        assert client.get('/api/history/history').json()['response']['results'][0]['index_deleted']
        assert client.get('/api/frames/'+frame['id']).status_code==404
        build(runtime)
        assert runtime.get_store().count()==0
        assert first.exists()
