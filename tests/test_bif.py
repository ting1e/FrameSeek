import io
import struct
from types import SimpleNamespace

import pytest

from imgsearch.bif import BifError, parse, read_frame
from imgsearch.paths import canonical_mtime, media_path, escaped_part, original_relative
from conftest import write_bif


def test_cross_platform_timestamp_precision():
    nas = SimpleNamespace(st_mtime_ns=1234567890123456798)
    ntfs = SimpleNamespace(st_mtime_ns=1234567890123456700)
    assert canonical_mtime(nas)==canonical_mtime(ntfs)
    modified = SimpleNamespace(st_mtime_ns=1234567890123456800)
    assert canonical_mtime(modified)!=canonical_mtime(nas)


def test_timestamp_multiplier_and_last_frame(tmp_path):
    path = write_bif(tmp_path / 'normal.bif', multiplier=1250)
    frames = parse(path)
    assert [f.time_ms for f in frames] == [0, 12500, 25000]
    assert frames[-1].offset + frames[-1].length == path.stat().st_size
    assert read_frame(path, frames[-1].offset, frames[-1].length).startswith(b'\xff\xd8')
    path = write_bif(tmp_path / 'zero.bif', multiplier=0)
    assert parse(path)[1].time_ms == 10000


@pytest.mark.parametrize('corruption', ['header', 'version', 'count', 'sentinel', 'offset', 'time', 'truncated'])
def test_malformed_bif_rejected(tmp_path, corruption):
    path = write_bif(tmp_path / 'bad.bif')
    data = bytearray(path.read_bytes())
    if corruption == 'header': data[0] = 0
    if corruption == 'version': struct.pack_into('<I', data, 8, 9)
    if corruption == 'count': struct.pack_into('<I', data, 12, 0xffffffff)
    if corruption == 'sentinel': struct.pack_into('<I', data, 64 + 3 * 8, 2)
    if corruption == 'offset': struct.pack_into('<I', data, 68, 1)
    if corruption == 'time': struct.pack_into('<I', data, 64 + 2 * 8, 1)
    if corruption == 'truncated': data = data[:70]
    path.write_bytes(data)
    with pytest.raises(BifError): parse(path)


def test_path_escape_and_windows_round_trip(tmp_path):
    with pytest.raises(ValueError): media_path(tmp_path, '../secret')
    with pytest.raises(ValueError): media_path(tmp_path, '/etc/passwd')
    with pytest.raises(ValueError): media_path(tmp_path, '..\\secret')
    name = 'CON/画面:100%?.bif'
    safe = media_path(tmp_path, name, escaped=True)
    assert original_relative(tmp_path.resolve(), safe, True) == name
    assert ':' not in safe.name and '?' not in safe.name


def test_symlink_escape(tmp_path):
    root = tmp_path / 'media'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    link = root / 'link'
    try: link.symlink_to(outside, target_is_directory=True)
    except OSError: pytest.skip('Symlink privilege unavailable')
    with pytest.raises(ValueError): media_path(root, 'link/image.bif')
