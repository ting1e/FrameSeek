import json

import pytest

from frameseek.core.config import Settings, directory_sources
from frameseek.engine.indexer import file_id


def test_existing_source_ids_are_preserved_for_flat_directory_configuration():
    local = directory_sources('["bif/sda", "bif/sdc"]')
    nas = directory_sources('["/mnt/test/sda/video", "/mnt/test/sdc/video"]')
    assert list(local) == list(nas) == ['sda','sdc']
    assert file_id(next(iter(local)), 'folder/movie.bif') == file_id(next(iter(nas)), 'folder/movie.bif')


def test_arbitrary_directory_ids_are_stable_and_list_has_priority(tmp_path, monkeypatch):
    paths = [str(tmp_path/'videos'), str(tmp_path/'archive')]
    first = directory_sources(json.dumps(paths))
    second = directory_sources(json.dumps(list(reversed(paths))))
    assert first == second
    assert all(key.startswith('dir_') for key in first)
    monkeypatch.setenv('IMGS_MEDIA_DIRECTORIES',json.dumps(paths))
    monkeypatch.setenv('IMGS_SOURCES','{"legacy":"elsewhere"}')
    settings = Settings(data=tmp_path/'data')
    assert settings.sources == first
    assert {entry['source'] for entry in settings.monitor_folders} == set(first)


@pytest.mark.parametrize('value',['{}','[]','[null]','[""]','["bif/sda","bif/sda"]'])
def test_bad_media_directory_lists_are_rejected(value):
    with pytest.raises(ValueError): directory_sources(value)
