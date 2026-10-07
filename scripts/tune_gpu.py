"""Compare bounded decode pipelines using the same real BIF frames and FP32 model."""
import json
import random
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from imgsearch.bif import parse
from imgsearch.config import Settings
from imgsearch.decode import decoded_batches, prepared_batches
from imgsearch.model import Embedder


def main():
    load_dotenv('.env')
    settings = Settings()
    paths = sorted(p for root in settings.sources.values() for p in root.rglob('*.bif'))
    random.Random(20261006).shuffle(paths)
    selected = []
    for path in paths:
        frames = parse(path)
        random.Random(42).shuffle(frames)
        remaining = 256 - sum(len(frames) for _, frames in selected)
        selected.append((path, [dict(offset=f.offset, length=f.length) for f in frames[:min(64, remaining)]]))
        if sum(len(frames) for _, frames in selected) >= 256:
            break
    model = Embedder(settings)
    model.load()
    torch = model.torch
    results = []
    reference = None
    for batch, workers, preprocessed in [(16, 4, False), (16, 4, True), (32, 4, True)]:
        model.batch_size = batch
        torch.cuda.reset_peak_memory_stats()
        durations = []
        wait_durations = []
        vectors = []
        for repeat in range(4):
            start = time.perf_counter()
            vectors = []
            waiting = 0
            for path, frames in selected:
                batches = decoded_batches(path, frames, batch, workers)
                if preprocessed:
                    batches = prepared_batches(batches, model.prepare_images)
                try:
                    while True:
                        wait_start = time.perf_counter()
                        decoded = next(batches, None)
                        waiting += time.perf_counter() - wait_start
                        if decoded is None:
                            break
                        if preprocessed:
                            _, bad, tensors = decoded
                            if bad:
                                raise RuntimeError('Invalid JPEG in tuning sample')
                            vectors.append(model.embed_prepared(tensors))
                            continue
                        images = [image for _, image, _ in decoded if image is not None]
                        try:
                            if len(images) != len(decoded):
                                raise RuntimeError('Invalid JPEG in tuning sample')
                            vectors.append(model.embed(images))
                        finally:
                            for image in images:
                                image.close()
                finally:
                    batches.close()
            torch.cuda.synchronize()
            if repeat:
                durations.append(time.perf_counter() - start)
                wait_durations.append(waiting)
        combined = np.concatenate(vectors)
        if reference is None:
            reference = combined
        result = dict(batch=batch, effective_batch=model.batch_size, decode_workers=workers,
                      preprocessed_prefetch=preprocessed,
                      waiting_for_batches_seconds=wait_durations,
                      frames=len(combined), frames_per_second=len(combined) / np.mean(durations),
                      repeat_seconds=durations, peak_gpu_allocated_mib=torch.cuda.max_memory_allocated()/1024**2,
                      min_cosine_with_baseline=float(np.min(np.sum(reference*combined, axis=1))))
        results.append(result)
        print(json.dumps(result), flush=True)
    Path('reports/gpu-preprocess-tuning.json').write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
