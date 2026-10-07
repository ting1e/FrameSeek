import json

import pytest

from frameseek.remote import configuration, roots


def test_no_ssh_host_or_directories_are_shipped_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv('IMGS_SYNC_CONFIG', str(tmp_path / 'absent.json'))
    assert configuration() == {} and roots() == {}
    with pytest.raises(ValueError, match='同步未配置'):
        configuration(required=True)


def test_private_directory_list_and_connection_settings(tmp_path, monkeypatch):
    path = tmp_path / 'sync.local.json'
    path.write_text(json.dumps({'host':'nas.example.test','user':'tester','port':2222,
                               'directories':['/mnt/sda/video','/mnt/sdc/video']}), encoding='utf-8')
    monkeypatch.setenv('IMGS_SYNC_CONFIG', str(path))
    assert configuration(required=True)['port'] == 2222
    assert roots(required=True) == {'sda':'/mnt/sda/video','sdc':'/mnt/sdc/video'}
    path.write_text(json.dumps({'host':'nas.example.test','user':'tester','port':2222,
                               'directories':['relative/path']}), encoding='utf-8')
    with pytest.raises(ValueError, match='absolute POSIX'):
        roots(required=True)
