from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import nullcontext
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .config import DIMENSION, MODEL_ID, Settings


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(4 * 1024 * 1024):
            value.update(data)
    return value.hexdigest()


def prepare_model(directory: Path, revision: str = "main") -> dict:
    """Download only from the authorized official repository. Never log tokens."""
    from huggingface_hub import HfApi, snapshot_download
    directory.mkdir(parents=True, exist_ok=True)
    resolved = HfApi().model_info(MODEL_ID, revision=revision).sha
    snapshot_download(MODEL_ID, revision=resolved, local_dir=directory,
                      allow_patterns=["config.json", "*.safetensors", "*.safetensors.index.json"])
    return create_manifest(directory, resolved)


def create_manifest(directory: Path, revision: str | None = None, destination: Path | None = None) -> dict:
    if revision is None:
        existing = directory / 'manifest.json'
        revision = json.loads(existing.read_text(encoding='utf-8')).get('revision','local') if existing.is_file() else 'local'
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if (config.get("model_type") != "dinov3_vit" or config.get("hidden_size") != 1024
            or config.get("num_hidden_layers") != 24 or config.get("patch_size") != 16):
        raise RuntimeError("Expected a DINOv3 ViT-L/16 Hugging Face model directory")
    weights = sorted(directory.glob("*.safetensors"))
    if not weights:
        raise RuntimeError("No safetensors weights found")
    manifest = {"model_id": MODEL_ID, "dimension": DIMENSION, "revision": revision,
                "feature": "last_hidden_state[:,0]", "input_size": 256,
                "normalization": {"mean": [0.485, 0.456, 0.406], "std": [0.229, 0.224, 0.225]},
                "resize": "torchvision-v2-square-bilinear-antialias",
                "weights": {p.name: digest(p) for p in [directory / "config.json", *weights]}}
    manifest["fingerprint"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    destination = destination or directory / 'manifest.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.tmp')
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, destination)
    return manifest


class InferenceGate:
    """One model owner; queued screenshot queries get the next inference slot."""
    def __init__(self):
        self.condition = threading.Condition()
        self.busy = False
        self.search_waiters = 0

    def acquire(self, search: bool):
        with self.condition:
            if search:
                self.search_waiters += 1
            try:
                while self.busy or (not search and self.search_waiters):
                    self.condition.wait()
                self.busy = True
            finally:
                if search:
                    self.search_waiters -= 1

    def release(self):
        with self.condition:
            self.busy = False
            self.condition.notify_all()


class Embedder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.gate = InferenceGate()
        self.net = None
        self.batch_size = settings.batch
        self.openvino = None

    def load(self):
        if self.net is not None:
            return
        import torch
        from torchvision.transforms import v2
        manifest = self.settings.manifest
        torch.set_num_threads(self.settings.cpu_threads)
        self.torch = torch
        self.transform = v2.Compose([
            v2.ToImage(), v2.Resize((256, 256), antialias=True),
            v2.ToDtype(torch.float32, scale=True),
            v2.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ])
        if self.settings.device.startswith('openvino:'):
            from .inference import load_openvino
            self.openvino = load_openvino(self.settings)
            self.net = self.openvino
            # The validated Intel model has a fixed one-image input.
            self.batch_size = 1
        else:
            for name, expected in manifest['weights'].items():
                if digest(self.settings.model / name) != expected:
                    raise RuntimeError(f'Model checksum mismatch: {name}')
            from transformers import AutoModel
            self.net = AutoModel.from_pretrained(self.settings.model, local_files_only=True,
                                                 use_safetensors=True).eval().to(self.settings.device)

    def prepare_images(self, images: list[Image.Image]):
        """CPU preprocessing may run ahead of GPU inference in a producer thread."""
        if self.net is None:
            self.gate.acquire(False)
            try:
                self.load()
            finally:
                self.gate.release()
        tensors = [self.transform(ImageOps.exif_transpose(image).convert('RGB')) for image in images]
        prepared = self.torch.stack(tensors)
        return prepared.pin_memory() if str(self.settings.device).startswith('cuda') else prepared

    def _encode_prepared(self, prepared) -> np.ndarray:
        pieces = []
        start = 0
        while start < len(prepared):
            count = min(self.batch_size, len(prepared) - start)
            inputs = result = None
            try:
                if self.openvino is not None:
                    values = self.openvino(prepared[start:start+count].cpu().numpy())
                    if values.shape != (count, DIMENSION) or not np.isfinite(values).all():
                        raise RuntimeError('核显输出无效，请改用 FP32 后重启应用')
                    lengths = np.linalg.norm(values, axis=1, keepdims=True)
                    if not np.isfinite(lengths).all() or (lengths < 1e-12).any():
                        raise RuntimeError('核显输出无效，请改用 FP32 后重启应用')
                    pieces.append((values / lengths).astype(np.float32))
                    start += count
                    continue
                inputs = prepared[start:start + count].to(self.settings.device, non_blocking=True)
                device_type = 'cuda' if self.settings.device.startswith('cuda') else 'cpu'
                mixed = (self.torch.autocast(device_type, dtype=self.torch.float16)
                         if self.settings.precision == 'fp16' else nullcontext())
                with self.torch.inference_mode(), mixed:
                    result = self.net(pixel_values=inputs).last_hidden_state[:, 0]
                # Residuals/LayerScale retain FP32; normalization and index vectors always use FP32.
                result = result.float()
                with self.torch.inference_mode():
                    result = self.torch.nn.functional.normalize(result, dim=1)
                values = result.cpu().numpy()
                if values.shape[1] != DIMENSION or not np.isfinite(values).all():
                    raise RuntimeError('Invalid model output')
                pieces.append(values)
                start += count
            except self.torch.cuda.OutOfMemoryError:
                if count == 1:
                    raise
                self.batch_size = max(1, count // 2)
                inputs = result = None
                self.torch.cuda.empty_cache()
        return np.concatenate(pieces).astype(np.float32) if pieces else np.empty((0, DIMENSION), np.float32)

    def embed_prepared(self, prepared, search: bool = False) -> np.ndarray:
        self.gate.acquire(search)
        try:
            return self._encode_prepared(prepared)
        finally:
            self.gate.release()

    def embed(self, images: list[Image.Image], search: bool = False) -> np.ndarray:
        self.gate.acquire(search)
        try:
            self.load()
            pieces = []
            start = 0
            while start < len(images):
                count = min(self.batch_size, len(images) - start)
                prepared = self.prepare_images(images[start:start + count])
                pieces.append(self._encode_prepared(prepared))
                start += count
            return np.concatenate(pieces).astype(np.float32) if pieces else np.empty((0, DIMENSION), np.float32)
        finally:
            self.gate.release()
