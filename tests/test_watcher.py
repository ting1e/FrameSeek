import time

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileOpenedEvent, DirMovedEvent

from conftest import build, write_bif
from frameseek.engine.watcher import MediaWatcher


def eventually(check, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.05)
    assert check(), 'Background update did not finish in time'


def test_targeted_reconciliation_stability_and_delete_isolation(runtime):
    root = runtime.settings.sources['sda']
    one = write_bif(root/'one.bif')
    two = write_bif(root/'two.bif')
    build(runtime)
    indexer = runtime.get_indexer()
    previous = runtime.db.one("SELECT active_version FROM files WHERE relpath='one.bif'")['active_version']
    runtime.settings.stable_seconds = 120
    write_bif(one, colors=('yellow',))
    two.unlink()  # Unrelated paths must not be implicitly treated as deleted.
    result = indexer.scan(changes={('sda','one.bif')})
    assert result['modified'] == 1 and result['queued'] == 0 and result['deleted'] == 0
    assert runtime.db.one("SELECT active_version FROM files WHERE relpath='two.bif'")['active_version']
    runtime.db.execute("UPDATE files SET stable_since=? WHERE relpath='one.bif'", (time.time()-121,))
    assert indexer.scan(changes={('sda','one.bif')})['queued'] == 1
    indexer.cleanup()
    for job in indexer.pending():
        indexer.process_version(job['id'])
    assert runtime.db.one("SELECT active_version FROM files WHERE relpath='one.bif'")['active_version'] != previous
    one.unlink()
    assert indexer.scan(changes={('sda','one.bif')})['deleted'] == 1
    indexer.cleanup()
    assert runtime.db.one("SELECT status FROM files WHERE relpath='two.bif'")['status'] != 'deleted'


def test_event_scope_filter_and_rename(runtime):
    watcher = runtime.worker.watcher
    root = runtime.settings.sources['sda'].resolve()
    watcher.roots = {'sda':root}
    watcher.scope = [{'source':'sda','path':'series'}]
    watcher.on_any_event(FileCreatedEvent(str(root/'series-backup'/'outside.bif')))
    watcher.on_any_event(FileCreatedEvent(str(root/'series'/'ignore.txt')))
    watcher.on_any_event(FileOpenedEvent(str(root/'series'/'read.bif')))
    assert not watcher.has_pending
    watcher.on_any_event(FileMovedEvent(str(root/'series'/'old.bif'),str(root/'series'/'new.bif')))
    assert set(watcher.pending) == {('sda','series/old.bif'),('sda','series/new.bif')}
    watcher.on_any_event(DirMovedEvent(str(root/'series'),str(root/'archive')))
    assert watcher.rescan


def test_native_watcher_updates_without_full_rescans_and_respects_pause(runtime, monkeypatch):
    runtime.settings.auto_update = True
    runtime.settings.interval = 3600
    runtime.settings.stable_seconds = 1
    indexer = runtime.get_indexer()
    full_scans = []
    original = indexer.scan
    def scan(*args, **kwargs):
        if kwargs.get('changes') is None:
            full_scans.append(time.time())
        return original(*args, **kwargs)
    monkeypatch.setattr(indexer, 'scan', scan)
    runtime.worker.start()
    try:
        eventually(lambda: runtime.worker.watcher.active and full_scans and runtime.worker.activity == 'idle')
        path = write_bif(runtime.settings.sources['sda']/'live.bif')
        eventually(lambda: runtime.db.stats()['frames'] == 3)
        runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('paused','true')")
        runtime.worker.wake_event.set()
        eventually(lambda: runtime.worker.activity == 'paused')
        write_bif(path, colors=('yellow',))
        eventually(lambda: runtime.worker.watcher.has_pending)
        assert runtime.db.stats()['frames'] == 3
        runtime.db.execute("UPDATE meta SET value='false' WHERE key='paused'")
        runtime.worker.wake_event.set()
        eventually(lambda: runtime.db.stats()['frames'] == 1)
        renamed = path.with_name('renamed.bif')
        path.rename(renamed)
        eventually(lambda: runtime.db.one("SELECT active_version FROM files WHERE relpath='renamed.bif'") and
                   runtime.db.one("SELECT active_version FROM files WHERE relpath='renamed.bif'")['active_version'])
        eventually(lambda: runtime.get_store().count() == 1)
        renamed.unlink()
        eventually(lambda: runtime.db.stats()['frames'] == 0 and runtime.get_store().count() == 0)
        assert len(full_scans) == 1
        runtime.settings.auto_update = False
        runtime.worker.configuration_changed()
        eventually(lambda: not runtime.worker.watcher.active and runtime.worker.activity == 'disabled')
    finally:
        runtime.worker.stop()
    assert not runtime.worker.watcher.active


def test_watch_start_failure_preserves_periodic_mode(runtime, monkeypatch):
    import frameseek.engine.watcher as module
    class BrokenObserver:
        def schedule(self, *args, **kwargs):
            raise OSError('watch limit reached')
        def stop(self):
            pass
        def is_alive(self):
            return False
    monkeypatch.setattr(module, 'Observer', BrokenObserver)
    watcher = MediaWatcher(runtime.settings, runtime.db, runtime.worker.wake_event)
    watcher.configure(True)
    assert not watcher.active and 'watch limit' in watcher.error
    assert runtime.db.one("SELECT kind FROM events ORDER BY id DESC LIMIT 1")['kind'] == 'watch_error'
    assert runtime.get_indexer().scan()['scan_errors'] == 0


def test_daily_reconciliation_default_and_saved_interval_compatibility(runtime, monkeypatch):
    from frameseek.core.config import Settings
    from frameseek.core.preferences import Preferences
    monkeypatch.delenv('IMGS_SCAN_INTERVAL', raising=False)
    assert Preferences().scan_interval_seconds == 86400
    settings = Settings(data=runtime.settings.data, model=runtime.settings.model, sources=runtime.settings.sources)
    assert settings.interval == 86400
    assert Preferences(scan_interval_seconds=600).scan_interval_seconds == 600
