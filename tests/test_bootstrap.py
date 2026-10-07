import json
from pathlib import Path
import sqlite3

import numpy as np
import pytest
from dotenv import dotenv_values
from safetensors.numpy import save_file

from frameseek.auth import verify_password
from frameseek.bootstrap import initialize
from frameseek.config import Settings
from frameseek.model import create_manifest


def manual_model(path):
    path.mkdir()
    (path/'config.json').write_text(json.dumps({'model_type':'dinov3_vit','hidden_size':1024,
                                              'num_hidden_layers':24,'patch_size':16}),encoding='utf-8')
    save_file({'test':np.ones(1,dtype=np.float32)},path/'model.safetensors')
    return path


def test_manual_weights_initialize_without_env_and_keep_login_on_repeat(tmp_path, monkeypatch):
    model=manual_model(tmp_path/'model'); data=tmp_path/'data'
    first=initialize(data,model)
    credentials=(data/'credentials.txt').read_bytes()
    auth=dotenv_values(data/'runtime/auth.env')
    password=credentials.decode().splitlines()[1].split(':',1)[1].strip()
    assert verify_password(password,auth['IMGS_PASSWORD_HASH'])
    assert json.loads((data/'runtime/qdrant/qdrant.yaml').read_text())['service']['api_key']==auth['IMGS_QDRANT_KEY']
    assert [path.name for path in (data/'runtime/qdrant').iterdir()] == ['qdrant.yaml']
    assert not (model/'manifest.json').exists()
    second=initialize(data,model)
    assert first['created_login'] and not second['created_login']
    assert (data/'credentials.txt').read_bytes()==credentials
    assert dotenv_values(data/'runtime/auth.env')==auth
    monkeypatch.delenv('IMGS_MODEL_MANIFEST',raising=False)
    settings=Settings(data=data,model=model,manifest_file=data/'runtime/model-manifest.json')
    assert settings.manifest['fingerprint']==first['manifest']['fingerprint']


def test_existing_model_revision_and_environment_credentials_are_preserved(tmp_path):
    model=manual_model(tmp_path/'model'); original=create_manifest(model,'official-revision')
    supplied={'IMGS_PASSWORD_HASH':'existing-hash','IMGS_SESSION_SECRET':'a'*64,'IMGS_QDRANT_KEY':'b'*64}
    data=tmp_path/'data'; result=initialize(data,model,'owner',supplied)
    assert result['manifest']==original
    assert dotenv_values(data/'runtime/auth.env')==supplied
    assert '使用已有登录密码' in (data/'credentials.txt').read_text(encoding='utf-8')


def test_changed_weights_cannot_replace_an_existing_index_manifest(tmp_path):
    model=manual_model(tmp_path/'model'); data=tmp_path/'data'; first=initialize(data,model)
    with sqlite3.connect(data/'metadata.sqlite3') as connection:
        connection.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        connection.execute('INSERT INTO meta VALUES(?,?)',('model_fingerprint',first['manifest']['fingerprint']))
    old_manifest=(data/'runtime/model-manifest.json').read_bytes()
    save_file({'test':np.zeros(1,dtype=np.float32)},model/'model.safetensors')
    with pytest.raises(RuntimeError,match='模型与现有索引不同'):
        initialize(data,model)
    assert (data/'runtime/model-manifest.json').read_bytes()==old_manifest


def test_missing_weights_or_lfs_pointer_do_not_initialize_login(tmp_path):
    with pytest.raises(RuntimeError,match='下载'):
        initialize(tmp_path/'data',tmp_path/'absent')
    model=manual_model(tmp_path/'model')
    (model/'model.safetensors').write_text('version https://git-lfs.github.com/spec/v1\n')
    with pytest.raises(RuntimeError,match='权重文件不完整'):
        initialize(tmp_path/'data',model)
    assert not (tmp_path/'data/credentials.txt').exists()
