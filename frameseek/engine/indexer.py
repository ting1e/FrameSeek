from __future__ import annotations

import json
import os
import threading
import time
import uuid
import portalocker
from pathlib import Path

import numpy as np

from frameseek.media import bif
from frameseek.core.config import DIMENSION, Settings
from frameseek.core.db import Database
from frameseek.media.decode import decoded_batches, prepared_batches
from frameseek.engine.model import Embedder, digest
from frameseek.core.paths import canonical_mtime, media_path, original_relative
from frameseek.engine.vectors import VectorStore
from frameseek.engine.monitoring import folders, includes, visit_directory, sql_scope
from frameseek.media.images import supported, media_type, MAX_IMAGE_BYTES


def file_id(source: str, relative: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([source, relative], ensure_ascii=False)))


def frame_id(version: str, number: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"bif:{version}:{number}"))


class WriterLock:
    def __init__(self, path: Path):
        self.path, self.thread_lock = path, threading.Lock()
        self.check_interval = 0.25

    def __enter__(self):
        self.thread_lock.acquire()
        try:
            self.file_lock = portalocker.Lock(self.path, timeout=120, check_interval=self.check_interval)
            self.file_lock.acquire()
        except BaseException:
            self.thread_lock.release()
            raise

    def __exit__(self, *args):
        self.file_lock.release()
        self.thread_lock.release()


