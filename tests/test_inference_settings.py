import json
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from frameseek.config import Settings
from frameseek.inference import check_openvino_manifest, load_openvino
from frameseek.model import Embedder, digest
from test_web import client, login


def test_precision_persists_and_only_applies_after_restart(runtime, client):
    headers = login(client)
    before = client.get('/api/settings').json()
    changed = {**before['saved'], 'precision': 'fp16'}
    result = client.put('/api/settings', json=changed, headers=headers)
    assert result.status_code == 200 and result.json()['restart_required']
    state = client.get('/api/settings').json()
    assert state['saved']['precision'] == 'fp16'
    assert state['running']['precision'] == 'fp32'
    restarted = Settings(data=runtime.settings.data, model=runtime.settings.model)
    assert restarted.precision == 'fp16' and restarted.device == 'cpu'
    for bad in ({'precision': 'int8'}, {'device': 'auto'}, {'device': 'GPU'}):
        assert client.put('/api/settings', json={**changed, **bad}, headers=headers).status_code == 422
    assert client.get('/api/settings').json()['saved']['precision'] == 'fp16'


def test_unavailable_gpu_cannot_be_saved_and_old_clients_preserve_precision(runtime, client, monkeypatch):
    monkeypatch.setattr('frameseek.inference.hardware_devices', lambda: ([], []))
    headers = login(client)
    before = client.get('/api/settings').json()['saved']
    result = client.put('/api/settings', json={**before, 'device': 'openvino:GPU'}, headers=headers)
    assert result.status_code == 422
    assert client.get('/api/settings').json()['saved'] == before
    assert client.put('/api/settings', json={**before, 'precision': 'fp16'}, headers=headers).status_code == 200
    legacy = {k: v for k, v in before.items() if k not in {'device', 'precision'}}
    assert client.put('/api/settings', json=legacy, headers=headers).status_code == 200
    assert client.get('/api/settings').json()['saved']['precision'] == 'fp16'


def test_legacy_saved_settings_keep_explicit_cuda_device(runtime):
    from frameseek.preferences import current, requested
    old = current(runtime.settings)
    old.pop('device'); old.pop('precision')
    runtime.db.execute("INSERT OR REPLACE INTO meta VALUES('runtime_preferences',?)", (json.dumps(old),))
    runtime.settings.device = 'cuda:1'
    assert requested(runtime.settings, runtime.db)['device'] == 'cuda:1'
    restored = Settings(data=runtime.settings.data, device='cuda:1')
    assert restored.device == 'cuda:1' and restored.precision == 'fp32'


def test_gpu_and_precision_can_be_selected_together_without_changing_running_model(runtime, client, monkeypatch):
    monkeypatch.setattr('frameseek.inference.hardware_devices', lambda: (['Test GPU'], []))
    headers = login(client)
    initial = client.get('/api/settings').json()
    assert any(option['value'] == 'cuda' and option['available'] for option in initial['inference_options'])
    changed = {**initial['saved'], 'device': 'cuda', 'precision': 'fp16'}
    response = client.put('/api/settings', json=changed, headers=headers)
    assert response.status_code == 200 and response.json()['restart_required']
    state = client.get('/api/settings').json()
    assert (state['saved']['device'], state['saved']['precision']) == ('cuda', 'fp16')
    assert (state['running']['device'], state['running']['precision']) == ('cpu', 'fp32')
    restarted = Settings(data=runtime.settings.data, model=runtime.settings.model)
    assert (restarted.device, restarted.precision) == ('cuda', 'fp16')


def make_manifest(runtime, tmp_path):
    directory = tmp_path / 'openvino'
    directory.mkdir()
    for name in ('embedding.xml', 'embedding.bin'):
        (directory / name).write_bytes(b'test-artifact')
    manifest = {'source_fingerprint': runtime.settings.manifest['fingerprint'],
                'input_shape': [1, 3, 256, 256], 'dimension': 1024,
                'weights': {n: digest(directory / n) for n in ('embedding.xml', 'embedding.bin')}}
    (directory / 'openvino-manifest.json').write_text(json.dumps(manifest))
    runtime.settings.openvino_model = directory
    return directory


def test_openvino_artifact_is_bound_to_source_and_checked_for_corruption(runtime, tmp_path):
    directory = make_manifest(runtime, tmp_path)
    assert check_openvino_manifest(runtime.settings, verify=True) == directory
    (directory / 'embedding.bin').write_bytes(b'changed')
    with pytest.raises(ValueError, match='校验失败'):
        check_openvino_manifest(runtime.settings, verify=True)
    source = json.loads((directory / 'openvino-manifest.json').read_text())
    source['source_fingerprint'] = 'different-model'
    (directory / 'openvino-manifest.json').write_text(json.dumps(source))
    with pytest.raises(ValueError, match='不一致'):
        check_openvino_manifest(runtime.settings)


def test_intel_fp16_enables_scaling_and_refuses_cpu_fallback(runtime, tmp_path, monkeypatch):
    import openvino as ov
    make_manifest(runtime, tmp_path)
    runtime.settings.device = 'openvino:GPU'; runtime.settings.precision = 'fp16'
    captured = {}
    compiled = SimpleNamespace(get_property=lambda key: ['GPU.0'],
                               input=lambda: SimpleNamespace(shape=[1, 3, 256, 256]),
                               output=lambda: SimpleNamespace(shape=[1, 1024]),
                               create_infer_request=lambda: object())
    def compile_model(model, device, config):
        captured.update(device=device, config=config)
        return compiled
    monkeypatch.setattr(ov, 'Core', lambda: SimpleNamespace(available_devices=['CPU', 'GPU.0'], compile_model=compile_model))
    load_openvino(runtime.settings)
    assert captured['device'] == 'GPU'
    assert captured['config']['ACTIVATIONS_SCALE_FACTOR'] == 8.0
    assert captured['config']['INFERENCE_PRECISION_HINT'] == 'f16'
    compiled.get_property = lambda key: ['CPU']
    with pytest.raises(RuntimeError, match='实际推理设备'):
        load_openvino(runtime.settings)


@pytest.mark.parametrize('value', [np.nan, np.inf, 0.0])
def test_invalid_intel_embeddings_are_rejected_before_index_writes(runtime, value):
    runtime.settings.device = 'openvino:GPU'
    model = Embedder(runtime.settings)
    model.torch = torch
    model.openvino = lambda pixels: np.full((len(pixels), 1024), value, np.float32)
    with pytest.raises(RuntimeError, match='输出无效'):
        model.embed_prepared(torch.ones(1, 3, 256, 256))


def test_fp16_autocast_preserves_fp32_layerscale_and_normalized_vectors(runtime):
    runtime.settings.precision = 'fp16'
    model = Embedder(runtime.settings)
    model.torch = torch
    observed = []
    class SensitiveNet(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.linear = torch.nn.Linear(3, 1024, bias=False)
            torch.nn.init.constant_(self.linear.weight, 1.0)
            self.scale = torch.nn.Parameter(torch.full((1024,), 3.0))
        def forward(self, pixel_values):
            linear = self.linear(pixel_values[:, :, 0, 0])
            observed.append(linear.dtype)
            # Representable MatMul output; LayerScale exceeds FP16 but retains FP32.
            out = linear * self.scale
            assert out.dtype == torch.float32
            return SimpleNamespace(last_hidden_state=out[:, None, :])
    model.net = SensitiveNet().eval()
    vectors = model.embed_prepared(torch.full((1, 3, 1, 1), 10000.0))
    assert observed == [torch.float16]
    assert vectors.dtype == np.float32 and np.isfinite(vectors).all()
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
