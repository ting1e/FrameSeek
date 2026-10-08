import time

from fastapi.testclient import TestClient
from conftest import write_bif
from frameseek.web import create_app


def eventually(check):
    end=time.monotonic()+8
    while time.monotonic()<end:
        if check():return
        time.sleep(.03)
    assert check()


def test_manual_scan_and_reparse_do_not_encode_until_requested(runtime):
    path=write_bif(runtime.settings.sources['sda']/'one.bif')
    other=runtime.settings.sources['sda']/'bad.bif';other.write_bytes(b'bad header')
    app=create_app(runtime.settings,runtime)
    with TestClient(app) as client:
        assert client.post('/api/updates/index').status_code==401
        login=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers={'X-CSRF-Token':login.json()['csrf']}
        assert client.post('/api/updates/reparse').status_code==403
        assert client.post('/api/updates/scan',headers=headers).status_code==200
        eventually(lambda:runtime.db.one("SELECT desired_version FROM files WHERE relpath='one.bif'") and runtime.db.one("SELECT desired_version FROM files WHERE relpath='one.bif'")['desired_version'])
        eventually(lambda:runtime.worker.activity=='disabled')
        assert runtime.db.stats()['frames']==0 and runtime.embedder.calls==0
        assert runtime.db.one("SELECT error FROM files WHERE relpath='bad.bif'")['error']
        write_bif(other,colors=('green',))
        assert client.post('/api/updates/reparse',headers=headers).status_code==200
        eventually(lambda:runtime.db.one("SELECT desired_version FROM files WHERE relpath='bad.bif'")['desired_version'])
        assert runtime.embedder.calls==0
        assert client.post('/api/updates/index',headers=headers).status_code==200
        eventually(lambda:runtime.db.stats()['frames']==4)
        eventually(lambda:not runtime.worker.manual_active)
        client.post('/api/updates/pause',headers=headers)
        assert client.post('/api/updates/reparse',headers=headers).status_code==409


def test_events_retain_full_text_paginate_and_clear_snapshot(runtime):
    text='完整处理记录'*500
    with runtime.db.connect() as c:
        c.executemany('INSERT INTO events(time,kind,message) VALUES(?,?,?)',[(1,'scan','old')]*2001)
    runtime.db.event('scan_error',text)
    assert runtime.db.one('SELECT COUNT(*) AS n FROM events')['n']==2002
    app=create_app(runtime.settings,runtime)
    with TestClient(app) as client:
        assert client.get('/api/updates/events').status_code==401
        login=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers={'X-CSRF-Token':login.json()['csrf']}
        data=client.get('/api/updates/events?limit=2').json()
        assert data['total']==2002 and data['items'][0]['message']==text and data['has_more']
        runtime.db.event('scan','new')
        page=client.get(f"/api/updates/events?offset=2&limit=2&snapshot={data['snapshot']}").json()
        assert page['total']==2002
        assert not {r['id'] for r in page['items']} & {r['id'] for r in data['items']}
        url=f"/api/updates/events?through_id={data['snapshot']}"
        assert client.delete(url).status_code==403
        assert client.delete(url,headers=headers).json()['removed']==2002
        assert client.get('/api/updates/events').json()['items'][0]['message']=='new'


def test_progress_scope_key_survives_new_jobs_and_failures_do_not_block_queue(runtime, monkeypatch):
    app=create_app(runtime.settings,runtime)
    captured=[]
    def update(completed, remaining, key, **kwargs):
        captured.append((key,kwargs))
        return {'remaining_frames':remaining,'completed_frames':completed}
    monkeypatch.setattr(runtime.progress,'update',update)
    with TestClient(app) as client:
        runtime.worker.stop()
        write_bif(runtime.settings.sources['sda']/'one.bif')
        (runtime.settings.sources['sda']/'bad.bif').write_bytes(b'bad')
        runtime.get_indexer().scan(True)
        client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        data=client.get('/api/status').json()
        assert data['tasks']['parse_failed']==1 and data['tasks']['queued']==1
        assert captured[-1][1]['blocked'] is False
        write_bif(runtime.settings.sources['sda']/'two.bif');runtime.get_indexer().scan(True)
        client.get('/api/status')
        assert captured[0][0]==captured[-1][0]


def test_pause_encoding_allows_other_tasks_and_resumes_cursor(runtime):
    import threading
    write_bif(runtime.settings.sources['sda']/'one.bif', colors=('red',)*6)
    indexer=runtime.get_indexer();indexer.scan(True)
    entered=threading.Event();release=threading.Event()
    original=runtime.embedder.embed
    def delayed(images, search=False):
        entered.set();assert release.wait(8)
        return original(images, search)
    runtime.embedder.embed=delayed
    app=create_app(runtime.settings,runtime)
    with TestClient(app) as client:
        login=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers={'X-CSRF-Token':login.json()['csrf']}
        client.post('/api/updates/index',headers=headers)
        assert entered.wait(8)
        assert client.post('/api/updates/pause-current',headers=headers).status_code==200
        release.set()
        eventually(lambda:runtime.worker.activity=='task_paused')
        saved=runtime.db.one("SELECT * FROM versions")
        assert saved['cursor']==2 and saved['status']=='pending'
        write_bif(runtime.settings.sources['sda']/'two.bif',colors=('blue',))
        assert client.post('/api/updates/scan',headers=headers).status_code==200
        eventually(lambda:runtime.db.one("SELECT desired_version FROM files WHERE relpath='two.bif'") is not None)
        assert client.post('/api/updates/index',headers=headers).status_code==200
        eventually(lambda:runtime.db.stats()['frames']==1)
        assert runtime.db.one('SELECT cursor FROM versions WHERE id=?',(saved['id'],))['cursor']==2
        assert client.get('/api/status').json()['suspended_task']['version']==saved['id']
        assert client.post('/api/updates/resume-current',headers=headers).status_code==200
        eventually(lambda:runtime.db.stats()['frames']==7)
        assert client.get('/api/status').json()['suspended_task'] is None


def test_interrupted_scan_does_not_delete_unvisited_files(runtime, monkeypatch):
    import threading
    write_bif(runtime.settings.sources['sda']/'one.bif')
    write_bif(runtime.settings.sources['sda']/'two.bif')
    indexer=runtime.get_indexer();indexer.scan(True)
    interrupted=threading.Event()
    original=runtime.db.execute
    def execute(sql,args=()):
        result=original(sql,args)
        if sql.startswith('UPDATE files SET seen='):
            interrupted.set()
        return result
    monkeypatch.setattr(runtime.db,'execute',execute)
    result=indexer.scan(interrupt=interrupted)
    assert result['interrupted'] is True
    assert runtime.db.one("SELECT COUNT(*) AS n FROM files WHERE status='deleted'")['n']==0
