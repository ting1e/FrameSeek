"""First-run initialization for the image-based Compose deployment."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sqlite3

from dotenv import dotenv_values
import portalocker

from frameseek.core.auth import password_hash
from frameseek.engine.model import create_manifest


KEYS = ('IMGS_PASSWORD_HASH', 'IMGS_SESSION_SECRET', 'IMGS_QDRANT_KEY')


def write_private(path: Path, text: str):
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def application_permissions(data: Path):
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return
    # Only the application's data volume is writable; model/media mounts are read-only.
    os.chown(data, 10001, 10001)
    for directory, children, files in os.walk(data, followlinks=False):
        if Path(directory) == data:
            children[:] = [name for name in children if name != 'qdrant']
        for name in children + files:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, 10001, 10001)


def initialize(data: Path, model: Path, username='admin', overrides: dict | None = None) -> dict:
    runtime = data / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(runtime / 'bootstrap.lock', timeout=60):
        if not (model / 'config.json').is_file() or not list(model.glob('*.safetensors')):
            raise RuntimeError('请先将完整 DINOv3 模型下载到 models/dinov3-vitl16。')
        from safetensors import safe_open
        for weight in model.glob('*.safetensors'):
            try:
                with safe_open(weight, framework='numpy') as tensors:
                    tensors.keys()
            except Exception as error:
                raise RuntimeError('权重文件不完整，请确认下载了实际 safetensors 文件。') from error
        # Write metadata outside the read-only weight directory. Existing revisions remain unchanged.
        manifest_path = runtime / 'model-manifest.json'
        staged_manifest = runtime / 'model-manifest.pending.json'
        manifest = create_manifest(model, destination=staged_manifest)
        if (data / 'metadata.sqlite3').is_file():
            with sqlite3.connect(data / 'metadata.sqlite3', timeout=60) as database:
                exists = database.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
                fingerprint = database.execute("SELECT value FROM meta WHERE key='model_fingerprint'").fetchone() if exists else None
            if fingerprint and fingerprint[0] != manifest['fingerprint']:
                staged_manifest.unlink()
                raise RuntimeError('模型与现有索引不同，请使用建立该索引时的完整模型目录。')
        staged_manifest.replace(manifest_path)
        auth_path = runtime / 'auth.env'
        saved = dotenv_values(auth_path) if auth_path.exists() else {}
        supplied = overrides or {}
        generated_password = None
        values = {key:supplied.get(key) or saved.get(key) for key in KEYS}
        if not values['IMGS_PASSWORD_HASH']:
            generated_password = secrets.token_urlsafe(18)
            values['IMGS_PASSWORD_HASH'] = password_hash(generated_password)
        values['IMGS_SESSION_SECRET'] = values['IMGS_SESSION_SECRET'] or secrets.token_hex(32)
        values['IMGS_QDRANT_KEY'] = values['IMGS_QDRANT_KEY'] or secrets.token_hex(32)
        write_private(auth_path, ''.join(f'{key}={values[key]}\n' for key in KEYS))
        credentials = data / 'credentials.txt'
        if generated_password:
            write_private(credentials, f'用户名: {username}\n密码: {generated_password}\n')
        elif credentials.exists():
            lines = credentials.read_text(encoding='utf-8').splitlines()
            if saved.get('IMGS_PASSWORD_HASH') != values['IMGS_PASSWORD_HASH']:
                lines = ['密码: 使用已有登录密码']
            else:
                lines = [line for line in lines if not line.startswith('用户名:')]
            write_private(credentials, f'用户名: {username}\n' + '\n'.join(lines) + '\n')
        else:
            write_private(credentials, f'用户名: {username}\n密码: 使用已有登录密码\n')
        qdrant_config = runtime / 'qdrant'
        qdrant_config.mkdir(exist_ok=True)
        write_private(qdrant_config / 'qdrant.yaml', json.dumps({'service': {'api_key':values['IMGS_QDRANT_KEY']}}, indent=2) + '\n')
        # Remove the old duplicate config after migrating an existing data directory.
        (runtime / 'qdrant.yaml').unlink(missing_ok=True)
        (data / 'vectors').mkdir(exist_ok=True)
        (data / 'qdrant').mkdir(exist_ok=True)
        application_permissions(data)
    print('首次启动配置已就绪，登录信息保存在 /data/credentials.txt。')
    return {'manifest':manifest, 'created_login':generated_password is not None}
