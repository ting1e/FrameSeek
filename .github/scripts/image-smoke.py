"""Runs inside an isolated temporary container; no application data mounts."""
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

import torch
from transformers import AutoModel
from frameseek.remote import roots
import frameseek
assert os.getuid() == 10001
assert torch.version.cuda is None
assert roots() == {}
package = Path(frameseek.__file__).parent
assert (package/'static/app.js').is_file()
assert not Path('/app/.env').exists()
assert not any(p.name in ('credentials.txt', 'sync.local.json') or p.name.endswith(('.bif','.safetensors','.snapshot','.npz','.sqlite3')) for p in Path('/app').rglob('*'))
assert not any(p.name in ('credentials.txt', 'sync.local.json') or p.name.endswith(('.bif','.safetensors','.snapshot','.npz','.sqlite3')) for p in package.rglob('*'))

with tempfile.TemporaryDirectory() as folder:
    env_path = Path(folder) / '.env'
    subprocess.run(['frameseek','init'], cwd=folder, check=True, capture_output=True)
    credentials = (Path(folder) / 'credentials.txt').read_text().splitlines()
    username, password = [line.split(':',1)[1].strip() for line in credentials[:2]]
    assert username == 'admin'
    child_env = dict(os.environ, IMGS_DEVICE='cpu', IMGS_DATA=folder+'/data', IMGS_SECURE_COOKIE='false')
    process = subprocess.Popen(['frameseek','serve','--port','18501'], cwd=folder, env=child_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = 'http://127.0.0.1:18501'
        for _ in range(100):
            try:
                with urllib.request.urlopen(base+'/health/live',timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                if process.poll() is not None: raise RuntimeError('Application exited before startup')
                time.sleep(.1)
        else: raise RuntimeError('Application startup timeout')
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request = urllib.request.Request(base+'/api/login', data=json.dumps({'username':username,'password':password}).encode(), headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=5) as response:
            assert response.status == 200
        with opener.open(base+'/api/settings',timeout=5) as response:
            profile = json.load(response)
        assert profile['device'] == 'cpu' and profile['saved']['auto_update'] is False
        assert not profile['restart_required']
        print('Isolated image: init, server liveness, login and settings passed without model or database service.')
    finally:
        process.terminate()
        process.wait(timeout=15)
