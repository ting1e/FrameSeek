import json

import pytest

from conftest import build, write_bif
from imgsearch.emby import Emby, EmbySettings, EmbyError
from imgsearch.web import create_app
from fastapi.testclient import TestClient


def configured(db):
    service = Emby(db)
    service.save(EmbySettings(enabled=True, server_url='https://emby.example', api_key='secret-key',
                              user_id='u1', device_id='d1', source_paths={'sda':'/videos/a','sdc':'/videos/b'}))
    return service


def test_emby_config_secret_and_validation(runtime):
    service = configured(runtime.db)
    assert service.public()['api_key_set']
    assert 'secret-key' not in json.dumps(service.public())
    values = service.config().model_dump(); values['api_key'] = ''
    service.save(EmbySettings.model_validate(values))
    assert service.config().api_key == 'secret-key'
    for url in ['javascript:alert(1)', 'https://user:password@host', 'https://host?api_key=secret']:
        with pytest.raises(ValueError): EmbySettings(server_url=url)
    with pytest.raises(ValueError): EmbySettings(source_paths={'sda':'../escape'})


def test_emby_http_token_stays_in_header_and_errors_are_redacted(runtime, monkeypatch):
    import httpx
    service = configured(runtime.db)
    def handle(request):
        assert request.headers['X-Emby-Token'] == 'secret-key'
        assert 'secret-key' not in str(request.url)
        assert request.url.path.startswith('/emby/')
        return httpx.Response(401, json={'message':'secret-key'})
    original = httpx.Client
    monkeypatch.setattr('imgsearch.emby.httpx.Client', lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(EmbyError, match='授权失败') as error:
        service.clients()
    assert 'secret-key' not in str(error.value)


def test_emby_play_exact_path_timestamp_and_cached_mapping(runtime, monkeypatch):
    service = configured(runtime.db)
    commands, catalog_calls = [], []
    def request(config, method, route, **kwargs):
        assert config.api_key == 'secret-key'
        if route == '/Items':
            catalog_calls.append(1)
            return {'Items':[
                {'Id':'correct','Path':'/videos/a/folder/movie.mkv','MediaSources':[{'Id':'media-1','Path':'/videos/a/folder/movie.mkv'}]},
                {'Id':'other','Path':'/videos/b/folder/movie.mkv'}], 'TotalRecordCount':2}
        if route == '/Sessions':
            return [{'Id':'session','DeviceId':'d1','UserId':'u1','SupportsRemoteControl':True,'PlayableMediaTypes':['Video']}]
        commands.append((route,kwargs)); return None
    monkeypatch.setattr(service, 'request', request)
    row = {'source':'sda','relpath':'folder/movie-320-10.bif','time_ms':123456}
    assert service.play(row)['ok']
    assert commands[0][0] == '/Sessions/session/Playing'
    assert commands[0][1]['params'] == {'ItemIds':'correct','PlayCommand':'PlayNow','StartPositionTicks':1234560000}
    assert commands[0][1]['json']['MediaSourceId'] == 'media-1'
    service.play(row)
    assert len(catalog_calls) == 1
    service.play({**row,'relpath':'folder/movie.mkv-320-10.bif'})
    assert commands[-1][1]['params']['ItemIds'] == 'correct'


def test_emby_does_not_guess_missing_ambiguous_or_offline_target(runtime, monkeypatch):
    service = configured(runtime.db)
    payload = {'Items':[{'Id':'a','Path':'/videos/a/movie.mkv'}, {'Id':'b','Path':'/videos/a/movie.mp4'}], 'TotalRecordCount':2}
    def request(config, method, route, **kwargs):
        assert method == 'GET', 'Invalid matches must never start playback'
        return payload if route == '/Items' else []
    monkeypatch.setattr(service,'request',request)
    with pytest.raises(EmbyError, match='唯一'):
        service.play({'source':'sda','relpath':'movie-320-10.bif','time_ms':0})
    with pytest.raises(EmbyError, match='唯一'):
        service.play({'source':'sdc','relpath':'movie-320-10.bif','time_ms':0})
    runtime.db.execute("DELETE FROM emby_items WHERE item_id='b'")
    with pytest.raises(EmbyError, match='未在线'):
        service.play({'source':'sda','relpath':'movie-320-10.bif','time_ms':0})


def test_emby_api_auth_csrf_stale_frame_and_play(runtime, monkeypatch):
    path = write_bif(runtime.settings.sources['sda']/'movie-320-10.bif')
    build(runtime)
    app = create_app(runtime.settings, runtime)
    with TestClient(app) as client:
        assert client.get('/api/emby/settings').status_code == 401
        assert client.get('/api/emby/clients').status_code == 401
        login = client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers = {'X-CSRF-Token':login.json()['csrf']}
        frame = runtime.db.one('SELECT id FROM frames ORDER BY frame_no')['id']
        assert client.post('/api/emby/play/'+frame).status_code == 403
        assert client.put('/api/emby/settings',json={}).status_code == 403
        values = EmbySettings(server_url='https://emby.example',api_key='secret-key').model_dump()
        result = client.put('/api/emby/settings',json=values,headers=headers)
        assert result.status_code == 200 and 'secret-key' not in result.text
        assert 'secret-key' not in client.get('/api/emby/settings').text
        called=[]
        monkeypatch.setattr(app.state.emby,'play',lambda row:called.append(row) or {'ok':True})
        assert client.post('/api/emby/play/'+frame,headers=headers).json()['ok']
        path.unlink()
        assert client.post('/api/emby/play/'+frame,headers=headers).status_code == 410
        assert len(called)==1


@pytest.mark.parametrize('container,codec,direct', [('mp4','h264',True),('mkv','hevc',False)])
def test_new_window_stream_seeks_or_transcodes_without_device(runtime, monkeypatch, container, codec, direct):
    service = configured(runtime.db)
    values = service.config().model_dump(); values['device_id'] = ''
    service.save(EmbySettings.model_validate(values))
    def request(config, method, route, **kwargs):
        if route == '/Items':
            return {'Items':[{'Id':'a','Path':'/videos/a/movie.'+container,'MediaSources':[{'Id':'media','Path':'/videos/a/movie.'+container}]}],'TotalRecordCount':1}
        assert route == '/Items/a/PlaybackInfo'
        return {'MediaSources':[{'Id':'media','Container':container,'MediaStreams':[{'Type':'Video','Codec':codec},{'Type':'Audio','Codec':'aac'}]}]}
    monkeypatch.setattr(service,'request',request)
    result = service.prepare({'source':'sda','relpath':'movie-320-10.bif','time_ms':123456})
    assert 'secret-key' not in json.dumps(result)
    token = result['stream_url'].rsplit('/',1)[-1]
    stream = service.stream(token)
    assert stream['direct'] is direct
    assert result['seek_seconds'] == (123.456 if direct else 0)
    assert result['offset_seconds'] == (0 if direct else 123.456)
    if not direct:
        assert stream['params']['StartTimeTicks'] == '1234560000'
        assert stream['params']['AllowVideoStreamCopy'] == 'false'


def test_player_stream_is_authenticated_and_forwards_range_without_token(runtime, monkeypatch):
    import httpx,time
    path = write_bif(runtime.settings.sources['sda']/'movie-320-10.bif'); build(runtime)
    app = create_app(runtime.settings,runtime)
    service = app.state.emby
    service.save(EmbySettings(enabled=True,server_url='https://emby.example',api_key='secret-key',user_id='u1'))
    class VideoBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'fake-mp4'
    def transport(request):
        assert request.headers['X-Emby-Token']=='secret-key'
        assert request.headers['Range']=='bytes=10-17'
        assert 'secret-key' not in str(request.url)
        return httpx.Response(206,headers={'Content-Type':'video/mp4','Content-Range':'bytes 10-17/100','Accept-Ranges':'bytes'},stream=VideoBytes())
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(transport),**kwargs))
    service.streams['local-token']={'config':service.config(),'route':'/Videos/a/stream.mp4','params':{},'expires':time.time()+100,'direct':True}
    with TestClient(app) as client:
        frame = runtime.db.one('SELECT id FROM frames')['id']
        assert client.get('/emby/player/'+frame).status_code==401
        assert client.get('/api/emby/stream/local-token').status_code==401
        login=client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        headers={'X-CSRF-Token':login.json()['csrf']}
        assert client.get('/emby/player/'+frame).status_code==200
        assert client.post('/api/emby/open/'+frame).status_code==403
        result=client.get('/api/emby/stream/local-token',headers={'Range':'bytes=10-17'})
        assert result.status_code==206 and result.content==b'fake-mp4'
        assert result.headers['content-range']=='bytes 10-17/100'
        assert 'secret-key' not in result.text
        path.unlink()
        assert client.post('/api/emby/open/'+frame,headers=headers).status_code==410
        assert client.post('/api/emby/stop/local-token',headers=headers).status_code==200
        assert client.get('/api/emby/stream/local-token').status_code==410


