"""Directory inventory and restartable removal of derived index data."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import PurePosixPath

from frameseek.core.preferences import Preferences, requested
from frameseek.media.scope import validate_scope


def boundary(relative, directory):
    return not directory or relative.startswith(directory + '/')


def inventory(runtime, source='', directory='', keyword='', offset=0):
    source, directory = validate_scope(runtime.settings.sources, source, directory)
    db = runtime.db
    condition, arguments = '1', []
    if source:
        condition += ' AND f.source=?'
        arguments.append(source)
    if directory:
        condition += ' AND substr(f.relpath,1,?)=?'
        arguments.extend([len(directory)+1,directory+'/'])
    rows = db.rows("""SELECT f.id,f.source,f.relpath,f.media_type,
        COALESCE((SELECT status FROM versions WHERE id=f.desired_version),f.status) AS status,
        COUNT(v.id) AS versions,
        COALESCE(SUM(CASE WHEN v.id=f.active_version AND v.status='ready' THEN
          v.total-(SELECT COUNT(*) FROM frames fr WHERE fr.version=v.id AND fr.valid=0) ELSE 0 END),0) AS vectors,
        MAX(v.created) AS updated
        FROM files f JOIN versions v ON v.file_id=f.id WHERE """ + condition + ' GROUP BY f.id', arguments
    )
    rows = [r for r in rows if (not source or r['source'] == source) and boundary(r['relpath'], directory)]
    chunks = db.rows('SELECT v.file_id,c.path FROM chunks c JOIN versions v ON v.id=c.version JOIN files f ON f.id=v.file_id WHERE ' + condition, arguments)
    selected = {r['id']: r for r in rows}
    for r in rows:
        r['bytes'] = 0
    for chunk in chunks:
        row = selected.get(chunk['file_id'])
        if row:
            path = (runtime.settings.data / chunk['path']).resolve()
            if path.is_relative_to((runtime.settings.data / 'vectors').resolve()):
                try:
                    row['bytes'] += path.stat().st_size
                except FileNotFoundError:
                    pass
    groups = {}
    for row in rows:
        parent = PurePosixPath(row['relpath']).parent
        # Include every ancestor, including roots, regardless of current monitoring scope.
        for path in [parent, *parent.parents]:
            name = '' if str(path) == '.' else str(path)
            if directory and name != directory and not name.startswith(directory + '/'):
                continue
            key = (row['source'], name)
            group = groups.setdefault(key, {'source':key[0], 'directory':name, 'files':0,
                                           'bif':0, 'images':0, 'vectors':0, 'bytes':0})
            group['files'] += 1
            group['images' if row['media_type'] == 'image' else 'bif'] += 1
            group['vectors'] += row['vectors']
            group['bytes'] += row['bytes']
    for group in groups.values():
        root = runtime.settings.sources.get(group['source'])
        group['path'] = (root.resolve().as_posix() if root else group['source']).rstrip('/') + ('/' + group['directory'] if group['directory'] else '')
    matches = sorted((g for g in groups.values() if keyword.casefold() in g['path'].casefold() and
                      (keyword or (not source and not g['directory']) or
                       (source and (g['directory'] == directory or
                        str(PurePosixPath(g['directory']).parent) == (directory or '.'))))), key=lambda g:g['path'])
    rows.sort(key=lambda r:r['relpath'])
    return {'directories':matches[:200], 'directories_truncated':len(matches)>200,
            'files':rows[offset:offset+50] if source else [], 'total_files':len(rows),
            'offset':offset, 'has_more':bool(source and len(rows)>offset+50),
            'jobs':db.rows('SELECT * FROM index_removals ORDER BY created DESC LIMIT 30')}


def enqueue(runtime, source, directory, exclude=True):
    source, directory = validate_scope(runtime.settings.sources, source, directory)
    if not source:
        raise ValueError('请选择一个目录')
    ident = str(uuid.uuid4())
    # The job itself excludes this scope immediately, before the writer yields.
    with runtime.db.connect() as c:
        if c.execute("SELECT 1 FROM index_removals WHERE status!='done' AND source=? AND directory=?", (source,directory)).fetchone():
            raise ValueError('该目录已有清理任务')
        c.execute('INSERT INTO index_removals(id,source,directory,status,created) VALUES(?,?,?,?,?)',
                  (ident,source,directory,'pending',time.time()))
        if exclude:
            profile = requested(runtime.settings, runtime.db)
            path = runtime.settings.sources[source].resolve().as_posix().rstrip('/') + ('/'+directory if directory else '')
            if path not in profile['excluded_directories']:
                profile['excluded_directories'].append(path)
            profile = Preferences.model_validate(profile).model_dump()
            c.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)", (json.dumps(profile,ensure_ascii=False),))
            runtime.settings.excluded_directories = profile['excluded_directories']
    runtime.worker.configuration_changed()
    runtime.db.event('index_removal_queued', f'已安排删除目录索引：{source}/{directory}')
    return {'id':ident, 'status':'pending'}


def process_removal(runtime):
    db = runtime.db
    job = db.one("SELECT * FROM index_removals WHERE status!='done' AND retry_at<=? ORDER BY created LIMIT 1", (time.time(),))
    if not job:
        return False
    indexer = runtime.get_indexer()
    try:
        with indexer.operation_lock:
            if job['status'] == 'pending':
                files = [r for r in db.rows('SELECT id,relpath FROM files WHERE source=?', (job['source'],)) if boundary(r['relpath'],job['directory'])]
                with db.connect() as c:
                    for file in files:
                        c.execute('INSERT OR IGNORE INTO index_removal_versions SELECT ?,id,file_id FROM versions WHERE file_id=?', (job['id'],file['id']))
                        c.execute("UPDATE files SET active_version=NULL,desired_version=NULL,status='deleted',error=NULL WHERE id=?", (file['id'],))
                    c.execute("UPDATE index_removals SET status='running',total=(SELECT COUNT(*) FROM index_removal_versions WHERE job=?) WHERE id=?", (job['id'],job['id']))
            with runtime.worker.task_lock:
                task = runtime.worker.suspended_task
                if task and task.get('version') and db.one(
                        'SELECT 1 FROM index_removal_versions WHERE job=? AND version=?', (job['id'],task['version'])):
                    db.execute("DELETE FROM meta WHERE key='suspended_task'")
                    runtime.worker.suspended_task = None
                if runtime.worker.resume_version and db.one(
                        'SELECT 1 FROM index_removal_versions WHERE job=? AND version=?', (job['id'],runtime.worker.resume_version)):
                    runtime.worker.resume_version = ''
            for item in db.rows('SELECT * FROM index_removal_versions WHERE job=? LIMIT 32', (job['id'],)):
                if runtime.worker.stop_event.is_set():
                    return True
                version = str(uuid.UUID(item['version']))
                runtime.get_store().delete_version(version)
                root = (runtime.settings.data/'vectors').resolve()
                path = (root/version).resolve()
                if path.parent != root:
                    raise ValueError('向量分块目录无效')
                if path.exists():
                    shutil.rmtree(path)
                with db.connect() as c:
                    for table, column in [('frames','version'),('chunks','version'),('deletions','version'),('versions','id')]:
                        c.execute(f'DELETE FROM {table} WHERE {column}=?',(version,))
                    c.execute('DELETE FROM index_removal_versions WHERE job=? AND version=?',(job['id'],version))
                    c.execute("UPDATE index_removals SET completed=completed+1,error=NULL WHERE id=?",(job['id'],))
            if not db.one('SELECT 1 FROM index_removal_versions WHERE job=?',(job['id'],)):
                db.execute("UPDATE index_removals SET status='done',error=NULL WHERE id=?", (job['id'],))
                db.event('index_removal_done',f"目录索引已删除：{job['source']}/{job['directory']}，原文件保留")
    except Exception as error:
        db.execute('UPDATE index_removals SET error=?,retry_at=? WHERE id=?',(str(error),time.time()+30,job['id']))
        db.event('index_removal_error',str(error))
    return True
