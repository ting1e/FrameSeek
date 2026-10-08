import io
import mimetypes

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import build, jpeg, write_bif
from frameseek.web import create_app


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime.settings, runtime)) as client:
        yield client


def login(client):
    result = client.post('/api/login', json={'username':'admin','password':'testing-secret'})
    assert result.status_code == 200
    return {'X-CSRF-Token':result.json()['csrf']}


def test_javascript_mime_ignores_windows_file_association(runtime):
    previous = mimetypes.guess_type('app.js')[0]
    try:
        mimetypes.add_type('text/plain', '.js')
        with TestClient(create_app(runtime.settings, runtime)) as client:
            response = client.get('/static/app.js')
            assert response.headers['content-type'].split(';')[0] == 'text/javascript'
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert response.headers['cache-control'] == 'no-cache'
    finally:
        mimetypes.add_type(previous or 'text/javascript', '.js')


def test_auth_and_csrf_protect_all_operations(client):
    assert client.get('/').status_code == 200
    assert client.get('/api/status').status_code == 401
    assert client.get('/api/frames/nonexistent').status_code == 401
    assert client.post('/api/updates/scan').status_code == 401
    headers = login(client)
    assert client.get('/api/status').status_code == 200
    assert client.post('/api/updates/pause').status_code == 403
    assert client.post('/api/updates/pause', headers=headers).json()['paused']
    assert client.post('/api/updates/resume', headers=headers).json()['paused'] is False
    assert client.post('/api/logout', headers=headers).status_code == 200
    assert client.get('/api/status').status_code == 401


def test_task_buttons_pause_scan_and_manual_resume(runtime, client):
    headers = login(client)
    runtime.worker.stop()
    runtime.worker.scan_event.clear()
    result = client.post('/api/updates/scan', headers=headers)
    assert result.json()['already_requested'] is False
    assert client.post('/api/updates/scan', headers=headers).json()['already_requested'] is True
    assert client.post('/api/updates/pause', headers=headers).json()['paused'] is True
    assert client.post('/api/updates/scan', headers=headers).status_code == 409
    runtime.worker.scan_event.clear()
    assert client.post('/api/updates/resume', headers=headers).json()['paused'] is False
    assert runtime.worker.index_event.is_set()  # Resume encodes the queue without another full scan.
    runtime.settings.monitor_folders = []
    assert client.post('/api/updates/scan', headers=headers).status_code == 409


def test_task_summary_matches_monitored_scope(runtime, client):
    runtime.worker.stop()
    write_bif(runtime.settings.sources['sda'] / 'watched' / 'one.bif')
    write_bif(runtime.settings.sources['sda'] / 'outside' / 'two.bif')
    runtime.get_indexer().scan(True)
    runtime.settings.monitor_folders = [{'source':'sda','path':'watched'}]
    headers = login(client)
    status = client.get('/api/status').json()
    assert status['tasks'] == {'stabilizing':0,'queued':1,'processing':0,'failed':0,'parse_failed':0}
    assert status['pending'] == 1
    assert status['monitor_directories'] == [(runtime.settings.sources['sda'] / 'watched').resolve().as_posix()]
    assert status['last_scan'] is not None and status['current_task'] is None
    job = runtime.get_indexer().pending()[0]
    runtime.db.execute("UPDATE versions SET status='failed' WHERE id=?", (job['id'],))
    assert client.get('/api/status').json()['tasks']['failed'] == 1


def test_full_search_preview_neighbors_and_changed_file(runtime, client):
    path = write_bif(runtime.settings.sources['sda'] / 'real.bif'); build(runtime)
    headers = login(client)
    result = client.post('/api/search', data={'top':20,'collapse':'false'}, files={'image':('shot.jpg',jpeg('red'),'image/jpeg')}, headers=headers)
    assert result.status_code == 200
    winner = result.json()['results'][0]
    assert winner['frame_no'] == 0
    assert client.get(winner['preview_url']).content.startswith(b'\xff\xd8')
    neighbors = client.get(winner['preview_url'] + '/neighbors').json()
    assert len(neighbors['frames']) == 3
    path.write_bytes(b'replaced')
    assert client.get(winner['preview_url']).status_code == 410


