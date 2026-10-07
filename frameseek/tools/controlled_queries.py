"""Create labelled image distortions from published BIF frames, not real screenshot acceptance."""
from __future__ import annotations

import argparse
import io
import json
import random
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw

from frameseek.media.bif import read_frame
from frameseek.core.config import Settings
from frameseek.core.db import Database
from frameseek.core.paths import media_path


def main():
    from frameseek.core.console import configure_console
    configure_console()
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('--files-per-source', type=int, default=10)
    parser.add_argument('--output', type=Path, default=Path('reports/controlled-queries'))
    args = parser.parse_args()
    settings = Settings()
    db = Database(settings.db_path)
    args.output.mkdir(parents=True,exist_ok=True)
    labels = []
    rng = random.Random(20261006)
    for source in settings.sources:
        files = db.rows("SELECT active_version,relpath FROM files WHERE source=? AND status='ready'",(source,))
        for file in rng.sample(files,min(len(files),args.files_per_source)):
            rows = db.rows('SELECT * FROM frames WHERE version=? AND valid=1 ORDER BY frame_no',(file['active_version'],))
            if not rows:
                continue
            frame = rng.choice(rows)
            path = media_path(settings.sources[source],file['relpath'],settings.escaped_paths)
            with Image.open(io.BytesIO(read_frame(path,frame['offset'],frame['length']))) as opened:
                image = opened.convert('RGB')
            width,height = image.size
            subtitle = image.copy()
            draw = ImageDraw.Draw(subtitle)
            draw.rectangle((0,height*0.84,width,height),fill='black')
            draw.text((width*0.1,height*0.87),'TEST SUBTITLE',fill='white')
            variants = {'original':image,'scaled':image.resize((max(32,width*2),max(32,height*2))),
                        'compressed':image.copy(),'subtitle':subtitle,
                        'crop':image.crop((int(width*.05),int(height*.05),int(width*.95),int(height*.95)))}
            for name,value in variants.items():
                filename = f'{len(labels):04d}-{source}-{name}.jpg'
                value.save(args.output/filename,'JPEG',quality=25 if name=='compressed' else 92)
                labels.append({'image':filename,'source':source,'relpath':file['relpath'],
                               'time_ms':frame['time_ms'],'tolerance_ms':10000,'variant':name,
                               'origin':'controlled distortion of a BIF frame'})
                value.close()
    with (args.output/'labels.jsonl').open('w',encoding='utf-8') as stream:
        for row in labels:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    print(json.dumps({'queries':len(labels),'output':str(args.output.resolve()),
                      'note':'Controlled tests only; not real screenshot acceptance accuracy'}))


if __name__ == '__main__':
    main()
