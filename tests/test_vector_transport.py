import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import numpy as np

from frameseek.config import Settings
from frameseek.vectors import VectorStore


def test_internal_qdrant_requests_bypass_system_proxy(tmp_path, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.respond({'title': 'qdrant', 'version': '1.19.2'})

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            requests.append(self.path)
            result = {'count': 3} if self.path.endswith('/count') else {'points': []}
            self.respond({'result': result, 'status': 'ok', 'time': 0.001})

        def log_message(self, *args):
            pass

    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = VectorStore(Settings(data=tmp_path, qdrant_url=f'http://127.0.0.1:{server.server_port}'), 'abc')
    try:
        assert store.count() == 3
        assert store.search(np.ones(1024, np.float32), 20) == []
        assert len(requests) == 2
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        thread.join()