def test_web_mode_waits_then_controls_only_unique_web_session(runtime, monkeypatch):
    service = configured(runtime.db)
    values = service.config().model_dump(); values.update(playback_mode='emby_web',device_id='')
    service.save(EmbySettings.model_validate(values))
    assert service.public()['playback_mode']=='emby_web'
    sessions, commands = [], []
    def request(config, method, route, **kwargs):
        if route == '/Items':
            return {'Items':[{'Id':'video','Path':'/videos/a/movie.mkv'}],'TotalRecordCount':1}
        if route == '/Sessions': return sessions
        commands.append((route,kwargs)); return None
    monkeypatch.setattr(service,'request',request)
    row={'source':'sda','relpath':'movie-320-10.bif','time_ms':42000}
    assert service.play(row,web_only=True)['waiting']
    sessions.append({'Id':'tv','DeviceId':'tv','UserId':'u1','Client':'Emby Theater','SupportsRemoteControl':True,'PlayableMediaTypes':['Video']})
    assert service.play(row,web_only=True)['waiting']
    web={'Id':'web1','DeviceId':'browser','UserId':'u1','Client':'Emby Web','SupportsRemoteControl':True,'PlayableMediaTypes':['Video']}
    sessions.append(web)
    assert service.play(row,web_only=True)['ok']
    assert commands[-1][0]=='/Sessions/web1/Playing'
    assert commands[-1][1]['params']['StartPositionTicks']==420000000
    sessions.append({**web,'Id':'web2','DeviceId':'other-browser'})
    with pytest.raises(EmbyError,match='不唯一'): service.play(row,web_only=True)
    values['device_id']='browser'; service.save(EmbySettings.model_validate(values))
    assert service.play(row,web_only=True)['ok']
    sessions.append({**web,'Id':'shared-tab'})
    with pytest.raises(EmbyError,match='不唯一'): service.play(row,web_only=True)


