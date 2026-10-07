"""Inference benchmark usable locally or in a temporary, approved NAS test container."""
from __future__ import annotations

import argparse
import io
import json
import platform
import random
import time
from pathlib import Path

import numpy as np
from PIL import Image

from frameseek.media.bif import parse, read_frame
from frameseek.core.config import Settings
from frameseek.engine.model import Embedder


def main():
    from frameseek.core.console import configure_console
    configure_console()
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--batch', type=int, default=1)
    parser.add_argument('--frames', type=int, default=128)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--source', action='append', default=[])
    parser.add_argument('--output', default='reports/inference-benchmark.json')
    args = parser.parse_args()
    if args.frames < 1 or args.repeat < 1 or args.batch < 1:
        parser.error('frames, repeat and batch must be positive')
    settings = Settings(device=args.device, batch=args.batch)
    roots = [Path(p) for p in args.source] or list(settings.sources.values())
    files = sorted(p for root in roots for p in root.rglob('*.bif') if p.is_file() and not p.is_symlink())
    random.Random(20261006).shuffle(files)
    selected = []
    errors = []
    for path in files:
        try:
            frames = parse(path)
            random.Random(42).shuffle(frames)
            selected.extend((path, frame) for frame in frames[:min(8, len(frames))])
        except Exception as error:
            errors.append({'path':str(path),'error':str(error)})
        if len(selected) >= args.frames:
            break
    selected = selected[:args.frames]
    if not selected:
        raise RuntimeError('No valid BIF frames available')
    model = Embedder(settings)
    start = time.perf_counter(); model.load(); load_seconds = time.perf_counter() - start
    with Image.open(io.BytesIO(read_frame(selected[0][0], selected[0][1].offset, selected[0][1].length))) as image:
        model.embed([image.convert('RGB')])
    durations = []
    for repeat in range(args.repeat):
        start = time.perf_counter()
        for cursor in range(0, len(selected), args.batch):
            images = []
            for path, frame in selected[cursor:cursor+args.batch]:
                with Image.open(io.BytesIO(read_frame(path, frame.offset, frame.length))) as opened:
                    opened.load(); images.append(opened.convert('RGB'))
            try: model.embed(images)
            finally:
                for image in images: image.close()
        elapsed = time.perf_counter() - start
        durations.append(elapsed)
        print(json.dumps({'repeat':repeat+1,'frames':len(selected),'seconds':elapsed,'frames_per_second':len(selected)/elapsed}), flush=True)
    peak_rss = None
    if platform.system() != 'Windows':
        import resource
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if platform.system() == 'Linux' else 1)
    mean = float(np.mean(durations))
    result = {'device':args.device,'platform':platform.platform(),'batch':args.batch,'frames':len(selected),
              'load_seconds':load_seconds,'repeat_seconds':durations,'frames_per_second':len(selected)/mean,
              'estimated_1000_frames_seconds':1000*mean/len(selected),'peak_rss_bytes':peak_rss,
              'includes':'BIF span read + JPEG decode + preprocess + embedding; excludes Qdrant writes',
              'model_fingerprint':settings.manifest['fingerprint'],'errors':errors}
    result['processor'] = platform.processor()
    if Path('/proc/cpuinfo').is_file():
        result['processor'] = next((line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                                    if line.startswith('model name')),result['processor'])
    destination = Path(args.output); destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