def test_bad_upload_wrong_top_and_limit(client):
    headers = login(client)
    assert client.post('/api/search', files={'image':('bad.png',b'not an image')}, headers=headers).status_code == 422
    assert client.post('/api/search', data={'top':5}, files={'image':('shot.jpg',jpeg('red'))}, headers=headers).status_code == 422
    assert client.post('/api/search', files={'image':('big.jpg',b'0'*(10*1024*1024+1))}, headers=headers).status_code == 413


def test_arbitrary_media_directory_roundtrip_search_and_settings(runtime, tmp_path):
    from frameseek.core.config import directory_sources, Settings
    import json
    runtime.settings.sources = directory_sources(json.dumps([str(tmp_path / '媒体目录')]))
    source, root = next(iter(runtime.settings.sources.items()))
    runtime.settings.monitor_folders = [{'source':source, 'path':''}]
    write_bif(root / '电影' / 'sample.bif')
    build(runtime)
    with TestClient(create_app(runtime.settings, runtime)) as client:
        headers = login(client)
        values = client.get('/api/settings').json()
        assert values['source_directories'][0]['root'] == root.resolve().as_posix()
        profile = values['saved']
        profile['decode_workers'] = 3
        saved = client.put('/api/settings', json=profile, headers=headers)
        assert saved.status_code == 200 and saved.json()['restart_required']
        reloaded = Settings(data=runtime.settings.data, sources=runtime.settings.sources)
        assert reloaded.decode_workers == 3
        assert reloaded.monitor_folders == [{'source':source, 'path':''}]
        found = client.get('/api/search/directories', params={'q':'媒体目录'}).json()['directories']
        assert found[0]['display_path'] == (root / '电影').resolve().as_posix()
        result = client.post('/api/search', data={'directory_keyword':'电影'}, files={'image':('shot.jpg',jpeg('red'))}, headers=headers)
        assert result.status_code == 200
        winner = result.json()['results'][0]
        assert winner['source'] == source and winner['root_directory'] == root.resolve().as_posix()
        assert client.get(winner['preview_url'] + '/neighbors').json()['root_directory'] == root.resolve().as_posix()


def test_login_rate_limit_and_unicode(client):
    for _ in range(5):
        assert client.post('/api/login',json={'username':'陌生人','password':'bad'}).status_code == 401
    assert client.post('/api/login',json={'username':'admin','password':'testing-secret'}).status_code == 429


def test_cross_origin_login_rejected(client):
    assert client.post('/api/login',json={'username':'admin','password':'testing-secret'},headers={'Origin':'https://evil.test'}).status_code == 403


def test_tampered_session_rejected(client):
    login(client)
    token = client.cookies.get('imgsearch_session')
    client.cookies.clear(); client.cookies.set('imgsearch_session',token[:-2]+'00')
    assert client.get('/api/status').status_code == 401


def test_search_history_persisted_and_authenticated(runtime, client):
    write_bif(runtime.settings.sources['sda'] / 'history.bif'); build(runtime)
    assert client.get('/api/history').status_code == 401
    headers = login(client)
    result = client.post('/api/search', data={'top':20,'collapse':'false'}, files={'image':('portrait.jpg',jpeg('red'),'image/jpeg')}, headers=headers)
    history_id = result.json()['history_id']
    listing = client.get('/api/history').json()
    assert listing['total'] == 1
    assert listing['items'][0]['filename'] == 'portrait.jpg'
    detail = client.get('/api/history/' + history_id).json()
    assert detail['response']['results'] == result.json()['results']
    assert detail['response']['top'] == 20
    assert detail['response']['collapse'] is False
    thumbnail = client.get(detail['thumbnail_url'])
    assert thumbnail.headers['content-type'] == 'image/jpeg'
    assert thumbnail.content.startswith(b'\xff\xd8')
    from frameseek.core.db import Database
    reopened = Database(runtime.settings.db_path)
    assert reopened.one('SELECT id FROM search_history')['id'] == history_id
    client.post('/api/logout', headers=headers)
    assert client.get('/api/history/' + history_id).status_code == 401
    assert client.get(detail['thumbnail_url']).status_code == 401
    headers = login(client)
    assert client.delete('/api/history/' + history_id).status_code == 403
    assert client.delete('/api/history/' + history_id, headers=headers).status_code == 200
    assert client.get('/api/history/' + history_id).status_code == 404
    assert client.get('/api/history').json()['total'] == 0


