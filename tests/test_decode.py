import numpy as np
import pytest

from conftest import write_bif
from frameseek.media.bif import parse
from frameseek.media.decode import decoded_batches, prepared_batches
from frameseek.media.decode import decode_frame
from conftest import jpeg


def test_prepared_prefetch_order_bad_frame_and_cleanup(tmp_path):
    path = write_bif(tmp_path / 'pool.bif', ('red', 'green', 'blue', 'yellow', 'purple'), corrupt=1)
    frames = [dict(id=str(f.index), offset=f.offset, length=f.length) for f in parse(path)]
    opened = []
    def prepare(images):
        opened.extend(images)
        return np.stack([np.asarray(image).mean((0, 1)) for image in images])
    pipeline = prepared_batches(decoded_batches(path, frames, 2, 4), prepare)
    batches = list(pipeline)
    assert [f['id'] for good, _, _ in batches for f in good] == ['0', '2', '3', '4']
    assert [ident for _, bad, _ in batches for _, ident in bad] == ['1']
    assert sum(len(data) for _, _, data in batches) == 4
    for image in opened:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


def test_prepared_prefetch_early_close_releases_images(tmp_path):
    path = write_bif(tmp_path / 'close.bif', ('red',) * 20)
    frames = [dict(id=str(f.index), offset=f.offset, length=f.length) for f in parse(path)]
    opened = []
    def prepare(images):
        opened.extend(images)
        return np.zeros((len(images), 3))
    pipeline = prepared_batches(decoded_batches(path, frames, 2, 4), prepare)
    next(pipeline)
    pipeline.close()
    assert len(opened) <= 6  # Current batch plus two prefetched batches.
    for image in opened:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


def test_oversized_jpeg_is_rejected_before_pixel_allocation(tmp_path, monkeypatch):
    import struct
    from PIL import Image
    data = bytearray(jpeg('red'))
    position = data.index(b'\xff\xc0')
    struct.pack_into('>HH', data, position + 5, 4000, 6000)
    path = tmp_path / 'large.bif'
    path.write_bytes(bytes(64) + data)
    called = []
    monkeypatch.setattr(Image.Image, 'load', lambda image: called.append(1))
    frame, image, error = decode_frame(path, {'offset':64, 'length':len(data)})
    assert image is None and '20 million' in error
    assert not called


def test_non_jpeg_bif_frame_is_recorded_as_bad(tmp_path):
    import io
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (64, 36)).save(stream, format='PNG')
    path = tmp_path / 'wrong-format.bif'
    path.write_bytes(bytes(64) + stream.getvalue())
    frame, image, error = decode_frame(path, {'offset':64, 'length':len(stream.getvalue())})
    assert image is None and 'JPEG' in error
