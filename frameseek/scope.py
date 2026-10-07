from collections import Counter
from pathlib import PurePosixPath
from .images import validate_media_type


def validate_scope(sources, source='', directory=''):
    source, directory = source.strip(), directory.strip()
    if source and source not in sources:
        raise ValueError('未知的数据来源')
    if directory and not source:
        raise ValueError('选择目录时必须指定数据来源')
    if directory and (directory.startswith('/') or '\\' in directory or ':' in directory or any(part in {'.', '..'} for part in directory.split('/'))):
        raise ValueError('目录必须是来源内的相对路径，不能越界')
    return source, directory.rstrip('/')


def published_files(db, source='', media_type='all'):
    validate_media_type(media_type)
    condition, arguments = "v.status='ready'", []
    if source:
        condition += ' AND f.source=?'
        arguments.append(source)
    if media_type != 'all':
        condition += ' AND f.media_type=?'
        arguments.append(media_type)
    return db.rows('SELECT f.id,f.source,f.relpath,f.media_type FROM files f JOIN versions v ON v.id=f.active_version WHERE ' + condition, arguments)


def scope_file_ids(db, source, directory='', keyword='', roots=None):
    rows = published_files(db, source)
    if directory:
        prefix = directory + '/'
        return [row['id'] for row in rows if row['relpath'].startswith(prefix)]
    keyword = keyword.strip().casefold()
    return [row['id'] for row in rows if keyword in
            ((roots or {}).get(row['source'], row['source']) +
             ('/' + str(PurePosixPath(row['relpath']).parent) if str(PurePosixPath(row['relpath']).parent) != '.' else '')).casefold()]


def directories(db, source='', keyword='', roots=None, media_type='all'):
    counts = Counter()
    bif_counts = Counter()
    for row in published_files(db, source, media_type):
        parent = PurePosixPath(row['relpath']).parent
        while str(parent) != '.':
            counts[(row['source'], str(parent))] += 1
            if row.get('media_type', 'bif') == 'bif':
                bif_counts[(row['source'], str(parent))] += 1
            parent = parent.parent
    matches = [{'source':s, 'directory':d, 'file_count':n, 'bif_count':bif_counts[(s,d)], 'image_count':n-bif_counts[(s,d)], 'display_path':f"{(roots or {}).get(s,s)}/{d}"} for (s,d),n in sorted(counts.items()) if keyword.casefold() in f"{(roots or {}).get(s,s)}/{d}".casefold()]
    return {'directories':matches[:50], 'has_more':len(matches)>50}