def test_all_history_retained_and_paged(runtime, client):
    for i in range(105):
        runtime.db.save_search(str(i), 'query.jpg', jpeg('red'), {'returned':0,'results':[]})
    login(client)
    first = client.get('/api/history?limit=24').json()
    assert first['total'] == 105 and first['has_more']
    assert len(first['items']) == 24
    all_ids = set()
    for offset in range(0, 105, 24):
        page = client.get(f'/api/history?offset={offset}&limit=24').json()
        all_ids.update(item['id'] for item in page['items'])
    assert len(all_ids) == 105
    assert client.get('/api/history?offset=96&limit=24').json()['has_more'] is False
    assert client.get('/api/history?offset=-1').status_code == 422
    assert client.get('/api/history?limit=999').status_code == 422


def test_settings_validation_persistence_and_export(runtime, client):
    assert client.get('/api/settings').status_code == 401
    assert client.get('/api/settings/compose').status_code == 401
    headers = login(client)
    original = client.get('/api/settings').json()
    values = {**original['saved'], 'cpu_threads':2,'qdrant_memory_gib':3,'default_top':50}
    assert client.put('/api/settings',json=values).status_code == 403
    invalid = {**values,'qdrant_memory_gib':0}
    assert client.put('/api/settings',json=invalid,headers=headers).status_code == 422
    assert client.put('/api/settings',json={**values,'indexing_threads':2},headers=headers).status_code == 422
    assert client.put('/api/settings',json={**values,'password':'evil'},headers=headers).status_code == 422
    result = client.put('/api/settings',json=values,headers=headers)
    assert result.status_code == 200 and result.json()['restart_required']
    state = client.get('/api/settings').json()
    assert state['saved']['cpu_threads'] == 2
    assert state['running']['cpu_threads'] == 4
    assert runtime.settings.default_top == 50
    from frameseek.core.config import Settings
    restarted = Settings(data=runtime.settings.data)
    assert restarted.cpu_threads == 2
    assert restarted.qdrant_memory_gib == runtime.settings.qdrant_memory_gib
    assert restarted.default_top == 50
    exported = client.get('/api/settings/compose').json()
    assert exported['services']['qdrant']['mem_limit'] == f'{runtime.settings.qdrant_memory_gib}g'
    assert 'IMGS_TORCH_THREADS' not in exported['services']['app']['environment']
    assert 'password' not in str(exported)
    assert client.put('/api/settings',json=original['saved'],headers=headers).status_code == 200
    assert client.get('/api/settings').json()['restart_required'] is False


def test_docker_mode_is_not_overridden_by_saved_preferences(runtime, client, monkeypatch):
    headers = login(client)
    values = client.get('/api/settings').json()['saved']
    # Legacy API bodies remain readable but cannot alter container-owned settings.
    values.update(mode='high_memory', qdrant_memory_gib=12, app_memory_gib=8, indexing_threads=2)
    assert client.put('/api/settings', json=values, headers=headers).status_code == 200
    saved = client.get('/api/settings').json()['saved']
    assert saved['mode'] == runtime.settings.mode
    assert saved['qdrant_memory_gib'] == runtime.settings.qdrant_memory_gib
    monkeypatch.setenv('IMGS_MODE', 'high_memory')
    from frameseek.core.config import Settings
    restarted = Settings(data=runtime.settings.data)
    assert restarted.mode == 'high_memory'
    assert restarted.qdrant_memory_gib == 12


def test_monitor_configuration_validation_and_live_scope(runtime, client):
    headers = login(client)
    values = client.get('/api/settings').json()['saved']
    assert values['monitor_folders'] == [{'source':'sda','path':''},{'source':'sdc','path':''}]
    for path in ['../outside','/etc','folder/../../outside','folder\\outside']:
        invalid = {**values,'monitor_folders':[{'source':'sda','path':path}]}
        assert client.put('/api/settings',json=invalid,headers=headers).status_code == 422
    invalid = {**values,'monitor_folders':[{'source':'missing','path':''}]}
    assert client.put('/api/settings',json=invalid,headers=headers).status_code == 422
    values['monitor_folders'] = [{'source':'sda','path':'中文目录/%_test/'}]
    assert client.put('/api/settings',json=values,headers=headers).status_code == 200
    state = client.get('/api/settings').json()
    assert state['saved']['monitor_folders'][0]['path'] == '中文目录/%_test'
    assert state['restart_required'] is False
    assert runtime.settings.monitor_folders == state['saved']['monitor_folders']


