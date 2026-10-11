import io
import json
import time

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from conftest import build, write_bif
from frameseek.engine.embedding_api import EmbeddingService
from frameseek.core.preferences import requested
from frameseek.web import create_app


TOKEN = 'test-embedding-token-'+'a'*32


def login(client):
    r=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
    return {'X-CSRF-Token':r.json()['csrf']}


def enable(client,headers):
    r=client.put('/api/embedding-service',headers=headers,json={'enabled':True,'token':TOKEN})
    assert r.status_code==200
    return {'Authorization':'Bearer '+TOKEN}


def test_token_settings_rotation_and_disable(runtime):
    with TestClient(create_app(runtime.settings,runtime)) as client:
        assert client.get('/api/embedding-service').status_code==401
        assert client.post('/api/v1/embeddings/file',json={'path':'/x'}).status_code==503
        headers=login(client)
        assert client.put('/api/embedding-service',json={'generate':True}).status_code==403
        issued=client.put('/api/embedding-service',headers=headers,json={'generate':True}).json()
        assert len(issued['token'])>=32 and issued['token_set']
        assert 'token' not in client.get('/api/embedding-service').json()
        raw=runtime.db.one("SELECT value FROM meta WHERE key='embedding_api'")['value']
        assert issued['token'] not in raw
        auth=enable(client,headers)
        assert client.get('/api/v1/embeddings/jobs/missing',headers=auth).status_code==404
        assert client.get('/api/v1/embeddings/jobs/missing').status_code==401
        client.put('/api/embedding-service',headers=headers,json={'generate':True}).raise_for_status()
        assert client.get('/api/v1/embeddings/jobs/missing',headers=auth).status_code==401
        client.put('/api/embedding-service',headers=headers,json={'enabled':False}).raise_for_status()
        assert client.get('/api/v1/embeddings/jobs/missing',headers=auth).status_code==503


def test_uploaded_bif_reuses_index_and_downloads_npz(runtime):
    path=write_bif(runtime.settings.sources['sda']/'one.bif')
    build(runtime)
    calls=runtime.embedder.calls
    with TestClient(create_app(runtime.settings,runtime)) as client:
        headers=login(client);auth=enable(client,headers)
        result=client.post('/api/v1/embeddings/upload',headers=auth,files={'file':('one.bif',path.read_bytes())})
        assert result.status_code==200,result.text
        data=result.json()
        assert data['cache_hit'] and data['dimension']==1024 and len(data['frames'])==3
        assert data['frames'][1]['time_ms']==10000 and len(data['frames'][0]['embedding'])==1024
        assert runtime.embedder.calls==calls
        download=client.get(data['npz_url'],headers=auth)
        with np.load(io.BytesIO(download.content),allow_pickle=False) as saved:
            assert saved['vectors'].shape==(3,1024)
            assert saved['vectors'].dtype==np.float32
            assert saved['time_ms'].tolist()==[0,10000,20000]
            np.testing.assert_allclose(np.linalg.norm(saved['vectors'],axis=1),1,rtol=1e-5)
        assert client.get(data['npz_url']).status_code==401
        assert runtime.get_store().count()==3
        assert not list((runtime.embedding_api.root/'uploads').iterdir())


def test_image_generation_cache_and_changed_file(runtime):
    path=runtime.settings.sources['sda']/'one.png'
    Image.new('RGB',(24,12),'red').save(path)
    with TestClient(create_app(runtime.settings,runtime)) as client:
        auth=enable(client,login(client))
        first=client.post('/api/v1/embeddings/file',headers=auth,json={'path':str(path)}).json()
        assert first['status']=='ready' and not first['cache_hit'] and len(first['frames'])==1
        calls=runtime.embedder.calls
        upload=client.post('/api/v1/embeddings/upload',headers=auth,files={'file':('one.png',path.read_bytes())}).json()
        assert upload['cache_hit'] and runtime.embedder.calls==calls
        Image.new('RGB',(24,12),'blue').save(path)
        changed=client.post('/api/v1/embeddings/file',headers=auth,json={'path':str(path)}).json()
        assert not changed['cache_hit'] and changed['sha256']!=first['sha256']
        assert runtime.db.stats()['frames']==0  # API work never publishes search records.


