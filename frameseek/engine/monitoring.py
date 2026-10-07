"""Source-relative monitoring scope, independent of retained search data."""
from frameseek.core.preferences import requested


def folders(settings, db):
    return requested(settings, db)['monitor_folders']


def includes(scope, source, relative):
    return any(item['source'] == source and
               (not item['path'] or relative.startswith(item['path'] + '/')) for item in scope)


def visit_directory(scope, source, relative):
    return any(item['source'] == source and
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
    return '(' + ' OR '.join(clauses) + ')' if clauses else '0', params
