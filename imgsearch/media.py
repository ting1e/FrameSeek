"""Persistent media roots and web-configured monitoring directories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .paths import media_path

REGISTRY_KEY = 'media_registry'


def normalized(value):
    text = str(value).replace('\\', '/').rstrip('/') or '/'
    return text.casefold() if re.match(r'^[A-Za-z]:/', text) else text


def contains(root, path):
    root, path = normalized(root), normalized(path)
    return path == root or path.startswith(root.rstrip('/') + '/')


def restore_sources(registry, configured):
    sources = {}
    for source, record in registry.items():
        candidates = [record['root'], *record.get('aliases', [])]
        if source in configured:
            candidates.insert(0, str(configured[source].resolve()))
        sources[source] = next((Path(path).resolve() for path in candidates if Path(path).is_dir()),
                               Path(record['root']))
    return {**configured, **sources}


def registry_for(settings, db, aliases=None):
    row = db.one('SELECT value FROM meta WHERE key=?', (REGISTRY_KEY,))
    registry = json.loads(row['value']) if row else {}
    for source, root in settings.sources.items():
        previous = registry.get(source, {})
        current = root.resolve().as_posix()
        alternate = list(previous.get('aliases', []))
        if previous.get('root') and previous['root'] != current:
            alternate.append(previous['root'])
        if (aliases or {}).get(source):
            alternate.append(aliases[source])
        registry[source] = {'root':current, 'aliases':list(dict.fromkeys(path for path in alternate if path != current))}
    return registry


def remember_sources(settings, db, aliases=None):
    registry = registry_for(settings, db, aliases)
    if registry:
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                   (REGISTRY_KEY, json.dumps(registry, ensure_ascii=False)))


def monitor_directories(settings, folders):
    paths = []
    for folder in folders:
        root = settings.sources.get(folder['source'])
        if root is not None:
            path = root.resolve().as_posix().rstrip('/') + ('/' + folder['path'] if folder['path'] else '')
            paths.append(path or '/')
    # A parent selection may cover several retained roots. Display it once.
    return [path for path in dict.fromkeys(paths)
            if not any(normalized(other) != normalized(path) and contains(other, path) for other in paths)]


def plan_directories(settings, db, directories, aliases=None):
    from .config import directory_sources
    import hashlib
    sources = dict(settings.sources)
    registry = registry_for(settings, db, aliases)
    selected = []
    indexed_sources = {row['source'] for row in db.rows('SELECT DISTINCT source FROM files')}
    previous = {normalized(path) for path in monitor_directories(settings, settings.monitor_folders)}
    for value in directories:
        value = value.strip().replace('\\', '/')
        if (not value or '\x00' in value or any(part == '..' for part in value.split('/'))
                or any(char in value for char in '*?')
                or not (value.startswith('/') or re.match(r'^[A-Za-z]:/', value))):
            raise ValueError('监控目录应为完整目录，不支持通配符或 ..')
        candidates = []
        for source, root in sources.items():
            record = registry[source]
            for candidate in [root.resolve().as_posix(), *record.get('aliases', [])]:
                if contains(candidate, value):
                    candidates.append((len(normalized(candidate)), source, candidate))
        if candidates:
            _, source, matched = max(candidates)
            relative = value[len(matched.rstrip('/')):].lstrip('/')
            directory = media_path(sources[source], relative, settings.escaped_paths) if relative else sources[source].resolve()
        else:
            directory = Path(value).expanduser().resolve()
            recovered = None
            for parent in [directory, *directory.parents]:
                candidate = next(iter(directory_sources(json.dumps([str(parent)]))))
                if candidate in indexed_sources and (candidate not in sources or not sources[candidate].is_dir()):
                    recovered = candidate, parent
                    break
            if recovered:
                source, root = recovered
                old = registry.get(source, {})
                alternate = [*old.get('aliases', []), *([old['root']] if old.get('root') else [])]
                sources[source] = root
                registry[source] = {'root':root.as_posix(), 'aliases':list(dict.fromkeys(alternate))}
                relative = directory.relative_to(root).as_posix()
                if relative == '.':
                    relative = ''
                if not directory.is_dir() and normalized(directory.as_posix()) not in previous:
                    raise ValueError('目录不存在或不可访问，请确认已在 Compose 中挂载：' + value)
                selected.append((source, relative, directory))
                continue
            source = next(iter(directory_sources(json.dumps([str(directory)]))))
            if source in sources:
                source = 'dir_' + hashlib.sha256(os.path.normcase(str(directory)).encode()).hexdigest()[:20]
            sources[source] = directory
            registry[source] = {'root':directory.as_posix(), 'aliases':[]}
            relative = ''
        if not directory.is_dir() and normalized(directory.as_posix()) not in previous:
            raise ValueError('目录不存在或不可访问，请确认已在 Compose 中挂载：' + value)
        selected.append((source, relative, directory))
    folders = []
    for source, relative, directory in selected:
        folders.append({'source':source, 'path':relative})
        # Keep existing child roots responsible for their original file IDs.
        for child, root in sources.items():
            if child != source and contains(directory.as_posix(), root.resolve().as_posix()):
                folders.append({'source':child, 'path':''})
    compact = []
    for folder in folders:
        if folder in compact:
            continue
        if any(other['source'] == folder['source'] and other != folder and
               (not other['path'] or folder['path'].startswith(other['path'] + '/')) for other in folders):
            continue
        compact.append(folder)
    return sources, compact, registry
