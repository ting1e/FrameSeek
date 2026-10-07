"""Evaluate labelled screenshots against published frames and exact vector search."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from PIL import Image

from frameseek.core.config import Settings
from frameseek.engine.search import Runtime


def matches(hit: dict, label: dict) -> bool:
    return (hit['source'] == label['source'] and hit['relpath'] == label['relpath']
            and abs(hit['time_ms'] - label['time_ms']) <= label.get('tolerance_ms', 10000))


def evaluate(runtime: Runtime, labels: list[dict], base: Path) -> dict:
    rows = []
    store = runtime.get_store()
    if not store.count():
        raise ValueError('Cannot evaluate an empty index')
    for label in labels:
        with Image.open(base / label['image']) as opened:
            opened.load()
            image = opened.convert('RGB')
        try:
            start = time.perf_counter()
            vector = runtime.embedder.embed([image], search=True)[0]
            inference_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            ann = store.search(vector, min(100, store.count()))
            ann_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            exact = store.search(vector, min(100, store.count()), exact=True)
            exact_ms = (time.perf_counter() - start) * 1000
            published = runtime.db.published_frames([str(p.id) for p in ann])
            hits = [published[str(p.id)] for p in ann if str(p.id) in published
                    and runtime.available(published[str(p.id)])]
            k = min(20, len(exact))
            reference = {str(p.id) for p in exact[:k]}
            recall = len(reference & {str(p.id) for p in ann[:k]}) / k if k else 0.0
            rows.append({'image': label['image'], 'top1': any(matches(h,label) for h in hits[:1]),
                         'top5': any(matches(h,label) for h in hits[:5]), 'ann_recall_at20': recall,
                         'inference_ms': inference_ms, 'ann_ms': ann_ms, 'exact_ms': exact_ms,
                         'total_ms': inference_ms + ann_ms})
        finally:
            image.close()
    if not rows:
        raise ValueError('Evaluation needs at least one labelled screenshot')
    return {'mode':runtime.settings.mode, 'model_fingerprint':runtime.settings.manifest['fingerprint'],
            'queries':len(rows), 'vectors':store.count(),
            'top1':float(np.mean([r['top1'] for r in rows])),
            'top5':float(np.mean([r['top5'] for r in rows])),
            'ann_recall_at20':float(np.mean([r['ann_recall_at20'] for r in rows])),
            'warm_p95_ms':float(np.percentile([r['total_ms'] for r in rows[1:] or rows],95)),
            'first_query_ms':rows[0]['total_ms'], 'rows':rows,
            'notes':'Frame-level, without folding. Cold performance requires restarting services. '
                    'Use real screenshots for acceptance; generated distortions are only controlled tests.'}


def main():
    from frameseek.core.console import configure_console
    configure_console()
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('labels', type=Path)
    parser.add_argument('--output', type=Path, default=Path('reports/search-evaluation.json'))
    args = parser.parse_args()
    with args.labels.open(encoding='utf-8') as stream:
        labels = [json.loads(line) for line in stream if line.strip()]
    runtime = Runtime(Settings())
    try:
        result = evaluate(runtime, labels, args.labels.parent)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False,indent=2))
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