class Indexer:
    def __init__(self, settings: Settings, db: Database, embedder: Embedder, store: VectorStore):
        self.settings, self.db, self.embedder, self.store = settings, db, embedder, store
        self.db.bind_model(settings.manifest)
        self.operation_lock = WriterLock(settings.data / ".writer.lock")

    def path(self, source: str, relative: str) -> Path:
        return media_path(self.settings.sources[source], relative, self.settings.escaped_paths)

    def scan(self, trust_stable: bool = False, changes: set[tuple[str, str]] | None = None) -> dict:
        with self.operation_lock:
            marker, now = str(uuid.uuid4()), time.time()
            counts = {"observed": 0, "modified": 0, "queued": 0, "deleted": 0, "scan_errors": 0}
            scope = folders(self.settings, self.db)
            sources = dict(self.settings.sources)
            registered_roots = {root.resolve():source for source,root in sources.items()}
            for source, root in sources.items():
                targets = {relative for name, relative in changes if name == source} if changes is not None else None
                if targets is not None and not targets:
                    continue
                selected = [item for item in scope if item['source'] == source]
                if not selected:
                    continue
                failures: list[str] = []
                if not root.is_dir():
                    self.db.event("scan_error", f"Source unavailable; deletions skipped: {source}")
                    counts["scan_errors"] += 1
                    continue
                def onerror(error):
                    failures.append(str(error))
                for item in selected:
                    try:
                        selected_root = self.path(source, item['path']) if item['path'] else root
                        if not selected_root.is_dir():
                            failures.append(f'Monitored folder unavailable: {source}/{item["path"]}')
                    except (OSError, ValueError) as error:
                        failures.append(str(error))
                entries = os.walk(root, onerror=onerror, followlinks=False) if targets is None else (
                    (self.path(source, relative).parent, [], [self.path(source, relative).name]) for relative in targets)
                for directory, subdirs, filenames in entries:
                    subdirs[:] = [d for d in subdirs if not Path(directory, d).is_symlink() and
                                  registered_roots.get(Path(directory, d).resolve(), source) == source and
                                  visit_directory(scope, source, original_relative(root, Path(directory, d), self.settings.escaped_paths))]
                    for name in filenames:
                        if not supported(name):
                            continue
                        path = Path(directory, name)
                        if path.is_symlink():
                            continue
                        ident = None
                        try:
                            relative = original_relative(root, path, self.settings.escaped_paths)
                            if not includes(scope, source, relative):
                                continue
                            self.path(source, relative)  # Reject escaping paths before recording them.
                            stamp = path.stat()
                            ident = file_id(source, relative)
                            old = self.db.one("SELECT * FROM files WHERE id=?", (ident,))
                            changed = (not old or old["size"] != stamp.st_size or
                                       old["mtime_ns"] != canonical_mtime(stamp) or old["status"] == "deleted")
                            if changed:
                                if old and old['status'] != 'deleted':
                                    counts['modified'] += 1
                                with self.db.connect() as c:
                                    if old and old["desired_version"]:
                                        c.execute("INSERT OR IGNORE INTO deletions VALUES(?,?)", (old["desired_version"], now))
                                    if old and old["active_version"]:
                                        c.execute("INSERT OR IGNORE INTO deletions VALUES(?,?)", (old["active_version"], now))
                                    c.execute("""INSERT INTO files(id,source,relpath,size,mtime_ns,stable_since,seen)
                                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                                        size=excluded.size,mtime_ns=excluded.mtime_ns,stable_since=excluded.stable_since,
                                        seen=excluded.seen,active_version=NULL,desired_version=NULL,status='observed',error=NULL""",
                                              (ident, source, relative, stamp.st_size, canonical_mtime(stamp), now, marker))
                                    c.execute('UPDATE files SET media_type=? WHERE id=?', (media_type(path), ident))
                                if old and old['status'] != 'deleted':
                                    self.db.event('bif_modified', f'{source}/{relative}：文件已变动，等待稳定后重新获取 embedding', ident)
                            else:
                                self.db.execute("UPDATE files SET seen=? WHERE id=?", (marker, ident))
                            counts["observed"] += 1
                            current = self.db.one("SELECT * FROM files WHERE id=?", (ident,))
                            if not current["desired_version"] and (trust_stable or now - current["stable_since"] >= self.settings.stable_seconds):
                                self.queue_file(ident, path, stamp)
                                counts["queued"] += 1
                        except FileNotFoundError:
                            # A delete/rename event is reconciled by the unseen-record pass below.
                            if targets is None:
                                failures.append(f'File disappeared during scan: {path}')
                        except Exception as error:
                            failures.append(f"{path}: {error}")
                            if ident is not None:
                                self.db.execute("UPDATE files SET error=? WHERE id=?", (str(error)[:2000], ident))
                if failures:
                    counts["scan_errors"] += len(failures)
                    for error in failures[:20]:
                        self.db.event("scan_error", error)
                else:
                    if targets is None:
                        unseen = self.db.rows("SELECT * FROM files WHERE source=? AND seen!=? AND status!='deleted'", (source, marker))
                    else:
                        unseen = [row for relative in targets if (row := self.db.one(
                            "SELECT * FROM files WHERE id=? AND seen!=? AND status!='deleted'", (file_id(source, relative), marker)))]
                    for row in unseen:
                        if targets is not None and row['relpath'] not in targets:
                            continue
                        if not includes(scope, source, row['relpath']):
                            continue
                        if not includes(folders(self.settings, self.db), source, row['relpath']):
                            continue
                        with self.db.connect() as c:
                            for version in {row["desired_version"], row["active_version"]} - {None}:
                                c.execute("INSERT OR IGNORE INTO deletions VALUES(?,?)", (version, now))
                            c.execute("UPDATE files SET status='deleted',active_version=NULL,desired_version=NULL WHERE id=?", (row["id"],))
                        counts["deleted"] += 1
            if changes is None:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('last_scan',?)", (str(now),))
            self.db.event("scan" if changes is None else "watch_update", json.dumps(counts, ensure_ascii=False))
            return counts

    def queue_file(self, ident: str, path: Path, stamp):
        kind = media_type(path)
        if kind == 'image' and (stamp.st_size <= 0 or stamp.st_size > MAX_IMAGE_BYTES):
            raise ValueError('Image must be nonempty and at most 64 MiB')
        sha = digest(path)
        frames = bif.parse(path) if kind == 'bif' else [bif.Frame(0, 0, 0, stamp.st_size)]
        after = path.stat()
        if (after.st_size, canonical_mtime(after)) != (stamp.st_size, canonical_mtime(stamp)):
            raise RuntimeError("File changed during parsing; waiting for next scan")
        version = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ident}:{sha}:{self.settings.manifest['fingerprint']}"))
        with self.db.connect() as c:
            c.execute("""INSERT OR IGNORE INTO versions(id,file_id,sha256,size,mtime_ns,total,created)
                         VALUES(?,?,?,?,?,?,?)""", (version, ident, sha, after.st_size, canonical_mtime(after), len(frames), time.time()))
            # Same content reappearing at the same path can reuse archived vectors.
            c.execute("UPDATE versions SET size=?,mtime_ns=?,status='pending',retry_at=0 WHERE id=?",
                      (after.st_size, canonical_mtime(after), version))
            c.executemany("INSERT OR IGNORE INTO frames(id,version,frame_no,time_ms,offset,length) VALUES(?,?,?,?,?,?)",
                          [(frame_id(version, fr.index), version, fr.index, fr.time_ms, fr.offset, fr.length) for fr in frames])
            c.execute("DELETE FROM deletions WHERE version=?", (version,))
            c.execute("UPDATE chunks SET uploaded=0 WHERE version=?", (version,))
            c.execute("UPDATE files SET desired_version=?,status='queued',error=NULL WHERE id=?", (version, ident))

    def flush_chunk(self, chunk: dict):
        path = self.settings.data / chunk["path"]
        if digest(path) != chunk["sha256"]:
            raise RuntimeError(f"Vector chunk checksum mismatch: {chunk['path']}")
        with np.load(path, allow_pickle=False) as saved:
            numbers = saved["frames"].tolist()
            vectors = saved["vectors"]
            mapped = {r['frame_no']:r for r in self.db.rows("""SELECT fr.*,f.id AS file_id,f.source,f.media_type
                FROM frames fr JOIN versions v ON v.id=fr.version JOIN files f ON f.id=v.file_id
                WHERE fr.version=? AND fr.frame_no>=? AND fr.frame_no<?""",
                (chunk['version'], chunk['start'], chunk['end']))}
            rows = []
            for number in numbers:
                row = mapped.get(number)
                if not row or not row["valid"]:
                    raise RuntimeError("Vector chunk/frame mapping mismatch")
                rows.append(row)
            if vectors.shape != (len(rows), DIMENSION) or not np.isfinite(vectors).all():
                raise RuntimeError("Invalid saved vector block")
            self.store.upsert(rows, vectors)
        self.db.execute("UPDATE chunks SET uploaded=1 WHERE version=? AND start=?", (chunk["version"], chunk["start"]))

    def process_version(self, version: str, stop: threading.Event | None = None, max_frames: int | None = None,
                        yield_at: float | None = None, yield_requested: threading.Event | None = None) -> int:
        with self.operation_lock:
            return self._process_version(version, stop, max_frames, yield_at, yield_requested)

    def _process_version(self, version: str, stop: threading.Event | None, max_frames: int | None,
                         yield_at: float | None = None, yield_requested: threading.Event | None = None) -> int:
        row = self.db.one("""SELECT v.*,f.source,f.relpath,f.media_type,f.desired_version FROM versions v
                           JOIN files f ON f.id=v.file_id WHERE v.id=?""", (version,))
        if not row or row["desired_version"] != version:
            return 0
        if not includes(folders(self.settings, self.db), row['source'], row['relpath']):
            return 0
        path = self.path(row["source"], row["relpath"])
        self.db.execute("UPDATE versions SET status='processing',error=NULL WHERE id=?", (version,))
        processed = 0
        try:
            self.verify_stamp(path, row)
            for chunk in self.db.rows("SELECT * FROM chunks WHERE version=? AND uploaded=0 ORDER BY start", (version,)):
                self.flush_chunk(chunk)
            cursor = row["cursor"]
            while cursor < row["total"]:
                paused = self.db.one("SELECT value FROM meta WHERE key='paused'")
                if ((stop and stop.is_set()) or (yield_requested and yield_requested.is_set()) or (paused and paused['value'] == 'true')
                        or not includes(folders(self.settings, self.db), row['source'], row['relpath'])
                        or (max_frames is not None and processed >= max_frames)
                        or (yield_at is not None and time.time() >= yield_at)):
                    self.db.execute("UPDATE versions SET status='pending' WHERE id=?", (version,))
                    return processed
                self.verify_stamp(path, row)
                count = min(self.settings.chunk_frames, row["total"] - cursor)
                if max_frames is not None:
                    count = min(count, max_frames - processed)
                batch = self.db.rows("SELECT * FROM frames WHERE version=? AND frame_no>=? AND frame_no<? ORDER BY frame_no",
                                     (version, cursor, cursor + count))
                for frame in batch:
                    frame['media_type'] = row['media_type']
                good, bad, pieces = [], [], []
                batches = decoded_batches(path, batch, self.settings.batch, self.settings.decode_workers)
                prepared = (str(self.settings.device).startswith('cuda') and self.settings.decode_workers > 1
                            and hasattr(self.embedder, 'embed_prepared'))
                if prepared:
                    batches = prepared_batches(batches, self.embedder.prepare_images)
                try:
                    for decoded in batches:
                        if prepared:
                            mapped, errors, tensors = decoded
                            good.extend(mapped)
                            bad.extend(errors)
                            if tensors is not None:
                                pieces.append(self.embedder.embed_prepared(tensors))
                            continue
                        images = [image for _, image, _ in decoded if image is not None]
                        try:
                            for frame, image, error in decoded:
                                if image is None:
                                    bad.append((error, frame['id']))
                                else:
                                    good.append(frame)
                            if images:
                                pieces.append(self.embedder.embed(images))
                        finally:
                            for image in images:
                                image.close()
                finally:
                    batches.close()
                vectors = np.concatenate(pieces) if pieces else np.empty((0, DIMENSION), np.float32)
                self.verify_stamp(path, row)
                folder = self.settings.data / "vectors" / version
                folder.mkdir(exist_ok=True)
                destination = folder / f"{cursor:08d}.npz"
                temp = destination.with_suffix(".tmp")
                with temp.open("wb") as stream:
                    np.savez(stream, frames=np.array([r["frame_no"] for r in good], np.int32), vectors=vectors)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, destination)
                relative = destination.relative_to(self.settings.data).as_posix()
                checksum = digest(destination)
                with self.db.connect() as c:
                    c.executemany("UPDATE frames SET valid=0,error=? WHERE id=?", bad)
                    c.execute("INSERT OR REPLACE INTO chunks(version,start,end,path,sha256,uploaded) VALUES(?,?,?,?,?,0)",
                              (version, cursor, cursor + count, relative, checksum))
                    c.execute("UPDATE versions SET cursor=? WHERE id=?", (cursor + count, version))
                self.flush_chunk(self.db.one("SELECT * FROM chunks WHERE version=? AND start=?", (version, cursor)))
                cursor += count
                processed += count
            self.verify_stamp(path, row)
            if digest(path) != row["sha256"]:
                raise RuntimeError("Source content changed while building")
            with self.db.connect() as c:
                current = c.execute("SELECT active_version,desired_version FROM files WHERE id=?", (row["file_id"],)).fetchone()
                if current["desired_version"] != version:
                    raise RuntimeError("Source generation was superseded")
                if current["active_version"] and current["active_version"] != version:
                    c.execute("INSERT OR IGNORE INTO deletions VALUES(?,?)", (current["active_version"], time.time()))
                c.execute("UPDATE versions SET status='ready',attempts=0,error=NULL WHERE id=?", (version,))
                c.execute("UPDATE files SET active_version=?,status='ready',error=NULL WHERE id=?", (version, row["file_id"]))
            self.db.event("indexed", f"{row['source']}/{row['relpath']}：embedding 已更新，{row['total']} 帧（损坏帧跳过）", row["file_id"])
            return processed
        except Exception as error:
            attempts = row["attempts"] + 1
            with self.db.connect() as c:
                c.execute("UPDATE versions SET status='failed',attempts=?,retry_at=?,error=? WHERE id=?",
                          (attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)), str(error)[:2000], version))
                c.execute("UPDATE files SET error=? WHERE id=?", (str(error)[:2000], row["file_id"]))
            self.db.event("index_error", str(error), row["file_id"])
            raise

    @staticmethod
    def verify_stamp(path: Path, row: dict):
        stat = path.stat()
        if (stat.st_size, canonical_mtime(stat)) != (row["size"], row["mtime_ns"]):
            raise RuntimeError("Source file changed or was replaced; awaiting rescan")

    def scope_filter(self) -> tuple[str, list]:
        monitor_condition, monitor_arguments = sql_scope(folders(self.settings, self.db))
        if not self.settings.index_scope:
            return monitor_condition, monitor_arguments
        conditions, arguments = [], []
        for source, directories in self.settings.index_scope.items():
            for directory in directories:
                prefix = directory + '/'
                conditions.append('(f.source=? AND substr(f.relpath,1,?)=?)')
                arguments.extend([source, len(prefix), prefix])
        return '(' + ' OR '.join(conditions) + ') AND ' + monitor_condition, arguments + monitor_arguments

    def scope_stats(self) -> dict:
        condition, arguments = self.scope_filter()
        stats = self.db.one(f"""SELECT COUNT(*) AS files,COALESCE(SUM(v.total),0) AS total_frames,
            COALESCE(SUM(v.cursor),0) AS processed_frames,
            COALESCE(SUM(CASE WHEN v.status='ready' THEN 1 ELSE 0 END),0) AS ready_files,
            COALESCE(SUM(CASE WHEN v.status!='ready' THEN 1 ELSE 0 END),0) AS pending_files
            FROM versions v JOIN files f ON f.desired_version=v.id WHERE {condition}""", arguments)
        return {'directories': self.settings.index_scope, **stats}

    def pending(self, source: str | None = None) -> list[dict]:
        condition, arguments = self.scope_filter()
        return self.db.rows(f"""SELECT v.* FROM versions v JOIN files f ON f.desired_version=v.id
            WHERE (v.status IN ('pending','processing') OR (v.status='failed' AND v.retry_at<=?))
            AND (? IS NULL OR f.source=?) AND {condition} ORDER BY v.created LIMIT 100""",
            [time.time(), source, source, *arguments])

    def cleanup(self):
        with self.operation_lock:
            for row in self.db.rows("SELECT * FROM deletions ORDER BY created LIMIT 100"):
                live = self.db.one("SELECT id FROM files WHERE active_version=? OR desired_version=?",
                                   (row["version"], row["version"]))
                if live:
                    self.db.execute("DELETE FROM deletions WHERE version=?", (row["version"],))
                    continue
                self.store.delete_version(row["version"])
                self.db.execute("DELETE FROM deletions WHERE version=?", (row["version"],))


