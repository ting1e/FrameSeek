import pytest
from fastapi.testclient import TestClient

from conftest import build, write_bif
from frameseek.integrations.emby import EmbyError, EmbySettings
from frameseek.web import create_app


@pytest.mark.parametrize('mode,route', [('standalone','open'),('emby_web','web-begin')])
def test_emby_misses_fall_back_to_same_directory_mp4(runtime, monkeypatch, mode, route):
    path = write_bif(runtime.settings.sources['sda']/'中文目录'/'片名.mp4-320-10.bif')
    video = path.with_name('片名.mp4')
    video.write_bytes(b'0123456789abcdef')
    write_bif(runtime.settings.sources['sdc']/'中文目录'/'片名.mp4-320-10.bif')
    build(runtime)
    app = create_app(runtime.settings,runtime)
    app.state.emby.save(EmbySettings(enabled=True,server_url='https://emby.example',api_key='secret',user_id='u',playback_mode=mode))
    requests=[]
    def request(config, method, route, **kwargs):
        requests.append((method,route,kwargs))
        assert method=='GET' and route=='/Items'
        return {'Items':[], 'TotalRecordCount':0}
    monkeypatch.setattr(app.state.emby,'request',request)
    frame=runtime.db.one("SELECT fr.id FROM frames fr JOIN versions v ON v.id=fr.version JOIN files f ON f.id=v.file_id WHERE f.source='sda' AND fr.frame_no=1")['id']
    with TestClient(app) as client:
        assert client.get('/api/local/stream/'+frame).status_code==401
        assert client.get('/player/local/'+frame).status_code==401
        login=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers={'X-CSRF-Token':login.json()['csrf']}
        assert client.post('/api/local/open/'+frame).status_code==403
        result=client.post('/api/emby/'+route+'/'+frame,headers=headers)
        assert result.status_code==200,result.text
        data=result.json()
        assert data['local'] and data['seek_seconds']==10 and data['name']=='片名.mp4'
        assert data['stop_url']=='' and not data['transcoding']
        assert requests and any(call[2].get('params',{}).get('SearchTerm') for call in requests)
        assert client.get(data['player_url']).status_code==200
        before=len(requests)
        assert client.post('/api/local/open/'+frame,headers=headers).json()['local']
        stream=client.get(data['stream_url'],headers={'Range':'bytes=4-7'})
        assert stream.status_code==206 and stream.content==b'4567'
        assert stream.headers['content-range']=='bytes 4-7/16'
        assert client.get(data['stream_url'],headers={'Range':'bytes=999-'}).status_code==416
        assert len(requests)==before  # The local player does not call Emby again.
        other=runtime.db.one("SELECT fr.id FROM frames fr JOIN versions v ON v.id=fr.version JOIN files f ON f.id=v.file_id WHERE f.source='sdc'")['id']
        assert client.post('/api/local/open/'+other,headers=headers).status_code==409
        video.rename(video.with_name('片名花絮.mp4'))
        assert client.post('/api/local/open/'+frame,headers=headers).status_code==409
        path.unlink()
        assert client.get(data['stream_url']).status_code==410


def test_emby_connection_failure_does_not_trigger_no_match_fallback(runtime, monkeypatch):
    path=write_bif(runtime.settings.sources['sda']/'movie-320-10.bif')
    path.with_name('movie.mp4').write_bytes(b'video')
    build(runtime)
    app=create_app(runtime.settings,runtime)
    def failed(row):
        raise EmbyError('Emby 连接失败')
    monkeypatch.setattr(app.state.emby,'prepare',failed)
    with TestClient(app) as client:
        login=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        frame=runtime.db.one('SELECT id FROM frames')['id']
        response=client.post('/api/emby/open/'+frame,headers={'X-CSRF-Token':login.json()['csrf']})
        assert response.status_code==409 and response.json()['detail']=='Emby 连接失败'
