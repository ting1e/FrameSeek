import sqlite3
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import build, jpeg, write_bif
from imgsearch.db import Database, SCHEMA
from imgsearch.emby import Emby, EmbyError
from imgsearch.indexer import Indexer
from imgsearch.web import create_app


def save_image(path, color='red', format=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new('RGB', (80, 45), color) as image:
        image.save(path, format=format)
    return path


@pytest.mark.parametrize('extension', ['jpg', 'jpeg', 'PNG', 'webp', 'bmp', 'gif', 'tif', 'tiff'])
def test_image_formats_share_search_index(runtime, extension):
    save_image(runtime.settings.sources['sda'] / '图片' / f'example.{extension}')
    write_bif(runtime.settings.sources['sdc'] / 'movie.bif', ('red', 'blue'))
    build(runtime)
    with Image.new('RGB', (80, 45), 'red') as query:
        all_rows = runtime.query(query, 20)['results']
        images = runtime.query(query, 20, media_type='image')['results']
        videos = runtime.query(query, 20, media_type='bif')['results']
        scoped = runtime.query(query, 20, media_type='image', source='sda', directory='图片')['results']
    assert {row['media_type'] for row in all_rows} == {'bif', 'image'}
    assert len(images) == len(scoped) == 1
    assert images[0]['duration_ms'] is None and images[0]['time_ms'] == 0
    assert images[0]['frame_no'] == 0 and images[0]['group_count'] == 1
    assert len(videos) == 1 and videos[0]['media_type'] == 'bif'
    assert videos[0]['duration_ms'] == 10000
    assert runtime.get_store().count() == 3


def test_old_payloads_stay_searchable_without_reencoding(runtime):
    write_bif(runtime.settings.sources['sda'] / 'legacy.bif')
    save_image(runtime.settings.sources['sda'] / 'new.png')
    build(runtime)
    store = runtime.get_store()
    ids = [row['id'] for row in runtime.db.rows("SELECT fr.id FROM frames fr JOIN files f ON f.active_version=fr.version WHERE f.media_type='bif'")]
    store.client.delete_payload(store.collection, keys=['media_type'], points=ids)
    calls = runtime.embedder.calls
    assert runtime.get_indexer().scan(True)['queued'] == 0
    assert runtime.embedder.calls == calls
    with Image.new('RGB', (80, 45), 'red') as query:
        assert len(runtime.query(query, 20, media_type='bif')['results']) == 1
        assert len(runtime.query(query, 20, media_type='image')['results']) == 1
        assert len(runtime.query(query, 20)['results']) == 2


def test_image_changes_delete_and_resume_without_reembedding(runtime, monkeypatch):
    path = save_image(runtime.settings.sources['sda'] / 'change.png')
    indexer = runtime.get_indexer()
    indexer.scan(True)
    job = indexer.pending()[0]
    upsert = indexer.store.upsert
    monkeypatch.setattr(indexer.store, 'upsert', lambda *_: (_ for _ in ()).throw(ConnectionError('interrupted')))
    with pytest.raises(ConnectionError):
        indexer.process_version(job['id'])
    calls = runtime.embedder.calls
    monkeypatch.setattr(indexer.store, 'upsert', upsert)
    resumed = Indexer(runtime.settings, runtime.db, runtime.embedder, runtime.get_store())
    resumed.process_version(job['id'])
    assert runtime.embedder.calls == calls
    old_id = runtime.db.one('SELECT id FROM frames')['id']
    runtime.settings.stable_seconds = 60
    save_image(path, 'blue')
    assert indexer.scan()['modified'] == 1
    assert runtime.db.published_frame(old_id) is None
    assert not indexer.pending()
    runtime.db.execute('UPDATE files SET stable_since=stable_since-61')
    assert indexer.scan()['queued'] == 1
    resumed.process_version(resumed.pending()[0]['id'])
    resumed.cleanup()
    assert runtime.get_store().count() == 1
    path.unlink()
    assert indexer.scan()['deleted'] == 1
    resumed.cleanup()
    assert runtime.get_store().count() == 0


def test_bad_images_do_not_stop_other_files(runtime):
    root = runtime.settings.sources['sda']
    (root / 'bad.png').write_bytes(b'invalid image')
    save_image(root / 'good.png')
    (root / 'ignore.txt').write_text('not an image')
    with (root / 'too-large.jpg').open('wb') as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    build(runtime)
    assert runtime.db.stats()['files'] == 3
    assert runtime.db.stats()['frames'] == 1
    assert runtime.db.stats()['bad_frames'] == 1
    assert runtime.db.stats()['errors'] == 1


def test_image_preview_filter_history_and_directory_counts(runtime):
    save_image(runtime.settings.sources['sda'] / '同名' / 'photo.tiff')
    save_image(runtime.settings.sources['sdc'] / '同名' / 'photo.png')
    write_bif(runtime.settings.sources['sda'] / '同名' / 'video.bif')
    build(runtime)
    with TestClient(create_app(runtime.settings, runtime)) as client:
        login = client.post('/api/login', json={'username':'admin', 'password':'testing-secret'})
        headers = {'X-CSRF-Token':login.json()['csrf']}
        directories = client.get('/api/search/directories', params={'media_type':'image'}).json()['directories']
        assert len(directories) == 2
        assert all(row['image_count'] == row['file_count'] == 1 and row['bif_count'] == 0 for row in directories)
        response = client.post('/api/search', data={'media_type':'image', 'directory_keyword':'同名'},
                               files={'image':('shot.jpg', jpeg('red'))}, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result['media_type'] == 'image' and len(result['results']) == 2
        winner = result['results'][0]
        preview = client.get(winner['preview_url'])
        assert preview.status_code == 200 and preview.headers['content-type'] == 'image/jpeg'
        assert preview.content.startswith(b'\xff\xd8')
        nearby = client.get(winner['preview_url'] + '/neighbors').json()
        assert nearby['media_type'] == 'image' and len(nearby['frames']) == 1
        history = client.get('/api/history/' + result['history_id']).json()['response']
        assert history['media_type'] == 'image' and history['directory_keyword'] == '同名'
        assert all(row['duration_ms'] is None for row in history['results'])
        for value in ['images', 'unknown']:
            assert client.post('/api/search', data={'media_type':value}, files={'image':('q.jpg',jpeg('red'))}, headers=headers).status_code == 422
        assert client.get('/api/search/directories?media_type=invalid').status_code == 422
        assert client.post('/api/search', data={'media_type':'image','source':'sda','directory':'missing'}, files={'image':('q.jpg',jpeg('red'))}, headers=headers).json()['results'] == []
        # Old history without a type remains a whole-library search.
        runtime.db.save_search('old-history', 'old.jpg', jpeg('red'), {'results':[], 'returned':0})
        assert client.get('/api/history/old-history').json()['response']['media_type'] == 'all'
        root = runtime.settings.sources[winner['source']]
        (root / winner['relpath']).write_bytes(b'changed')
        assert client.get(winner['preview_url']).status_code == 410
    with pytest.raises(EmbyError, match='普通图片'):
        Emby(runtime.db).resolve(winner)


def test_sqlite_type_migration_preserves_ids_and_versions(tmp_path):
    path = tmp_path / 'old.sqlite'
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO files(id,source,relpath,size,mtime_ns,stable_since,seen,active_version) VALUES('existing','sda','movie.bif',123,1,0,'old','active')")
    db = Database(path)
    row = db.one('SELECT * FROM files')
    assert row['id'] == 'existing' and row['active_version'] == 'active' and row['media_type'] == 'bif'
    assert Database(path).one('SELECT COUNT(*) AS n FROM files')['n'] == 1


def test_animated_image_indexes_first_frame_and_exif_orientation(runtime):
    path = runtime.settings.sources['sda'] / 'animated.gif'
    red, blue = Image.new('RGB', (80,45), 'red'), Image.new('RGB', (80,45), 'blue')
    red.save(path, save_all=True, append_images=[blue], duration=100)
    red.close(); blue.close()
    build(runtime)
    assert runtime.get_store().count() == 1
    row = runtime.db.one('SELECT * FROM frames')
    from imgsearch.decode import decode_frame
    _, decoded, error = decode_frame(path, {**row, 'media_type':'image'})
    assert not error and decoded.getpixel((0,0)) == (255,0,0)
    decoded.close()

    photo = runtime.settings.sources['sda'] / 'rotated.jpg'
    with Image.new('RGB', (80,45), 'red') as image:
        exif = Image.Exif(); exif[274] = 6
        image.save(photo, exif=exif)
    _, decoded, error = decode_frame(photo, {'media_type':'image'})
    assert not error and decoded.size == (45,80)
    decoded.close()


@pytest.mark.skipif(os.getenv('IMGS_SERVER_TEST') != '1', reason='Local Qdrant integration is opt-in')
def test_real_server_mixed_types_and_legacy_payload(runtime):
    from dotenv import dotenv_values
    from imgsearch.vectors import VectorStore
    local = dotenv_values('.env')
    runtime.settings.qdrant_url = local['IMGS_QDRANT_URL']
    runtime.settings.qdrant_key = local['IMGS_QDRANT_KEY']
    assert '127.0.0.1' in runtime.settings.qdrant_url
    store = VectorStore(runtime.settings, uuid.uuid4().hex)
    store.ensure()
    runtime.store = store
    try:
        save_image(runtime.settings.sources['sda'] / 'images' / 'photo.png')
        write_bif(runtime.settings.sources['sda'] / 'videos' / 'movie.bif')
        build(runtime)
        ids = [row['id'] for row in runtime.db.rows("SELECT fr.id FROM frames fr JOIN files f ON f.active_version=fr.version WHERE f.media_type='bif'")]
        store.client.delete_payload(store.collection, keys=['media_type'], points=ids, wait=True)
        for mode in ['low_memory', 'high_memory']:
            runtime.settings.mode = mode
            store.ensure()
            with Image.new('RGB', (80,45), 'red') as image:
                videos = runtime.query(image, 20, media_type='bif')['results']
                stills = runtime.query(image, 20, media_type='image')['results']
                exact = runtime.query(image, 20, exact=True, media_type='image')['results']
            assert len(videos) == len(stills) == 1
            assert stills[0]['id'] == exact[0]['id']
            assert 'media_type' in store.client.get_collection(store.collection).payload_schema
    finally:
        store.client.delete_collection(store.collection)