class BackgroundWorker:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop_event = threading.Event()
        self.scan_event = threading.Event()
        self.config_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.activity = "idle"
        self.error: str | None = None
        self.manual_active = False
        from frameseek.engine.watcher import MediaWatcher
        self.watcher = MediaWatcher(runtime.settings, runtime.db, self.wake_event)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="bif-indexer")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.scan_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=120)
        self.watcher.stop()

    def configuration_changed(self):
        self.config_event.set()
        self.wake_event.set()

    def wait(self, seconds):
        self.wake_event.wait(seconds)
        self.wake_event.clear()

    def run(self):
        next_scan = 0.0
        while not self.stop_event.is_set():
            if self.config_event.is_set():
                self.config_event.clear()
                next_scan = 0.0
            self.watcher.configure(self.runtime.settings.auto_update)
            paused = self.runtime.db.one("SELECT value FROM meta WHERE key='paused'")
            if paused and paused["value"] == "true":
                self.activity = "paused"
                self.wait(1)
                continue
            manual = self.scan_event.is_set()
            if manual:
                self.manual_active = True
            enabled = self.runtime.settings.auto_update or self.manual_active
            if not enabled:
                self.activity = "disabled"
                self.wait(1)
                continue
            try:
                indexer = self.runtime.get_indexer()
                changed, directory_changed = self.watcher.drain()
                if manual or directory_changed or time.time() >= next_scan:
                    self.scan_event.clear()
                    self.activity = "scanning"
                    indexer.scan()
                    next_scan = time.time() + self.runtime.settings.interval
                else:
                    condition, arguments = indexer.scope_filter()
                    stable = self.runtime.db.rows(f"SELECT f.source,f.relpath FROM files f WHERE desired_version IS NULL AND status='observed' AND error IS NULL AND stable_since<=? AND {condition} LIMIT 256",
                        [time.time()-self.runtime.settings.stable_seconds, *arguments])
                    changed.update((row['source'],row['relpath']) for row in stable)
                    if changed:
                        self.activity = 'checking_changes'
                        indexer.scan(changes=changed)
                self.wake_event.clear()
                for job in indexer.pending():
                    paused = self.runtime.db.one("SELECT value FROM meta WHERE key='paused'")
                    if (self.stop_event.is_set() or (paused and paused['value'] == 'true')
                            or self.scan_event.is_set() or self.config_event.is_set() or self.watcher.has_pending or time.time() >= next_scan):
                        break
                    self.activity = f"indexing:{job['file_id']}"
                    try:
                        indexer.process_version(job["id"], self.stop_event, yield_at=next_scan, yield_requested=self.wake_event)
                    except Exception as error:
                        self.error = str(error)
                self.activity = "cleanup"
                indexer.cleanup()
                self.activity = "idle"
                self.error = None
                condition, arguments = indexer.scope_filter()
                unstable = self.runtime.db.one(f"SELECT COUNT(*) AS n FROM files f WHERE desired_version IS NULL AND status='observed' AND error IS NULL AND {condition}", arguments)
                if self.manual_active and not indexer.scope_stats()['pending_files'] and not unstable['n']:
                    self.manual_active = False
            except Exception as error:
                self.error = str(error)
                self.activity = "error"
            self.wait(2)
