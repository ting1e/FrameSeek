"""Opt-in end-to-end test of the real CPU application at localhost."""
import json
import os
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(os.getenv('IMGS_DOCKER_TEST') != '1',reason='Docker CPU test is opt-in')


def test_real_model_login_search_preview_and_neighbors():
    load_dotenv()
    password = Path('credentials.txt').read_text(encoding='utf-8').splitlines()[1].split(': ',1)[1]
    labels_path = Path('reports/controlled-queries/labels.jsonl')
    with labels_path.open(encoding='utf-8') as stream:
        labels = [json.loads(line) for line in stream]
    selected = [next(row for row in labels if row['source']==source and row['variant']=='original')
                for source in ('sda','sdc')]
    with httpx.Client(base_url='http://127.0.0.1:18443',timeout=180) as client:
        assert client.get('/health/live').status_code == 200
        assert client.get('/api/status').status_code == 401
        response = client.post('/api/login',json={'username':os.getenv('IMGS_USERNAME','admin'),'password':password})
        assert response.status_code == 200
        headers = {'X-CSRF-Token':response.json()['csrf']}
        status = client.get('/api/status')
        assert status.status_code == 200 and status.json()['model_ready']
        metrics = []
        for label in selected:
            with (labels_path.parent/label['image']).open('rb') as image:
                response = client.post('/api/search',headers=headers,
                    files={'image':('query.jpg',image,'image/jpeg')},data={'top':'20','collapse':'true'})
            assert response.status_code == 200,response.text
            result = response.json()
            assert result['results']
            matching = [hit for hit in result['results'] if hit['source']==label['source'] and hit['relpath']==label['relpath']]
            assert matching, 'Original BIF screenshot should find its source file'
            hit = matching[0]
            preview = client.get(hit['preview_url'])
            assert preview.status_code == 200 and preview.content.startswith(b'\xff\xd8')
            nearby = client.get('/api/frames/'+hit['id']+'/neighbors')
            assert nearby.status_code == 200
            assert 1 <= len(nearby.json()['frames']) <= 11
            metrics.append({'source':label['source'],'elapsed_ms':result['elapsed_ms'],
                            'inference_ms':result['inference_ms'],'results':result['returned']})
        assert client.post('/api/updates/pause').status_code == 403
        assert client.post('/api/updates/pause',headers=headers).json()['paused']
        assert not client.post('/api/updates/resume',headers=headers).json()['paused']
        assert client.post('/api/logout',headers=headers).status_code == 200
        assert client.get('/api/status').status_code == 401
        Path('reports/docker-cpu-search.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
