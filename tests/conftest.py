import io
import json
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from frameseek.bif import MAGIC
from frameseek.config import MODEL_ID, Settings
from frameseek.search import Runtime


def jpeg(color):
    out = io.BytesIO()
    Image.new('RGB', (64, 36), color).save(out, format='JPEG')
    return out.getvalue()


def write_bif(path: Path, colors=('red', 'green', 'blue'), multiplier=1000, corrupt=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    images = [jpeg(color) if i != corrupt else b'bad JPEG data' for i, color in enumerate(colors)]
    header = MAGIC + struct.pack('<III', 0, len(images), multiplier) + bytes(44)
    offset = 64 + (len(images) + 1) * 8
    entries = []
    for i, data in enumerate(images):
        entries.append(struct.pack('<II', i * 10, offset))
        offset += len(data)
    entries.append(struct.pack('<II', 0xffffffff, offset))
    path.write_bytes(header + b''.join(entries) + b''.join(images))
    return path


class TestEmbedder:
    """Deterministic test double for transaction tests, never used by production."""
    def __init__(self):
        self.calls = 0

    def embed(self, images, search=False):
        self.calls += 1
        values = []
        for image in images:
            value = np.zeros(1024, np.float32)
            value[:3] = np.asarray(image.convert('RGB'), dtype=np.float32).mean(axis=(0, 1)) + 1
            value /= np.linalg.norm(value)
            values.append(value)
        return np.array(values, np.float32)


@pytest.fixture
def runtime(tmp_path):
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'manifest.json').write_text(json.dumps({'model_id': MODEL_ID, 'dimension': 1024, 'fingerprint': '1234567890abcdef1234567890abcdef'}))
    settings = Settings(data=tmp_path / 'data', model=model,
                        sources={'sda': tmp_path / 'sda', 'sdc': tmp_path / 'sdc'},
                        qdrant_url=':memory:', stable_seconds=0, auto_update=False,
                        escaped_paths=False, batch=2, chunk_frames=2,
                        secure_cookie=False, session_secret='a' * 64,
                        password_hash='')
    for root in settings.sources.values():
        root.mkdir()
    from frameseek.auth import password_hash
    settings.password_hash = password_hash('testing-secret')
    result = Runtime(settings, embedder=TestEmbedder())
    yield result
    result.close()


def build(runtime):
    indexer = runtime.get_indexer()
    indexer.scan(trust_stable=True)
    for job in indexer.pending():
        indexer.process_version(job['id'])
    indexer.cleanup()