def test_playback_mode_defaults_and_validation(runtime):
    assert EmbySettings().playback_mode=='standalone'
    with pytest.raises(ValueError): EmbySettings(playback_mode='unknown')
    service=configured(runtime.db)
    values=service.config().model_dump(); values.pop('playback_mode')
    runtime.db.execute("UPDATE meta SET value=? WHERE key='emby_settings'",(json.dumps(values),))
    assert service.public()['playback_mode']=='standalone'


def test_web_target_new_recent_shared_and_offline_fallback(runtime, monkeypatch):
    service=configured(runtime.db)
    values=service.config().model_dump(); values.update(playback_mode='emby_web',web_target='new_tab',device_id='')
    service.save(EmbySettings.model_validate(values))
    def session(id,activity,user='u1',client='Emby Web'):
        return {'Id':id,'DeviceId':id,'DeviceName':id,'UserId':user,'Client':client,'LastActivityDate':activity,
                'SupportsRemoteControl':True,'PlayableMediaTypes':['Video']}
    sessions=[session('old','2026-10-06T10:00:00Z')]
    commands=[]
    def request(config,method,route,**kwargs):
        if route=='/Sessions': return sessions
        if route=='/Items': return {'Items':[{'Id':'video','Path':'/videos/a/movie.mkv'}],'TotalRecordCount':1}
        commands.append(route)
    monkeypatch.setattr(service,'request',request)
    row={'id':'frame','source':'sda','relpath':'movie-320-10.bif','time_ms':2000}
    plan=service.web_begin(row)
    assert plan['open_web']
    assert service.web_poll(row,plan['ticket'])['waiting']
    sessions.append(session('new','2026-10-06T10:02:00Z'))
    sessions.append(session('tv','2026-10-06T11:00:00Z',client='Emby Theater'))
    sessions.append(session('other-user','2026-10-06T12:00:00Z',user='u2'))
    result=service.web_poll(row,plan['ticket'])
    assert result['target_device']=='new' and commands[-1]=='/Sessions/new/Playing'
    service.web_poll(row,plan['ticket'])
    assert len(commands)==1  # Repeated poll cannot issue playback twice.
    values['web_target']='recent'; service.save(EmbySettings.model_validate(values))
    plan=service.web_begin(row)
    assert not plan['open_web']
    sessions.append(session('later','2026-10-06T10:03:00Z'))
    result=service.web_poll(row,plan['ticket'])
    assert result['target_device']=='new'  # Pin the chosen recent session for this click.
    sessions.clear()
    plan=service.web_begin(row)
    assert plan['open_web']  # No existing page means opening a new one.
    sessions.append(session('shared','2026-10-06T10:04:00Z'))
    plan=service.web_begin(row)
    assert not plan['open_web']
    sessions.clear()
    assert service.web_poll(row,plan['ticket'])['open_web']  # Existing page went offline.
    values['web_target']='new_tab'; service.save(EmbySettings.model_validate(values))
    sessions.append(session('shared','2026-10-06T10:04:00Z'))
    plan=service.web_begin(row)
    service.web_intents[plan['ticket']]['created']-=4
    sessions[0]['LastActivityDate']='2026-10-06T10:05:00Z'
    assert service.web_poll(row,plan['ticket'])['shared_session']
    with pytest.raises(EmbyError,match='失效'): service.web_poll({**row,'id':'wrong'},plan['ticket'])
