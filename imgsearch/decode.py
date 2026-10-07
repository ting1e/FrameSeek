"""Bounded, ordered JPEG prefetch; GPU and database retain one owner."""
from __future__ import annotations

import io
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

from . import bif
from .images import IMAGE_FORMATS, read_image


def decode_frame(path, frame):
    try:
        standalone = frame.get('media_type') == 'image'
        data = read_image(path) if standalone else bif.read_frame(path, frame['offset'], frame['length'])
        with Image.open(io.BytesIO(data)) as image:
            formats = IMAGE_FORMATS if standalone else {'JPEG'}
            if image.format not in formats or image.width * image.height > 20_000_000:
                raise ValueError('Unsupported image format or image exceeds 20 million pixels' if standalone else
                                 'BIF frame must be a JPEG with at most 20 million pixels')
            image.load()
            if standalone:
                with ImageOps.exif_transpose(image) as oriented:
                    return frame, oriented.convert('RGB'), None
            return frame, image.convert('RGB'), None
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        return frame, None, str(error)[:1000]


def decoded_batches(path, frames, batch_size, workers):
    """Caller closes yielded images. At most two batches wait ahead of inference."""
    if workers == 1:
        for start in range(0, len(frames), batch_size):
            yield [decode_frame(path, frame) for frame in frames[start:start + batch_size]]
        return
    pending = deque()
    source = iter(frames)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bif-jpeg') as pool:
        def submit_one():
            frame = next(source, None)
            if frame is not None:
                pending.append(pool.submit(decode_frame, path, frame))
        for _ in range(min(len(frames), batch_size * 2)):
            submit_one()
        try:
            while pending:
                batch = []
                try:
                    for _ in range(min(batch_size, len(pending))):
                        batch.append(pending.popleft().result())
                        submit_one()
                except BaseException:
                    for _, image, _ in batch:
                        if image is not None:
                            image.close()
                    raise
                yield batch
        finally:
            # Even a failed inference must release images decoded by queued work.
            for future in pending:
                try:
                    _, image, _ = future.result()
                    if image is not None:
                        image.close()
                except Exception:
                    pass


def prepared_batches(decoded, prepare):
    """Two pending batches include decode, resize and normalization before inference."""
    def prepare_next():
        batch = next(decoded, None)
        if batch is None:
            return None
        images = [image for _, image, _ in batch if image is not None]
        try:
            good = [frame for frame, image, _ in batch if image is not None]
            bad = [(error, frame['id']) for frame, image, error in batch if image is None]
            return good, bad, prepare(images) if images else None
        finally:
            for image in images:
                image.close()
    pending = deque()
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='bif-preprocess') as pool:
            pending.extend(pool.submit(prepare_next) for _ in range(2))
            while pending:
                batch = pending.popleft().result()
                if batch is None:
                    break
                pending.append(pool.submit(prepare_next))
                yield batch
    finally:
        decoded.close()
