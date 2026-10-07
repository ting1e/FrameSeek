"""Optional private SSH settings. No host-specific defaults are shipped."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath


def configuration(required=False) -> dict:
    path = Path(os.getenv('IMGS_SYNC_CONFIG', 'sync.local.json'))
    if not path.is_file():
        if required:
            raise ValueError('SSH 同步未配置：请复制 sync.example.json 为 sync.local.json 并填写连接及目录。')
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('SSH configuration must be a JSON object')
    if required:
        if not isinstance(value.get('host'), str) or not value['host'].strip():
            raise ValueError('SSH host is required')
        if not isinstance(value.get('user'), str) or not value['user'].strip():
            raise ValueError('SSH user is required')
        port = value.get('port', 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid SSH port')
    return value


def roots(required=False) -> dict[str, str]:
    value = configuration(required).get('directories', [])
    if not value:
        if required:
            raise ValueError('SSH media directory list is required')
        return {}
    if not isinstance(value, list) or any(not isinstance(root, str) or not PurePosixPath(root).is_absolute() or '..' in PurePosixPath(root).parts for root in value):
        raise ValueError('SSH directories must be an absolute POSIX path list')
    from frameseek.core.config import directory_sources
    return {key: path.as_posix() for key, path in directory_sources(json.dumps(value)).items()}