@pytest.mark.parametrize("top", [200, 500])
def test_large_search_result_limits(runtime, client, top):
    write_bif(runtime.settings.sources['sda'] / 'large-limit.bif')
    build(runtime)
    headers = login(client)
    response = client.post('/api/search', data={'top':top}, files={'image':('shot.jpg',jpeg('red'),'image/jpeg')}, headers=headers)
    assert response.status_code == 200
    assert response.json()['requested'] == top
    values = client.get('/api/settings').json()['saved']
    saved = client.put('/api/settings', json={**values, 'default_top':top}, headers=headers)
    assert saved.status_code == 200
    assert client.get('/api/settings').json()['saved']['default_top'] == top


def test_directory_scope_and_duration(runtime, client):
    from frameseek.media.bif import parse
    from frameseek.media.scope import directories
    selected = write_bif(runtime.settings.sources['sda'] / '剧集' / '子目录' / 'one.bif')
    write_bif(runtime.settings.sources['sda'] / '剧集备份' / 'other.bif')
    write_bif(runtime.settings.sources['sdc'] / '剧集' / 'same.bif')
    build(runtime)
    assert client.get('/api/search/directories').status_code == 401
    headers = login(client)
    listing = client.get('/api/search/directories', params={'source':'sda','q':'剧集'}).json()
    assert {item['directory'] for item in listing['directories']} == {'剧集','剧集/子目录','剧集备份'}
    assert next(item['bif_count'] for item in listing['directories'] if item['directory']=='剧集') == 1
    def search(**scope):
        return client.post('/api/search', data={'top':20,'collapse':'true',**scope}, files={'image':('scope.jpg',jpeg('red'),'image/jpeg')}, headers=headers)
    filtered = search(source='sda', directory='剧集')
    assert filtered.status_code == 200
    data = filtered.json()
    assert len(data['results']) == 1
    assert data['results'][0]['relpath'] == '剧集/子目录/one.bif'
    assert data['results'][0]['duration_ms'] == parse(selected)[-1].time_ms
    assert {row['source'] for row in search(source='sdc').json()['results']} == {'sdc'}
    assert len(search().json()['results']) == 3
    assert search(source='sda', directory='不存在').json()['results'] == []
    for scope in [{'source':'missing'}, {'directory':'剧集'}, {'source':'sda','directory':'../剧集'}, {'source':'sda','directory':'/剧集'}, {'source':'sda','directory':'剧集\\子目录'}]:
        assert search(**scope).status_code == 422
    history = client.get('/api/history/'+data['history_id']).json()['response']
    assert (history['source'], history['directory']) == ('sda','剧集')
    # Old history is supplemented without updating the persisted snapshot.
    import json
    legacy = {**history}
    legacy.pop('source'); legacy.pop('directory')
    for row in legacy['results']: row.pop('duration_ms')
    runtime.db.execute('UPDATE search_history SET response=? WHERE id=?', (json.dumps(legacy),data['history_id']))
    restored = client.get('/api/history/'+data['history_id']).json()['response']
    assert restored['source'] == restored['directory'] == ''
    assert restored['results'][0]['duration_ms'] == parse(selected)[-1].time_ms
    unknown = [{'version':'not-present'}, {'version':data['results'][0]['version'], 'duration_ms':0}]
    runtime.db.add_durations(unknown)
    assert unknown[0]['duration_ms'] is None
    assert unknown[1]['duration_ms'] == 0
    assert directories(runtime.db, keyword='%')['directories'] == []


