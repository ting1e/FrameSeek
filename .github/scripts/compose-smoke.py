"""Verify the one-command deployment with a tiny model-header fixture, not real inference."""
import http.cookiejar
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import urllib.request
import uuid


repository = Path.cwd()
project = 'compose-ci-' + uuid.uuid4().hex[:10]


with tempfile.TemporaryDirectory() as temporary:
    folder = Path(temporary)
    model = folder / 'models/dinov3-vitl16'
    model.mkdir(parents=True)
    (model / 'config.json').write_text(json.dumps({'model_type':'dinov3_vit','hidden_size':1024,
                                                'num_hidden_layers':24,'patch_size':16}))
    header = json.dumps({'test':{'dtype':'F32','shape':[1],'data_offsets':[0,4]}}).encode()
    header += b' ' * (-len(header) % 8)
    (model / 'model.safetensors').write_bytes(struct.pack('<Q',len(header)) + header + struct.pack('<f',1.0))
    (folder / 'media').mkdir()
    (folder / 'empty.env').write_text('')
    original = ['docker','compose','--project-directory',str(folder),'--env-file',str(folder/'empty.env'),'-p',project,
                '-f',str(repository/'compose.ghcr.yml')]
    parsed = subprocess.run(original + ['config','--format','json'], check=True,capture_output=True,text=True,encoding='utf-8')
    config = json.loads(parsed.stdout)
    def omit_nulls(value):
        if isinstance(value, dict):
            return {key:omit_nulls(item) for key,item in value.items() if item is not None}
        if isinstance(value, list):
            return [omit_nulls(item) for item in value]
        return value
    config = omit_nulls(config)
    for name in ('init','app'):
        config['services'][name]['image'] = os.getenv('TEST_IMAGE','frameseek:ci')
        config['services'][name]['pull_policy'] = 'never'
    config['services']['app']['ports'] = [{'target':8000,'published':'18542','host_ip':'127.0.0.1','protocol':'tcp','mode':'ingress'}]
    config['services']['app']['environment']['IMGS_SECURE_COOKIE'] = 'false'
    for mount in config['services']['app']['volumes']:
        if mount['target'] in ('/mnt/videos','/mnt/archive'):
            mount['source'] = str(folder/'media')
    (folder/'compose.json').write_text(json.dumps(config))
    compose = ['docker','compose','--project-directory',str(folder),'--env-file',str(folder/'empty.env'),
               '-p',project,'-f',str(folder/'compose.json')]

    def run(arguments):
        result = subprocess.run(compose + arguments, capture_output=True,text=True,encoding='utf-8',errors='replace')
        if result.returncode:
            raise RuntimeError('Compose check failed: ' + arguments[0] + '\n' + result.stderr[-2000:])
        return result.stdout

    base = 'http://127.0.0.1:18542'

    def wait_ready():
        for _ in range(120):
            try:
                with urllib.request.urlopen(base+'/health/live',timeout=1) as response:
                    assert response.status == 200
                return
            except OSError:
                time.sleep(.25)
        raise RuntimeError('Application startup timeout')

    try:
        run(['up','-d'])
        wait_ready()
        login_text = run(['exec','-T','app','cat','/data/credentials.txt'])
        username,password = [line.split(':',1)[1].strip() for line in login_text.splitlines()]
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request = urllib.request.Request(base+'/api/login',data=json.dumps({'username':username,'password':password}).encode(),headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=15) as response: assert response.status == 200
        with opener.open(base+'/api/status',timeout=15) as response:
            status = json.load(response)
            assert status['model_ready']
        with opener.open(base+'/api/settings',timeout=15) as response:
            settings = json.load(response)
            assert settings['monitor_directories'] == []
        profile = settings['saved']
        profile['monitor_directories'] = ['/mnt/videos']
        request = urllib.request.Request(base+'/api/settings',method='PUT',data=json.dumps(profile).encode(),headers={'Content-Type':'application/json','X-CSRF-Token':status['csrf']})
        with opener.open(request,timeout=15) as response: assert response.status == 200
        code = "from dotenv import dotenv_values; import os,urllib.request; assert os.getuid()==10001; key=dotenv_values('/data/runtime/auth.env')['IMGS_QDRANT_KEY']; request=urllib.request.Request('http://qdrant:6333/collections',headers={'api-key':key}); assert urllib.request.urlopen(request).status==200"
        run(['exec','-T','app','python','-c',code])
        run(['exec','-T','qdrant','sh','-c',
             'test -f /bootstrap/qdrant.yaml && test ! -e /bootstrap/auth.env && test ! -e /bootstrap/model-manifest.json'])
        run(['up','-d','--force-recreate'])
        wait_ready()
        assert run(['exec','-T','app','cat','/data/credentials.txt']) == login_text
        with opener.open(base+'/api/settings',timeout=15) as response:
            assert json.load(response)['monitor_directories'] == ['/mnt/videos']
        print('Compose first start, generated credentials, Qdrant authentication and restart checks passed.')
    finally:
        run(['down','-v'])
        # The Linux containers own their bind-mounted data as UID 10001.
        # Return this test directory to the runner before TemporaryDirectory removes it.
        if os.name == 'posix' and (folder / 'data').exists():
            subprocess.run([
                'docker','run','--rm','--network','none','--user','0:0',
                '--entrypoint','chown','-v',str(folder / 'data') + ':/cleanup',
                os.getenv('TEST_IMAGE','frameseek:ci'),
                '-R',f'{os.getuid()}:{os.getgid()}','/cleanup',
            ], check=True)
