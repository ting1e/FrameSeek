"""Find a same-directory MP4 for a published BIF hit."""
from frameseek.core.paths import media_path
from frameseek.integrations.emby import bif_filename


def local_mp4(settings, row):
    if row.get('media_type') == 'image':
        raise ValueError('普通图片不支持视频播放。')
    root = settings.sources[row['source']]
    bif = media_path(root, row['relpath'], settings.escaped_paths)
    name = bif_filename(bif.name).casefold()
    matches = []
    for path in bif.parent.iterdir():
        if path.suffix.lower() == '.mp4' and path.stem.casefold() == name and path.is_file():
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            matches.append(path)
    if not matches:
        raise ValueError('Emby 未找到对应视频，同目录下也没有同名 MP4。')
    if len(matches) != 1:
        raise ValueError('同目录下有多个同名 MP4，无法确定播放文件。')
    return matches[0]