def test_reuses_qdrant_when_npz_archives_are_missing(runtime):
    path=write_bif(runtime.settings.sources['sda']/'one.bif')
    build(runtime)
    for chunk in (runtime.settings.data/'vectors').rglob('*.npz'):
        chunk.unlink()
    calls=runtime.embedder.calls
    with TestClient(create_app(runtime.settings,runtime)) as client:
        auth=enable(client,login(client))
        response=client.post('/api/v1/embeddings/file',headers=auth,json={'path':str(path)})
        assert response.status_code==200,response.text
        assert response.json()['cache_hit'] and len(response.json()['frames'])==3
        assert runtime.embedder.calls==calls


def test_async_bif_bad_frames_and_cache_clear(runtime):
    path=write_bif(runtime.settings.sources['sda']/'many.bif',colors=('red',)*9,corrupt=3)
    with TestClient(create_app(runtime.settings,runtime)) as client:
        headers=login(client);auth=enable(client,headers)
        result=client.post('/api/v1/embeddings/upload',headers=auth,files={'file':('many.bif',path.read_bytes())})
        assert result.status_code==202
        ident=result.json()['id']
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=client.get('/api/v1/embeddings/jobs/'+ident,headers=auth).json()
            if result['status'] in {'ready','failed'}:break
            time.sleep(.03)
        assert result['status']=='ready',result
        assert result['vector_count']==8 and result['skipped_frames']==1
        data=client.get(result['result_url'],headers=auth).json()
        assert [f['frame_no'] for f in data['frames']]==[0,1,2,4,5,6,7,8]
        assert client.delete('/api/embedding-service/cache').status_code==403
        assert client.delete('/api/embedding-service/cache',headers=headers).status_code==200
        assert client.get('/api/v1/embeddings/jobs/'+ident,headers=auth).json()['status']=='cancelled'
        assert not list((runtime.embedding_api.root/'cache').iterdir())
        assert path.exists() and runtime.db.stats()['frames']==0


def test_paths_exclusions_and_bad_inputs(runtime,tmp_path):
    excluded=runtime.settings.sources['sda']/'private'
    excluded.mkdir();path=excluded/'one.png';Image.new('RGB',(8,8)).save(path)
    profile=requested(runtime.settings,runtime.db)
    profile['excluded_directories']=[str(excluded.as_posix())]
    runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)",(json.dumps(profile),))
    with TestClient(create_app(runtime.settings,runtime)) as client:
        auth=enable(client,login(client))
        assert client.post('/api/v1/embeddings/file',headers=auth,json={'path':str(path)}).status_code==403
        assert client.post('/api/v1/embeddings/file',headers=auth,json={'path':str(tmp_path/'outside.png')}).status_code==403
        assert client.post('/api/v1/embeddings/file',headers=auth,json={'path':'relative.png'}).status_code==422
        assert client.post('/api/v1/embeddings/upload',headers=auth,files={'file':('bad.png',b'not an image')}).status_code==422
        assert client.post('/api/v1/embeddings/upload',headers=auth,files={'file':('bad.bif',b'bad bif')}).status_code==422
        assert not list((runtime.embedding_api.root/'uploads').iterdir())


def test_partial_api_job_resumes_after_restart(runtime,monkeypatch):
    path=write_bif(runtime.settings.sources['sda']/'many.bif',colors=('red',)*9)
    service=runtime.embedding_api
    service.save(enabled=True,token=TOKEN)
    job=service.submit(path)
    embed=runtime.embedder.embed
    def interrupt(images,search=False):
        service.stop_event.set()
        return embed(images,search)
    monkeypatch.setattr(runtime.embedder,'embed',interrupt)
    service.process(job['id'])
    assert service.job(job['id'])['completed_frames']==2
    monkeypatch.setattr(runtime.embedder,'embed',embed)
    restarted=EmbeddingService(runtime)
    restarted.process(job['id'])
    data=json.loads(''.join(restarted.json_result(job['id'])))
    assert data['status']=='ready' and len(data['frames'])==9
    assert [f['frame_no'] for f in data['frames']]==list(range(9))


def test_api_upload_accepts_bif_above_search_image_limit(runtime):
    path=write_bif(runtime.settings.sources['sda']/'one.bif',colors=('red',))
    data=path.read_bytes()+bytes(12*1024*1024)
    with TestClient(create_app(runtime.settings,runtime)) as client:
        auth=enable(client,login(client))
        response=client.post('/api/v1/embeddings/upload',headers=auth,files={'file':('one.bif',data)})
        assert response.status_code==200,response.text
        assert len(response.json()['frames'])==1
