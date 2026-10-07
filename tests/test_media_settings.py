import json
import io
import shutil

from PIL import Image

from fastapi.testclient import TestClient
from conftest import build, jpeg, write_bif
from frameseek.core.config import Settings
from frameseek.media.directories import plan_directories, remember_sources
from frameseek.engine.search import Runtime
from frameseek.web import create_app


def login(client):
    result = client.post('/api/login', json={'username':'admin', 'password':'testing-secret'})
    assert result.status_code == 200
    return {'X-CSRF-Token':result.json()['csrf']}


def save(client, headers, paths):
    values = client.get('/api/settings').json()['saved']
    return client.put('/api/settings', json={**values, 'monitor_directories':paths}, headers=headers)


def test_fresh_site_accepts_full_directories_and_restores_without_env(runtime, tmp_path, monkeypatch):
    monkeypatch.delenv('IMGS_MEDIA_DIRECTORIES', raising=False)
    monkeypatch.delenv('IMGS_SOURCES', raising=False)
    settings = Settings(data=tmp_path/'fresh-data', model=runtime.settings.model,
                        password_hash=runtime.settings.password_hash, session_secret='a'*64,
                        qdrant_url=':memory:', secure_cookie=False, stable_seconds=0)
    fresh = Runtime(settings, embedder=runtime.embedder)
    media = tmp_path/'new-media'
    write_bif(media/'中文目录'/'one.bif')
    with TestClient(create_app(settings, fresh)) as client:
        headers = login(client)
        assert client.get('/api/settings').json()['monitor_directories'] == []
        assert settings.sources == {}
        assert save(client, headers, [media.as_posix()]).status_code == 200
        assert client.get('/api/settings').json()['monitor_directories'] == [media.as_posix()]
        build(fresh)
        result = client.post('/api/search',files={'image':('shot.jpg',jpeg('red'))},headers=headers)
        assert result.status_code == 200 and result.json()['results'][0]['relpath'] == '中文目录/one.bif'
        restored = Settings(data=settings.data, model=settings.model)
        assert restored.sources == settings.sources
        assert restored.monitor_folders == settings.monitor_folders


def test_existing_vectors_survive_subdirectory_selection_and_stopping_monitoring(runtime):
    root = runtime.settings.sources['sda']
    write_bif(root/'watched'/'one.bif')
    build(runtime)
    frames = runtime.db.rows('SELECT id,version FROM frames ORDER BY id')
    count = runtime.get_store().count()
    calls = runtime.embedder.calls
    with TestClient(create_app(runtime.settings, runtime)) as client:
        headers = login(client)
        assert save(client, headers, [(root/'watched').as_posix()]).status_code == 200
        assert runtime.settings.monitor_folders == [{'source':'sda', 'path':'watched'}]
        assert save(client, headers, []).status_code == 200
        assert client.get('/api/settings').json()['monitor_directories'] == []
        result = client.post('/api/search',files={'image':('shot.jpg',jpeg('red'))},headers=headers)
        assert result.json()['results'][0]['source'] == 'sda'
        assert runtime.db.rows('SELECT id,version FROM frames ORDER BY id') == frames
        assert runtime.get_store().count() == count
        assert runtime.embedder.calls == calls + 1  # Only this screenshot was encoded.


def test_parent_selection_keeps_child_ids_and_does_not_index_child_twice(runtime):
    child = runtime.settings.sources['sda']
    write_bif(child/'original.bif')
    build(runtime)
    original = runtime.db.one("SELECT id,active_version FROM files WHERE source='sda'")
    calls = runtime.embedder.calls
    write_bif(child.parent/'outside.bif')
    with TestClient(create_app(runtime.settings, runtime)) as client:
        headers = login(client)
        assert save(client, headers, [child.parent.as_posix()]).status_code == 200
        assert client.get('/api/settings').json()['monitor_directories'] == [child.parent.as_posix()]
        build(runtime)
        assert runtime.db.one("SELECT id,active_version FROM files WHERE source='sda'") == original
        assert runtime.db.one("SELECT COUNT(*) AS n FROM files WHERE relpath LIKE '%original.bif'")['n'] == 1
        assert runtime.db.stats()['ready_files'] == 2
        assert runtime.embedder.calls == calls + 3 // runtime.settings.batch + bool(3 % runtime.settings.batch)


def test_unavailable_directory_or_traversal_does_not_change_settings(runtime, tmp_path):
    with TestClient(create_app(runtime.settings, runtime)) as client:
        headers = login(client)
        before = client.get('/api/settings').json()['saved']
        registry = runtime.db.one("SELECT value FROM meta WHERE key='media_registry'")
        for path in ('relative/folder', (tmp_path/'missing').as_posix(), (tmp_path/'..'/'outside').as_posix(), '/mnt/*'):
            assert save(client, headers, [path]).status_code == 422
            assert client.get('/api/settings').json()['saved'] == before
            assert runtime.db.one("SELECT value FROM meta WHERE key='media_registry'") == registry


def test_legacy_alias_maps_to_local_root_and_portable_registry_selects_available_mount(runtime, tmp_path, monkeypatch):
    root = runtime.settings.sources['sda']
    write_bif(root/'series'/'one.bif')
    build(runtime)
    remember_sources(runtime.settings, runtime.db, {'sda':'/mnt/test-library'})
    sources, selected, _ = plan_directories(runtime.settings, runtime.db, ['/mnt/test-library/series'])
    assert sources['sda'] == root and selected == [{'source':'sda','path':'series'}]
    mounted = tmp_path/'mounted-library'
    shutil.copytree(root, mounted, copy_function=shutil.copy2)
    registry = json.loads(runtime.db.one("SELECT value FROM meta WHERE key='media_registry'")['value'])
    registry['sda'] = {'root':str(tmp_path/'unavailable-local-copy'), 'aliases':[mounted.as_posix()]}
    runtime.db.execute("UPDATE meta SET value=? WHERE key='media_registry'", (json.dumps(registry),))
    monkeypatch.delenv('IMGS_MEDIA_DIRECTORIES', raising=False)
    monkeypatch.delenv('IMGS_SOURCES', raising=False)
    restored = Settings(data=runtime.settings.data, model=runtime.settings.model)
    assert restored.sources['sda'] == mounted
    old_settings = runtime.settings
    runtime.settings = restored
    try:
        with Image.open(io.BytesIO(jpeg('red'))) as image:
            result = runtime.query(image, 20)
        assert result['results'][0]['source'] == 'sda'
    finally:
        runtime.settings = old_settings


def test_older_index_without_registry_can_recover_root_from_selected_subdirectory(runtime, tmp_path):
    root = tmp_path/'bif'/'sda'
    runtime.settings.sources = {'sda':root}
    runtime.settings.monitor_folders = [{'source':'sda','path':''}]
    write_bif(root/'series'/'old.bif')
    build(runtime)
    original = runtime.db.rows('SELECT id,active_version FROM files')
    calls = runtime.embedder.calls
    runtime.db.execute("DELETE FROM meta WHERE key='media_registry'")
    runtime.settings.sources = {}
    with TestClient(create_app(runtime.settings, runtime)) as client:
        headers = login(client)
        assert save(client, headers, [(root/'series').as_posix()]).status_code == 200
        assert runtime.settings.sources == {'sda':root}
        assert runtime.settings.monitor_folders == [{'source':'sda','path':'series'}]
        build(runtime)
        assert runtime.db.rows('SELECT id,active_version FROM files') == original
        assert runtime.embedder.calls == calls
