"""Source-relative monitoring scope, independent of retained search data."""
from frameseek.core.preferences import requested


def folders(settings, db):
    from frameseek.media.directories import contains, normalized
    profile = requested(settings, db)
    exclusions = []
    for source, root in settings.sources.items():
        root = root.resolve().as_posix()
        for path in profile['excluded_directories']:
            if contains(path, root):
                exclusions.append({'source':source, 'path':''})
            elif contains(root, path):
                exclusions.append({'source':source, 'path':normalized(path)[len(normalized(root).rstrip('/'))+1:], 'case_insensitive':len(root)>1 and root[1]==':'})
    return MonitorScope(profile['monitor_folders'], exclusions)


class MonitorScope(list):
    def __init__(self, selected, excluded):
        super().__init__(selected)
        self.excluded = excluded


def excluded(scope, source, relative):
    for item in getattr(scope, 'excluded', []):
        name = relative.casefold() if item.get('case_insensitive') else relative
        if item['source'] == source and (not item['path'] or name == item['path'] or name.startswith(item['path'] + '/')):
            return True
    return False


def includes(scope, source, relative):
    return not excluded(scope, source, relative) and any(item['source'] == source and
               (not item['path'] or relative.startswith(item['path'] + '/')) for item in scope)


def visit_directory(scope, source, relative):
    return not excluded(scope, source, relative) and any(item['source'] == source and
               (not item['path'] or relative == item['path'] or
                relative.startswith(item['path'] + '/') or item['path'].startswith(relative + '/')) for item in scope)


def sql_scope(scope):
    clauses, params = [], []
    for item in scope:
        if not item['path']:
            clauses.append('f.source=?')
            params.append(item['source'])
        else:
            prefix = item['path'] + '/'
            clauses.append('(f.source=? AND substr(f.relpath,1,?)=?)')
            params.extend([item['source'], len(prefix), prefix])
    selected = '(' + ' OR '.join(clauses) + ')' if clauses else '0'
    for item in getattr(scope, 'excluded', []):
        if not item['path']:
            selected += ' AND f.source!=?'
            params.append(item['source'])
        else:
            prefix = item['path'] + '/'
            column = 'lower(f.relpath)' if item.get('case_insensitive') else 'f.relpath'
            selected += f' AND NOT (f.source=? AND substr({column},1,?)=?)'
            params.extend([item['source'], len(prefix), prefix])
    return selected, params
