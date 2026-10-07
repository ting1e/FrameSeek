from __future__ import annotations

import numpy as np
from qdrant_client import QdrantClient, models

from .config import DIMENSION, Settings


class VectorStore:
    def __init__(self, settings: Settings, fingerprint: str, grpc_port: int | None = None):
        self.settings = settings
        self.collection = "bif_" + fingerprint[:20]
        self.client = (QdrantClient(":memory:") if settings.qdrant_url == ":memory:" else
                       QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_key, timeout=120,
                                    grpc_port=grpc_port or 6334, prefer_grpc=grpc_port is not None,
                                    trust_env=False))

    def ensure(self, apply_mode: bool = True):
        memory = models.Memory.COLD if self.settings.mode == "low_memory" else models.Memory.CACHED
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE, memory=memory),
                hnsw_config=models.HnswConfigDiff(m=32, ef_construct=200, memory=memory, max_indexing_threads=self.settings.indexing_threads),
                optimizers_config=models.OptimizersConfigDiff(
                    max_optimization_threads=1, max_segment_size=131072, indexing_threshold=8192,
                    default_segment_number=2),
            )
            if self.settings.qdrant_url != ":memory:":
                for key in ("version", "file_id", "source", "media_type"):
                    self.client.create_payload_index(self.collection, key, models.PayloadSchemaType.KEYWORD, wait=True)
        else:
            info = self.client.get_collection(self.collection)
            if info.config.params.vectors.size != DIMENSION or info.config.params.vectors.distance != models.Distance.COSINE:
                raise RuntimeError("Qdrant vector dimension/distance mismatch")
            if self.settings.qdrant_url != ':memory:' and 'media_type' not in info.payload_schema:
                self.client.create_payload_index(self.collection, 'media_type', models.PayloadSchemaType.KEYWORD, wait=True)
            if apply_mode and self.settings.qdrant_url != ":memory:":
                self.client.update_collection(
                    self.collection, vectors_config={"": models.VectorParamsDiff(memory=memory)},
                    hnsw_config=models.HnswConfigDiff(memory=memory, max_indexing_threads=self.settings.indexing_threads),
                    optimizers_config=models.OptimizersConfigDiff(max_optimization_threads=1, max_segment_size=131072,
                                                                  default_segment_number=2),
                )

    def upsert(self, rows: list[dict], vectors: np.ndarray):
        if not len(rows):
            return
        if vectors.shape != (len(rows), DIMENSION):
            raise ValueError('Vector batch/frame row count mismatch')
        points = models.Batch(ids=[row['id'] for row in rows], vectors=vectors.tolist(),
                              payloads=[{**{key: row[key] for key in ('version', 'file_id', 'source')},
                                         'media_type': row.get('media_type', 'bif')} for row in rows])
        self.client.upsert(self.collection, points, wait=True)

    def delete_version(self, version: str):
        self.client.delete(self.collection, models.FilterSelector(filter=models.Filter(must=[
            models.FieldCondition(key="version", match=models.MatchValue(value=version))
        ])), wait=True)

    @staticmethod
    def scope_filter(source='', file_ids=None, media_type='all'):
        conditions = []
        if source:
            conditions.append(models.FieldCondition(key='source', match=models.MatchValue(value=source)))
        if file_ids is not None:
            conditions.append(models.FieldCondition(key='file_id', match=models.MatchAny(any=file_ids)))
        image_condition = models.FieldCondition(key='media_type', match=models.MatchValue(value='image'))
        if media_type == 'image':
            conditions.append(image_condition)
        # Older BIF vectors have no media_type payload: keep them searchable without rewriting.
        excluded = [image_condition] if media_type == 'bif' else []
        return models.Filter(must=conditions, must_not=excluded) if conditions or excluded else None

    def search(self, vector: np.ndarray, limit: int, exact: bool = False, query_filter=None):
        return self.client.query_points(self.collection, query=vector.tolist(), limit=limit, query_filter=query_filter,
                                        search_params=models.SearchParams(hnsw_ef=max(256, limit), exact=exact),
                                        with_payload=False, with_vectors=False).points

    def count(self, query_filter=None) -> int:
        return self.client.count(self.collection, exact=True, count_filter=query_filter).count

    def create_snapshot(self):
        # Full-library snapshots can exceed the interactive client's 120s timeout.
        import httpx
        response = httpx.post(self.settings.qdrant_url.rstrip('/')+
                              f'/collections/{self.collection}/snapshots',
                              params={'wait':'true'}, headers={'api-key':self.settings.qdrant_key or ''},
                              timeout=3600, trust_env=False)
        response.raise_for_status()
        return models.SnapshotDescription.model_validate(response.json()['result'])

    def close(self):
        self.client.close()
