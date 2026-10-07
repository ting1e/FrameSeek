"""Supported image files share the versioned indexing pipeline with BIF frames."""
from pathlib import Path

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff'}
IMAGE_FORMATS = {'JPEG', 'PNG', 'WEBP', 'BMP', 'GIF', 'TIFF'}
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def media_type(path):
    return 'image' if Path(path).suffix.lower() in IMAGE_EXTENSIONS else 'bif'


def supported(path):
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS | {'.bif'}


def validate_media_type(value):
    if value not in {'all', 'bif', 'image'}:
        raise ValueError('搜索类型应为全部、仅 BIF 或仅图片')
    return value


def read_image(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds 64 MiB')
    return data
