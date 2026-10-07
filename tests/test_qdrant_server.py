"""Opt-in integration tests against the real local Docker server, never a NAS."""
import os
import uuid
import time

import numpy as np
import pytest
from dotenv import load_dotenv
from qdrant_client import models

from frameseek.config import Settings
from frameseek.vectors import VectorStore

pytestmark = pytest.mark.skipif(os.getenv('IMGS_SERVER_TEST') != '1', reason='Real Qdrant server integration is opt-in')


def test_modes_delete_snapshot_and_reload(tmp_path):
    load_dotenv()
    settings = Settings(data=tmp_path / 'data', qdrant_url=os.environ['IMGS_QDRANT_URL'],
                        qdrant_key=os.environ['IMGS_QDRANT_KEY'])
    assert '127.0.0.1' in settings.qdrant_url, 'Integration tests must only use localhost'
    store = VectorStore(settings, uuid.uuid4().hex)
    try:
        store.ensure()
        info = store.client.get_collection(store.collection)
        assert info.config.params.vectors.memory == models.Memory.COLD
        assert info.config.hnsw_config.memory == models.Memory.COLD
        rng = np.random.default_rng(42)
        vectors = rng.standard_normal((2200, 1024)).astype(np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        rows = [{'id':str(uuid.uuid4()),'version':'test-generation','file_id':'test-file','source':'sda'} for _ in vectors]
        for start in range(0, len(rows), 128): store.upsert(rows[start:start+128], vectors[start:start+128])
        assert store.count() == len(vectors)
        store.client.update_collection(store.collection,
            optimizers_config=models.OptimizersConfigDiff(indexing_threshold=1024))
        deadline = time.monotonic() + 60
        while store.client.get_collection(store.collection).indexed_vectors_count < len(vectors):
            if time.monotonic() >= deadline:
                raise AssertionError('HNSW did not finish building within 60 seconds')
            time.sleep(0.5)
        exact = store.search(vectors[100], 20, exact=True)
        assert str(exact[0].id) == rows[100]['id']
        settings.mode = 'high_memory'; store.ensure()
        info = store.client.get_collection(store.collection)
        assert info.config.params.vectors.memory == models.Memory.CACHED
        assert info.config.hnsw_config.memory == models.Memory.CACHED
        assert str(store.search(vectors[100],20)[0].id) == rows[100]['id']
        snapshot = store.create_snapshot()
        store.delete_version('test-generation')
        assert store.count() == 0
        # Recovery from the server's own snapshot verifies the persisted index.
        store.client.recover_snapshot(store.collection,
            location=f'file:///qdrant/snapshots/{store.collection}/{snapshot.name}',
            priority=models.SnapshotPriority.SNAPSHOT, wait=True)
        assert store.count() == len(vectors)
        settings.mode = 'low_memory'; store.ensure()
        assert str(store.search(vectors[100],20,exact=True)[0].id) == rows[100]['id']
    finally:
        store.client.delete_collection(store.collection)
        store.close()
