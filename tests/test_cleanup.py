import csv

from conftest import build, write_bif
from frameseek.cleanup import prune_deleted


def test_prune_confirmed_missing_vectors_and_keep_existing_or_changed(runtime, tmp_path):
    root = runtime.settings.sources['sda']
    deleted = write_bif(root / 'remove.bif')
    changed = write_bif(root / 'mismatch.bif')
    write_bif(root / 'keep.bif')
    build(runtime)
    version = runtime.db.one("SELECT v.* FROM versions v JOIN files f ON f.desired_version=v.id WHERE f.relpath='remove.bif'")
    directory = runtime.settings.data / 'vectors' / version['id']
    deleted.unlink()
    changed.unlink()
    manifest = tmp_path / 'deletions.csv'
    with manifest.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['磁盘', 'BIF相对路径', '本地已删除', 'SHA256'])
        writer.writeheader()
        for name, sha in [('remove.bif', version['sha256']), ('mismatch.bif', 'wrong'), ('keep.bif', '')]:
            writer.writerow({'磁盘': 'sda', 'BIF相对路径': name, '本地已删除': 'True', 'SHA256': sha})
    result = prune_deleted(runtime, manifest, tmp_path / 'cleanup')
    assert result['removed_files'] == result['removed_versions'] == 1
    assert result['removed_processed_frames'] == 3
    assert result['skipped'] == {'sha_mismatch': 1, 'file_still_exists': 1}
    assert runtime.get_store().count() == 6
    assert not directory.exists()
    assert runtime.db.one('SELECT * FROM versions WHERE id=?', (version['id'],)) is None
    assert runtime.db.one("SELECT status FROM files WHERE relpath='remove.bif'")['status'] == 'deleted'
    assert (tmp_path / 'cleanup' / 'metadata-before.sqlite3').exists()
    assert runtime.db.one('SELECT COUNT(*) AS n FROM deletions')['n'] == 0
    repeated = prune_deleted(runtime, manifest, tmp_path / 'cleanup-repeat')
    assert repeated['removed_versions'] == 0
    assert runtime.get_store().count() == 6
