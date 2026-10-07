"""Native directory events; the indexing worker owns all database writes."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from frameseek.core.paths import original_relative
from frameseek.engine.monitoring import folders, includes, visit_directory
from frameseek.media.images import supported


class MediaWatcher(FileSystemEventHandler):
    def __init__(self, settings, db, wake):
        self.settings, self.db, self.wake = settings, db, wake
        self.observer = None
        self.signature = None
        self.roots = {}
        self.scope = []
        self.lock = threading.Lock()
        self.pending = {}
        self.rescan = False
        self.error = None

    @property
    def active(self):
        return self.observer is not None and self.observer.is_alive()

    @property
    def has_pending(self):
        with self.lock:
            return bool(self.pending or self.rescan)

    def configure(self, enabled):
        scope = folders(self.settings, self.db) if enabled else []
        roots = {name:root.resolve() for name,root in self.settings.sources.items()}
        signature = (tuple(sorted((item['source'],item['path']) for item in scope)),
                     tuple(sorted((name,str(root)) for name,root in roots.items())))
        if signature == self.signature:
            return
        self.stop()
        self.signature, self.scope, self.roots = signature, scope, roots
        self.error = None
        if not scope:
            return
        observer = Observer()
        try:
            paths = sorted({roots[item['source']] / item['path'] for item in scope}, key=lambda p:len(p.parts))
            selected = []
            for path in paths:
                # A missing selected folder can be created later under its existing parent.
                while not path.is_dir() and path != path.parent:
                    path = path.parent
                if not any(path == parent or parent in path.parents for parent in selected):
                    selected.append(path)
                    observer.schedule(self, str(path), recursive=True)
            observer.start()
            self.observer = observer
        except Exception as error:
            observer.stop()
            if observer.is_alive():
                observer.join(timeout=5)
            self.error = str(error)
            self.db.event('watch_error', '实时监听未启动，继续定时扫描：' + str(error))

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        self.signature = None
        with self.lock:
            self.pending.clear()
            self.rescan = False

    def on_any_event(self, event):
        # Reads performed by previews, hashing and decoding must not trigger another update.
        if event.event_type not in {'created','modified','deleted','moved'}:
            return
        if event.is_directory and event.event_type == 'modified':
            return
        for raw in [event.src_path, getattr(event, 'dest_path', '')]:
            if not raw:
                continue
            path = Path(raw)
            for source, root in sorted(self.roots.items(), key=lambda pair:-len(pair[1].parts)):
                try:
                    relative = original_relative(root, path, self.settings.escaped_paths)
                    path.resolve().relative_to(root)
                except ValueError:
                    continue
                if event.is_directory:
                    if visit_directory(self.scope, source, relative) or relative in {'', '.'}:
                        with self.lock:
                            self.rescan = True
                        self.wake.set()
                elif supported(path.name) and includes(self.scope, source, relative):
                    with self.lock:
                        self.pending[(source,relative)] = time.time() + .5
                    self.wake.set()
                break

    def drain(self):
        now = time.time()
        with self.lock:
            paths = set()
            for key, due in self.pending.items():
                if due <= now:
                    paths.add(key)
                if len(paths) >= 256:
                    break
            for key in paths:
                del self.pending[key]
            rescan, self.rescan = self.rescan, False
        return paths, rescan
