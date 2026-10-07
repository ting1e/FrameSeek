from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from frameseek.core.auth import Auth
from frameseek.web import create_app


def request(address='client', token=''):
    return Request({'type':'http', 'method':'GET', 'path':'/', 'scheme':'http',
                    'server':('testserver',80), 'client':(address,1234),
                    'headers':[(b'cookie', ('imgsearch_session='+token).encode())] if token else []})


def test_concurrent_bad_logins_cannot_bypass_limit(runtime, monkeypatch):
    auth = Auth(runtime.settings, runtime.db)
    barrier = threading.Barrier(8)
    calls = []
    def wrong_password(*args):
        calls.append(1)
        time.sleep(.03)
        return False
    monkeypatch.setattr('frameseek.core.auth.verify_password', wrong_password)
    def attempt(_):
        barrier.wait()
        try:
            auth.login(request(), 'admin', 'wrong')
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(8)))
    assert len(calls) == 5
    assert sorted(statuses) == [401]*5 + [429]*3


def test_single_account_limit_survives_changing_proxy_address(runtime, monkeypatch):
    auth = Auth(runtime.settings, runtime.db)
    monkeypatch.setattr('frameseek.core.auth.verify_password', lambda *args:False)
    statuses = []
    for i in range(6):
        try:
            auth.login(request(f'address-{i}'), 'admin', 'wrong')
        except HTTPException as error:
            statuses.append(error.status_code)
    assert statuses == [401]*5 + [429]


def test_logout_revokes_copied_cookie_and_keeps_other_login(runtime):
    with TestClient(create_app(runtime.settings, runtime)) as client:
        first = client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        copied = client.cookies.get('imgsearch_session')
        second = client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        other = client.cookies.get('imgsearch_session')
        client.cookies.clear()
        client.cookies.set('imgsearch_session', copied)
        assert client.post('/api/logout',headers={'X-CSRF-Token':first.json()['csrf']}).status_code == 200
        client.cookies.set('imgsearch_session', copied)
        assert client.get('/api/status').status_code == 401
        # Revocation persists when the application is recreated; another login remains valid.
        fresh = Auth(runtime.settings, runtime.db)
        try:
            fresh.require(request(token=copied))
        except HTTPException as error:
            assert error.status_code == 401
        else:
            raise AssertionError('Logged-out session was reused after restart')
        # The HTTP cookie jar may quote a base64 value containing '='; Request unquotes it.
        assert fresh.require(request(token=other)) == other.strip('"')
        assert second.status_code == 200


def test_non_ascii_csrf_is_rejected_without_server_error(runtime):
    with TestClient(create_app(runtime.settings, runtime)) as client:
        assert client.post('/api/login',json={'username':'admin','password':'testing-secret'}).status_code == 200
        assert client.post('/api/updates/pause',headers={b'x-csrf-token':b'\xff'}).status_code == 403


def test_sensitive_reads_and_mutations_require_login(runtime):
    with TestClient(create_app(runtime.settings, runtime)) as client:
        for path in ('/api/history', '/api/history/x', '/api/history/x/image',
                     '/api/settings', '/api/settings/compose', '/api/status',
                     '/api/search/directories', '/api/frames/x', '/api/frames/x/neighbors',
                     '/api/emby/settings', '/api/emby/clients', '/api/emby/stream/x', '/emby/player/x'):
            assert client.get(path).status_code == 401, path
        for path in ('/api/logout', '/api/updates/scan', '/api/updates/pause',
                     '/api/emby/refresh', '/api/emby/open/x', '/api/emby/play/x',
                     '/api/emby/web-begin/x', '/api/emby/web-play/x', '/api/emby/stop/x'):
            assert client.post(path).status_code == 401, path
        assert client.delete('/api/history/x').status_code == 401


def test_security_cookie_flags_and_upload_body_limit(runtime):
    runtime.settings.secure_cookie = True
    with TestClient(create_app(runtime.settings, runtime), base_url='https://testserver') as client:
        response = client.post('/api/login',json={'username':'admin','password':'testing-secret'})
        cookie = response.headers['set-cookie'].lower()
        assert all(flag in cookie for flag in ('httponly','secure','samesite=strict'))
        assert client.post('/api/login',content=b' '* (12*1024*1024),
                           headers={'Content-Type':'application/json'}).status_code == 413
