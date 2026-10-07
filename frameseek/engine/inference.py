"""Explicit device selection and the validated Intel OpenVINO embedding backend."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np


def openvino_directory(settings) -> Path:
    return settings.openvino_model or settings.model.parent / (settings.model.name + '-openvino')


def check_openvino_manifest(settings, verify=False):
    from frameseek.engine.model import digest
    directory = openvino_directory(settings)
    try:
        manifest = json.loads((directory / 'openvino-manifest.json').read_text(encoding='utf-8'))
        if manifest['source_fingerprint'] != settings.manifest['fingerprint']:
            raise ValueError('核显模型与当前 DINOv3 模型不一致，请重新转换')
        if manifest['input_shape'] != [1, 3, 256, 256] or manifest['dimension'] != 1024:
            raise ValueError('核显模型的输入或向量维度不匹配')
        for name in ('embedding.xml', 'embedding.bin'):
            if not (directory / name).is_file():
                raise ValueError('核显模型文件不完整')
            if verify and digest(directory / name) != manifest['weights'][name]:
                raise ValueError('核显模型校验失败，请重新转换')
        return directory
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError('尚未准备核显模型，请运行 frameseek prepare-openvino 并挂载模型目录') from error


@lru_cache(maxsize=1)
def hardware_devices():
    cuda, intel = [], []
    if importlib.util.find_spec('torch'):
        try:
            import torch
            cuda = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
        except (RuntimeError, OSError):
            pass
    if importlib.util.find_spec('openvino'):
        try:
            import openvino as ov
            core = ov.Core()
            intel = [(d, core.get_property(d, 'FULL_DEVICE_NAME')) for d in core.available_devices if d.startswith('GPU')]
        except (RuntimeError, OSError):
            pass
    return cuda, intel


def inference_options(settings):
    cuda, intel = hardware_devices()
    options = [dict(value='cpu', label='CPU', available=True, reason='')]
    options.extend(dict(value='cuda' if i == 0 else f'cuda:{i}', label=f'NVIDIA GPU · {name}', available=True, reason='')
                   for i, name in enumerate(cuda))
    if not cuda:
        options.append(dict(value='cuda', label='NVIDIA GPU', available=False, reason='未检测到可用的 NVIDIA GPU'))
    if not importlib.util.find_spec('openvino'):
        reason = '未安装核显推理依赖'
    elif not intel:
        reason = '未检测到 Intel 核显；Docker 需挂载 /dev/dri 并授予访问权限'
    else:
        try:
            check_openvino_manifest(settings)
            reason = ''
        except ValueError as error:
            reason = str(error)
    options.append(dict(value='openvino:GPU', label='Intel 核显' + (f' · {intel[0][1]}' if intel else ''),
                        available=not reason, reason=reason))
    # Preserve explicit NVIDIA device indices and the diagnostic CPU backend.
    if settings.device == 'cuda:0':
        options.append(dict(value='cuda:0', label='NVIDIA GPU 0', available=bool(cuda), reason='' if cuda else '未检测到 NVIDIA GPU'))
    if settings.device == 'openvino:CPU':
        options.append(dict(value='openvino:CPU', label='CPU（OpenVINO）', available=True, reason=''))
    return options


def validate_inference_choice(settings, device):
    option = next((o for o in inference_options(settings) if o['value'] == device), None)
    if option is None or not option['available']:
        raise ValueError(option['reason'] if option else '所选推理设备不可用')


def export_openvino(settings, destination=None):
    from frameseek.engine.model import digest
    import torch
    import openvino as ov
    from transformers import AutoModel
    source = settings.manifest
    for name, expected in source['weights'].items():
        if digest(settings.model / name) != expected:
            raise RuntimeError('Model checksum mismatch: ' + name)

    class CLS(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, pixels):
            features = self.model(pixel_values=pixels).last_hidden_state[:, 0].float()
            return torch.nn.functional.normalize(features, dim=1)

    torch.set_num_threads(settings.cpu_threads)
    net = CLS(AutoModel.from_pretrained(settings.model, local_files_only=True, use_safetensors=True).eval())
    converted = ov.convert_model(net, example_input=torch.zeros(1, 3, 256, 256), input=[1, 3, 256, 256])
    directory = Path(destination) if destination else openvino_directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='export-', dir=directory) as folder:
        temporary = Path(folder)
        ov.save_model(converted, temporary / 'embedding.xml', compress_to_fp16=True)
        manifest = {'source_fingerprint': source['fingerprint'], 'dimension': 1024,
                    'input_shape': [1, 3, 256, 256], 'openvino_version': ov.__version__,
                    'weights': {name: digest(temporary / name) for name in ('embedding.xml', 'embedding.bin')}}
        (temporary / 'openvino-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        for name in ('embedding.xml', 'embedding.bin', 'openvino-manifest.json'):
            os.replace(temporary / name, directory / name)
    return {'directory': str(directory.resolve()), **manifest}


class OpenVINOEncoder:
    def __init__(self, compiled):
        self.compiled = compiled
        self.request = compiled.create_infer_request()

    def __call__(self, pixels):
        self.request.infer({0: np.ascontiguousarray(pixels, dtype=np.float32)})
        return self.request.get_output_tensor(0).data.astype(np.float32, copy=True)


def load_openvino(settings):
    import openvino as ov
    directory = check_openvino_manifest(settings, verify=True)
    device = settings.device.split(':', 1)[1]
    core = ov.Core()
    if not any(d == device or d.startswith(device + '.') for d in core.available_devices):
        raise RuntimeError('所选 Intel 核显不可用，请检查设备挂载、权限和驱动')
    config = {'PERFORMANCE_HINT': 'LATENCY', 'INFERENCE_PRECISION_HINT': settings.precision.replace('fp', 'f')}
    if device == 'CPU':
        config['INFERENCE_NUM_THREADS'] = settings.cpu_threads
    elif settings.precision == 'fp16':
        config['ACTIVATIONS_SCALE_FACTOR'] = 8.0
    compiled = core.compile_model(directory / 'embedding.xml', device, config)
    actual = compiled.get_property('EXECUTION_DEVICES')
    if not actual or any(not d.startswith(device) for d in actual):
        raise RuntimeError('实际推理设备与选择不一致')
    if list(compiled.input().shape) != [1, 3, 256, 256] or list(compiled.output().shape) != [1, 1024]:
        raise RuntimeError('核显模型输入或输出形状不匹配')
    return OpenVINOEncoder(compiled)
