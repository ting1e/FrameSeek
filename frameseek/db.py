from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revoked_sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS files(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, relpath TEXT NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, stable_since REAL NOT NULL,
 seen TEXT NOT NULL, active_version TEXT, desired_version TEXT,
 status TEXT NOT NULL DEFAULT 'observed', error TEXT,
 UNIQUE(source, relpath));
CREATE TABLE IF NOT EXISTS versions(
 id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id),
 sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 total INTEGER NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS frames(
 id TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES versions(id),
 frame_no INTEGER NOT NULL, time_ms INTEGER NOT NULL, offset INTEGER NOT NULL,
 length INTEGER NOT NULL, valid INTEGER NOT NULL DEFAULT 1, error TEXT,
 UNIQUE(version, frame_no));
CREATE INDEX IF NOT EXISTS frames_version ON frames(version, frame_no);
CREATE INDEX IF NOT EXISTS frames_bad ON frames(version) WHERE valid=0;
CREATE INDEX IF NOT EXISTS files_active_version ON files(active_version);
CREATE INDEX IF NOT EXISTS files_desired_version ON files(desired_version);
CREATE TABLE IF NOT EXISTS chunks(
 version TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
 path TEXT NOT NULL, sha256 TEXT NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(version, start));
CREATE TABLE IF NOT EXISTS deletions(version TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, kind TEXT NOT NULL,
 file_id TEXT, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_history(
 id TEXT PRIMARY KEY, created REAL NOT NULL, filename TEXT NOT NULL,
 thumbnail BLOB NOT NULL, response TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS search_history_created ON search_history(created DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            if 'media_type' not in {row['name'] for row in connection.execute('PRAGMA table_info(files)')}:
                connection.execute("ALTER TABLE files ADD COLUMN media_type TEXT NOT NULL DEFAULT 'bif'")
            connection.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
            if connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "1":
                raise RuntimeError("Unsupported database schema")

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=60)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def rows(self, sql: str, values=()) -> list[dict]:
        with self.connect() as c:
            return [dict(row) for row in c.execute(sql, values)]

    def one(self, sql: str, values=()) -> dict | None:
        result = self.rows(sql, values)
        return result[0] if result else None

    def execute(self, sql: str, values=()):
        with self.connect() as c:
            c.execute(sql, values)

    def event(self, kind: str, message: str, file_id: str | None = None):
        with self.connect() as c:
            c.execute("INSERT INTO events(time,kind,file_id,message) VALUES(?,?,?,?)",
                      (time.time(), kind, file_id, message[:2000]))
            c.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-2000 FROM events)")

    def bind_model(self, manifest: dict):
        fingerprint = manifest["fingerprint"]
        with self.connect() as c:
            old = c.execute("SELECT value FROM meta WHERE key='model_fingerprint'").fetchone()
            if old and old[0] != fingerprint:
                raise RuntimeError("Model fingerprint differs from the database; refusing mixed embeddings")
            c.execute("INSERT OR IGNORE INTO meta VALUES('model_fingerprint',?)", (fingerprint,))
            c.execute("INSERT OR REPLACE INTO meta VALUES('model_manifest',?)",
                      (json.dumps(manifest, ensure_ascii=False),))

    def published_frame(self, frame_id: str) -> dict | None:
        return self.one("""SELECT fr.*, f.source,f.relpath,f.media_type,f.id AS file_id,
            v.size,v.mtime_ns FROM frames fr JOIN versions v ON v.id=fr.version
            JOIN files f ON f.id=v.file_id WHERE fr.id=? AND fr.valid=1
            AND f.active_version=fr.version AND v.status='ready'""", (frame_id,))

    def published_frames(self, ids: list[str]) -> dict[str, dict]:
        result = {}
        with self.connect() as c:
            for start in range(0, len(ids), 500):
                block = ids[start:start + 500]
                placeholders = ','.join('?' for _ in block)
                for row in c.execute(f"""SELECT fr.*,f.source,f.relpath,f.media_type,f.id AS file_id,
                    v.size,v.mtime_ns FROM frames fr JOIN versions v ON v.id=fr.version
                    JOIN files f ON f.id=v.file_id WHERE fr.id IN ({placeholders}) AND fr.valid=1
                    AND f.active_version=fr.version AND v.status='ready'""", block):
                    result[row['id']] = dict(row)
        return result

    def stats(self) -> dict:
        with self.connect() as c:
            return {
                "files": c.execute("SELECT COUNT(*) FROM files WHERE status!='deleted'").fetchone()[0],
                "ready_files": c.execute("SELECT COUNT(*) FROM files WHERE active_version IS NOT NULL").fetchone()[0],
                "frames": (c.execute("SELECT COALESCE(SUM(v.total),0) FROM files f JOIN versions v ON f.active_version=v.id WHERE v.status='ready'").fetchone()[0]
                           - c.execute("SELECT COUNT(*) FROM frames fr JOIN files f ON f.active_version=fr.version WHERE fr.valid=0").fetchone()[0]),
                "pending": c.execute("SELECT COUNT(*) FROM versions WHERE status IN ('pending','processing','failed') AND id IN (SELECT desired_version FROM files)").fetchone()[0],
                "errors": c.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL AND status!='deleted'").fetchone()[0],
                "bad_frames": c.execute("SELECT COUNT(*) FROM frames fr JOIN files f ON f.desired_version=fr.version WHERE fr.valid=0").fetchone()[0],
                "events": [dict(row) for row in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12")],
            }

    def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)

    def add_durations(self, results):
        for row in results:
            if 'media_type' not in row:
                from .images import media_type
                row['media_type'] = media_type(row.get('relpath', ''))
            if row['media_type'] == 'image':
                row['duration_ms'] = None
        versions = {row.get('version') for row in results if row.get('version') and 'duration_ms' not in row}
        with self.connect() as c:
            durations = {}
            for version in versions:
                last = c.execute('SELECT time_ms FROM frames WHERE version=? ORDER BY frame_no DESC LIMIT 1', (version,)).fetchone()
                durations[version] = last['time_ms'] if last else None
        for row in results:
            if 'duration_ms' not in row:
                row['duration_ms'] = durations.get(row.get('version'))
        return results

    def save_search(self, history_id: str, filename: str, thumbnail: bytes, response: dict):
        with self.connect() as c:
            c.execute('INSERT INTO search_history VALUES(?,?,?,?,?)',
                      (history_id, time.time(), filename[:255], thumbnail, json.dumps(response, ensure_ascii=False)))