def test_directory_list_limit_and_zero_duration(runtime):
    from frameseek.media.scope import directories
    class DirectoryDB:
        def rows(self, *args):
            return [{'source':'sda','relpath':f'dir-{number:03}/movie.bif'} for number in range(60)]
    listing = directories(DirectoryDB())
    assert len(listing['directories']) == 50
    assert listing['has_more'] is True
    assert len(directories(DirectoryDB(), keyword='dir-059')['directories']) == 1
    path = write_bif(runtime.settings.sources['sda'] / 'zero.bif')
    build(runtime)
    version = runtime.db.one('SELECT active_version FROM files')['active_version']
    runtime.db.execute('UPDATE frames SET time_ms=0 WHERE version=?', (version,))
    rows = [{'version':version}]
    runtime.db.add_durations(rows)
    assert rows[0]['duration_ms'] == 0


def test_directory_keyword_search_includes_all_matches(runtime, client):
    paths = [('sda','电影/目标/one.bif'), ('sda','目标备份/sub/two.bif'), ('sdc','目标/three.bif'), ('sda','其他/目标文件名.bif')]
    for source, path in paths: write_bif(runtime.settings.sources[source] / path)
    build(runtime)
    headers = login(client)
    def search(**scope):
        return client.post('/api/search', data={'top':20, **scope}, files={'image':('scope.jpg',jpeg('red'),'image/jpeg')}, headers=headers)
    response = search(directory_keyword='  目标  ')
    assert response.status_code == 200
    data = response.json()
    assert len(data['results']) == 3
    assert {row['relpath'] for row in data['results']} == {path for _,path in paths[:3]}
    assert len(search(source='sda', directory_keyword='目标').json()['results']) == 2
    assert search(directory_keyword='不存在').json()['results'] == []
    chosen = search(source='sda', directory='其他', directory_keyword='目标').json()
    assert len(chosen['results']) == 1
    assert chosen['directory_keyword'] == ''
    snapshot = client.get('/api/history/'+data['history_id']).json()['response']
    assert snapshot['directory_keyword'] == '目标'
    assert snapshot['directory'] == snapshot['source'] == ''
    assert search(directory_keyword='x'*257).status_code == 422


def test_auto_scan_settings_apply_without_restart(runtime, client):
    headers = login(client)
    original = client.get('/api/settings').json()['saved']
    values = {**original, 'auto_update':True, 'scan_interval_seconds':120, 'stable_seconds':15}
    response = client.put('/api/settings',json=values,headers=headers).json()
    assert response['restart_required'] is False
    assert runtime.settings.auto_update is True
    assert (runtime.settings.interval,runtime.settings.stable_seconds) == (120,15)
    status = client.get('/api/status').json()
    assert status['auto_update'] is True and status['scan_interval_seconds'] == 120
    assert 'completed_frames' in status['progress']
    changed = {**values, 'auto_update':False, 'cpu_threads':2}
    response = client.put('/api/settings',json=changed,headers=headers).json()
    assert response['restart_required'] is True
    assert runtime.settings.auto_update is False
    assert runtime.settings.cpu_threads == 4
    assert client.get('/api/settings').json()['running']['auto_update'] is False


def test_enabling_auto_update_wakes_and_processes_without_restart(runtime, client):
    import time
    path = write_bif(runtime.settings.sources['sda']/'hot-auto.bif')
    headers = login(client)
    values = client.get('/api/settings').json()['saved']
    assert runtime.settings.auto_update is False
    assert runtime.db.stats()['frames'] == 0
    enabled = {**values, 'auto_update':True, 'scan_interval_seconds':60, 'stable_seconds':0}
    assert client.put('/api/settings',json=enabled,headers=headers).json()['restart_required'] is False
    deadline = time.monotonic()+6
    while runtime.db.stats()['frames'] != 3 and time.monotonic()<deadline: time.sleep(.05)
    assert runtime.db.stats()['frames'] == 3
    assert runtime.worker.manual_active is False
    assert client.put('/api/settings',json={**enabled,'auto_update':False},headers=headers).status_code == 200
    deadline = time.monotonic()+3
    while runtime.worker.activity != 'disabled' and time.monotonic()<deadline: time.sleep(.05)
    assert runtime.worker.activity == 'disabled'
    write_bif(path, ('yellow','purple'))
    time.sleep(.15)
    assert runtime.db.stats()['frames'] == 3
    client.put('/api/settings',json=enabled,headers=headers)
    deadline = time.monotonic()+6
    while runtime.db.stats()['frames'] != 2 and time.monotonic()<deadline: time.sleep(.05)
    assert runtime.db.stats()['frames'] == 2
