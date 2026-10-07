from __future__ import annotations

import io
import threading
import time

from PIL import Image

from frameseek.core.config import Settings
from frameseek.core.db import Database
from frameseek.engine.indexer import BackgroundWorker, Indexer
from frameseek.engine.model import Embedder
from frameseek.core.paths import canonical_mtime, media_path
from frameseek.engine.vectors import VectorStore
from frameseek.media.scope import validate_scope, scope_file_ids
from frameseek.engine.progress import ProcessingProgress
from frameseek.media.images import validate_media_type


def fold(rows: list[dict]) -> list[dict]:
    by_file: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row.get("source"), row.get("relpath"), row["version"])
        by_file.setdefault(key, []).append(row)
    result = []
    for members in by_file.values():
        members = sorted({r["id"]: r for r in members}.values(), key=lambda r: (r["time_ms"], r["id"]))
        representative = dict(max(members, key=lambda r: (r["score"], r["id"])))
        representative["group"] = [{"id": r["id"], "time_ms": r["time_ms"], "score": r["score"],
                                    "frame_no": r["frame_no"], "preview_url": r.get("preview_url", f"/api/frames/{r['id']}")}
                                   for r in members]
        representative["group_count"] = len(members)
        result.append(representative)
    return sorted(result, key=lambda r: (-r["score"], r["id"]))


class Runtime:
    def __init__(self, settings: Settings, embedder=None, store=None):
        self.settings = settings
        self.db = Database(settings.db_path)
        from frameseek.media.directories import remember_sources
        from frameseek.integrations.remote import roots
        remember_sources(settings, self.db, roots())
        self.embedder = embedder or Embedder(settings)
        self.store = store
        self.indexer = None
        self.init_lock = threading.RLock()
        self.query_slots = threading.BoundedSemaphore(4)
        self.progress = ProcessingProgress()
        self.worker = BackgroundWorker(self)

    def get_store(self):
        with self.init_lock:
            if self.store is None:
                manifest = self.settings.manifest
                self.db.bind_model(manifest)
                store = VectorStore(self.settings, manifest["fingerprint"])
                store.ensure()
                self.store = store
            return self.store

    def get_indexer(self):
        with self.init_lock:
            if self.indexer is None:
                self.indexer = Indexer(self.settings, self.db, self.embedder, self.get_store())
            return self.indexer

    def available(self, row: dict) -> bool:
        try:
            path = media_path(self.settings.sources[row["source"]], row["relpath"], self.settings.escaped_paths)
            stat = path.stat()
            return (stat.st_size, canonical_mtime(stat)) == (row["size"], row["mtime_ns"])
        except (OSError, ValueError, KeyError):
            return False

    def query(self, image: Image.Image, top: int, collapse: bool = True, exact: bool = False, source: str = '', directory: str = '', directory_keyword: str = '', media_type: str = 'all') -> dict:
        validate_media_type(media_type)
        source, directory = validate_scope(self.settings.sources, source, directory)
        directory_keyword = directory_keyword.strip() if not directory else ''
        file_ids = scope_file_ids(self.db, source, directory, directory_keyword, {key:path.resolve().as_posix() for key,path in self.settings.sources.items()}) if directory or directory_keyword else None
        start = time.perf_counter()
        if file_ids == []:
            return {'results':[], 'returned':0, 'requested':top, 'inference_ms':0, 'elapsed_ms':0,
                    'mode':self.settings.mode, 'collapsed':collapse, 'candidate_limit_reached':False,
                    'source':source, 'directory':directory, 'directory_keyword':directory_keyword, 'media_type':media_type}
        vector = self.embedder.embed([image], search=True)[0]
        inference_ms = (time.perf_counter() - start) * 1000
        store = self.get_store()
        query_filter = VectorStore.scope_filter(source, file_ids, media_type)
        maximum = store.count(query_filter) if query_filter else store.count()
        budget = min(maximum, 10000)
        candidates = min(maximum, max(200, top * 10))
        valid = []
        # Filtering old versions and folding may require searching beyond initial candidates.
        while candidates:
            raw = store.search(vector, candidates, exact, query_filter=query_filter) if query_filter else store.search(vector, candidates, exact)
            valid = []
            available = {}
            published = self.db.published_frames([str(point.id) for point in raw])
            for point in raw:
                row = published.get(str(point.id))
                if row and row['version'] not in available:
                    available[row['version']] = self.available(row)
                if row and available[row['version']]:
                    row["score"] = float(point.score)
                    row["preview_url"] = f"/api/frames/{row['id']}"
                    valid.append(row)
            valid = fold(valid) if collapse else sorted(valid, key=lambda r: (-r["score"], r["id"]))
            if len(valid) >= top or candidates >= budget:
                break
            candidates = min(budget, candidates * 2)
        self.db.add_durations(valid[:top])
        for row in valid[:top]:
            row["root_directory"] = self.settings.sources[row["source"]].resolve().as_posix()
        return {"results": valid[:top], "returned": min(top, len(valid)), "requested": top,
                "inference_ms": inference_ms, "elapsed_ms": (time.perf_counter() - start) * 1000,
                "mode": self.settings.mode, "collapsed": collapse, "source":source, "directory":directory, "directory_keyword":directory_keyword, "media_type":media_type,
                "candidate_limit_reached": candidates < maximum and len(valid) < top}

    def close(self):
        self.worker.stop()
        if self.store:
            self.store.close()
