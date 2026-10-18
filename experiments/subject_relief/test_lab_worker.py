import copy
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from experiments.subject_relief import lab_worker as worker
from experiments.subject_relief.geometry import whole_image_relief
from experiments.subject_relief.lab_config import validate_parameters


@pytest.fixture
def job_request(tmp_path):
    source = tmp_path / 'photo.png'
    pixels = np.zeros((40, 60, 3), dtype=np.uint8)
    pixels[..., 0] = np.linspace(0, 250, 60, dtype=np.uint8)
    pixels[..., 1] = 90
    Image.fromarray(pixels).save(source)
    return {'id': 'job-a', 'image_id': 'image-a', 'source_path': str(source), 'source_name': 'photo.png',
            'mask_path': None, 'parameters': validate_parameters({'background_flatten': 0, 'mask_refine': False}),
            'boxes': [], 'cache_root': str(tmp_path / 'cache')}


class FakePredictor:
    calls = 0
    closed = 0

    def __call__(self, pixels, level):
        type(self).calls += 1
        depth = 1 + pixels[..., 0].astype(np.float32) / 255
        return depth, np.ones(depth.shape, dtype=bool)

    def close(self):
        type(self).closed += 1


@pytest.fixture
def fake_models(monkeypatch):
    FakePredictor.calls = FakePredictor.closed = 0
    monkeypatch.setattr(worker, 'DepthPredictor', FakePredictor)
    monkeypatch.setattr(worker, 'model_hashes', lambda directory, names: {name: 'a' * 64 for name in names})
    return FakePredictor


def test_depth_cache_keys_reuse_geometry_fusion_and_mask_changes_but_not_inference(job_request, fake_models):
    cache = worker.ArrayCache(job_request['cache_root'])
    with Image.open(job_request['source_path']) as texture:
        first, records, calls = worker.cached_depth_views(texture, job_request, cache, {'model': 'v1'})
        assert calls == len(records) == 2
        changed = copy.deepcopy(job_request)
        changed['id'] = 'job-b'
        changed['parameters'].update(amplitude=0.7, fusion='mean', min_area_fraction=0.1, grid_side=128)
        second, records, calls = worker.cached_depth_views(texture, changed, cache, {'model': 'v1'})
        assert calls == 0 and all(record['cache_hit'] for record in records)
        assert {record['source_job'] for record in records} == {'job-a'}
        for (a, va), (b, vb) in zip(first, second):
            np.testing.assert_array_equal(a, b)
            np.testing.assert_array_equal(va, vb)
        changed['parameters']['depth_level'] += 1
        _, _, calls = worker.cached_depth_views(texture, changed, cache, {'model': 'v1'})
        assert calls == 2
        changed['parameters']['threads'] += 1
        _, _, calls = worker.cached_depth_views(texture, changed, cache, {'model': 'v1'})
        assert calls == 2
        _, _, calls = worker.cached_depth_views(texture, changed, cache, {'model': 'v2'})
        assert calls == 2
    assert fake_models.closed == 4


def test_disabling_cache_forces_real_depth_calls_and_keeps_complete_cache(job_request, fake_models):
    cache = worker.ArrayCache(job_request['cache_root'])
    with Image.open(job_request['source_path']) as texture:
        _, first, calls = worker.cached_depth_views(texture, job_request, cache, {'model': 'v1'})
        assert calls == len(first) == 2
        _, warm, calls = worker.cached_depth_views(texture, job_request, cache, {'model': 'v1'})
        assert calls == 0 and all(view['cache_hit'] for view in warm)
        forced = copy.deepcopy(job_request)
        forced['id'] = 'forced-job'
        forced['parameters']['use_cache'] = False
        before = fake_models.calls
        _, recalculated, calls = worker.cached_depth_views(texture, forced, cache, {'model': 'v1'})
        assert calls == fake_models.calls - before == 2
        assert not any(view['cache_hit'] for view in recalculated)
        _, refreshed, calls = worker.cached_depth_views(texture, job_request, cache, {'model': 'v1'})
        assert calls == 0 and all(view['source_job'] == 'forced-job' for view in refreshed)


def test_cache_partial_and_invalid_serialization_never_become_hits(tmp_path, monkeypatch):
    cache = worker.ArrayCache(tmp_path)
    key = worker.cache_key({'kind': 'depth'})
    ready = cache._path('depth', key)
    ready.parent.mkdir()
    ready.with_suffix('.npz.orphan.partial').write_bytes(b'cancelled')
    assert cache.get('depth', key, worker.validate_depth) is None
    arrays = {'depth': np.arange(1, 26, dtype=np.float32).reshape(5, 5), 'valid': np.ones((5, 5), dtype=bool)}
    monkeypatch.setattr(worker.os, 'replace', lambda *args: (_ for _ in ()).throw(InterruptedError('cancel before publish')))
    with pytest.raises(InterruptedError):
        cache.put('depth', key, arrays, {'source_job': 'interrupted'}, worker.validate_depth)
    assert not ready.exists()
    assert cache.get('depth', key, worker.validate_depth) is None
    ready.write_bytes(b'corrupt file')
    assert cache.get('depth', key, worker.validate_depth) is None


def test_invalid_depth_is_not_published_to_cache(tmp_path):
    cache = worker.ArrayCache(tmp_path)
    key = worker.cache_key({'invalid': True})
    with pytest.raises(ValueError, match='geometry'):
        cache.put('depth', key, {'depth': np.zeros((5, 5)), 'valid': np.ones((5, 5), dtype=bool)}, {}, worker.validate_depth)
    assert not cache._path('depth', key).exists()


def test_provided_mask_aspect_threshold_alpha_and_nearest_resize(tmp_path):
    mask = np.full((20, 30, 4), 255, dtype=np.uint8)
    mask[:10, :10, :3] = 127
    mask[10:, 20:, 3] = 127
    path = tmp_path / 'mask.png'
    Image.fromarray(mask).save(path)
    result = worker.load_provided_mask(path, (120, 80), (60, 40))
    assert result.shape == (40, 60)
    assert not result[:20, :20].any()
    assert not result[20:, 40:].any()
    assert result[:, 20:40].all()
    with pytest.raises(ValueError, match='aspect'):
        worker.load_provided_mask(path, (100, 100), (100, 100))
    Image.new('L', (30, 20), 0).save(path)
    with pytest.raises(ValueError, match='no selected'):
        worker.load_provided_mask(path, (120, 80), (60, 40))


def test_provided_mask_applies_its_own_exif_before_aspect_check(tmp_path):
    path = tmp_path / 'oriented.png'
    image = Image.new('L', (20, 30), 255)
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)
    assert worker.load_provided_mask(path, (60, 40), (60, 40)).all()


def test_provided_mask_requires_exact_aspect_even_at_low_resolution(tmp_path):
    path = tmp_path / 'small-mask.png'
    Image.new('L', (8, 6), 255).save(path)
    accepted = worker.load_provided_mask(path, (16, 12), (16, 12))
    assert accepted.shape == (12, 16) and accepted.all()
    Image.new('L', (8, 7), 255).save(path)
    with pytest.raises(ValueError, match='aspect ratio'):
        worker.load_provided_mask(path, (16, 12), (16, 12))


def test_semantic_cache_survives_postprocessing_parameter_changes(job_request, tmp_path, fake_models, monkeypatch):
    calls = []

    def semantic(rgb, directory):
        calls.append(1)
        labels = np.zeros(rgb.shape[:2], np.int32)
        labels[5:-5, 5:-5] = 1
        return labels, np.ones(labels.shape, np.float32), {0: 'floor', 1: 'bed'}, {'model': 'fake'}

    monkeypatch.setattr(worker, '_infer_semantics', semantic)
    job_request['parameters']['background_flatten'] = 1
    first = tmp_path / 'mask-first'
    first.mkdir()
    a = worker.mask_stage(first, job_request)
    second = tmp_path / 'mask-second'
    second.mkdir()
    changed = copy.deepcopy(job_request)
    changed['id'] = 'job-b'
    changed['parameters'].update(min_area_fraction=0.03, mask_refine_radius=7, mask_refine_iterations=6)
    b = worker.mask_stage(second, changed)
    assert len(calls) == 1
    assert a['cache']['actual_segmentation_calls'] == 1
    assert b['cache']['hit'] and b['cache']['source_job'] == 'job-a'
    assert b['cache']['actual_segmentation_calls'] == 0
    third = tmp_path / 'mask-forced'
    third.mkdir()
    changed['id'] = 'job-c'
    changed['parameters']['use_cache'] = False
    c = worker.mask_stage(third, changed)
    assert len(calls) == 2
    assert not c['cache']['hit'] and c['cache']['actual_segmentation_calls'] == 1
    assert c['cache']['source_job'] == 'job-c'


def test_provided_mask_not_filtered_and_manual_boxes_add_foreground(job_request, tmp_path, monkeypatch):
    mask_path = tmp_path / 'provided.png'
    provided = np.zeros((40, 60), dtype=np.uint8)
    provided[2, 2] = 255
    Image.fromarray(provided).save(mask_path)
    job_request.update(mask_path=str(mask_path), boxes=[[0.2, 0.2, 0.6, 0.6]])
    job_request['parameters'].update(background_flatten=1, mask_mode='provided', min_area_fraction=1, mask_refine=True)
    monkeypatch.setattr(worker, '_infer_semantics', lambda *args: pytest.fail('provided mask must not infer semantics'))

    def manual(rgb, box, name):
        assert box == (12, 8, 36, 24)
        from experiments.subject_relief.segmentation import Region
        mask = np.zeros(rgb.shape[:2], bool)
        mask[12:16, 20:24] = True
        return Region(name, mask)

    monkeypatch.setattr(worker, 'grabcut_region', manual)
    job = tmp_path / 'provided-job'
    job.mkdir()
    report = worker.mask_stage(job, job_request)
    with Image.open(job / 'foreground-mask.png') as opened:
        actual = np.asarray(opened) > 0
    assert actual[2, 2] and actual[12:16, 20:24].all()
    assert actual.sum() == 17
    assert report['cache']['status'] == 'not-used'


def test_two_stages_generate_real_glb_with_fake_depth_and_geometry_rerun_uses_cache(job_request, tmp_path, fake_models, monkeypatch, capsys):
    monkeypatch.setattr(worker, '_infer_semantics', lambda *args: pytest.fail('flatten zero skips segmentation'))
    original_hash = worker.sha256(job_request['source_path'])
    job = tmp_path / 'job-a'
    job.mkdir()
    worker.mask_stage(job, job_request)
    first = worker.generate_stage(job, job_request)
    assert first['actual_inference_calls'] == first['cache']['depth_view_misses'] == 2
    assert first['cache']['semantic_hit'] is None
    assert first['cache']['depth_view_total'] == 2
    assert (job / 'model.glb').read_bytes()[:4] == b'glTF'
    for name in ('parameters.json', 'report.json', 'depth.npy', 'height.npy', 'source.png', 'overlay.png'):
        assert (job / name).is_file()
    changed = copy.deepcopy(job_request)
    changed['id'] = 'job-b'
    changed['parameters'].update(amplitude=0.64, fusion='mean')
    next_job = tmp_path / 'job-b'
    next_job.mkdir()
    worker.mask_stage(next_job, changed)
    second = worker.generate_stage(next_job, changed)
    assert second['actual_inference_calls'] == 0
    assert second['cache']['depth_hit']
    np.testing.assert_allclose(np.load(next_job / 'height.npy'), np.load(job / 'height.npy') * 2, atol=1e-6)
    assert worker.sha256(job_request['source_path']) == original_hash
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {'mask', 'inference', 'fusion', 'mesh'} <= {event['stage'] for event in events}
    assert all(event['event'] == 'progress' and event['current'] <= event['total'] for event in events)


def test_mask_stage_cli_emits_jsonl_and_normalized_artifacts_without_ai(job_request, tmp_path):
    job = tmp_path / 'cli-job'
    job.mkdir()
    (job / 'request.json').write_text(json.dumps(job_request), encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', '-m', 'experiments.subject_relief.lab_worker',
                             '--job-dir', str(job.resolve()), '--stage', 'mask'],
                            cwd=worker.ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]['stage'] == 'mask' and events[-1]['current'] == 1
    report = json.loads((job / 'mask-stage.json').read_text(encoding='utf-8'))
    assert report['diagnostics']['mode'] == 'skipped'
    assert report['parameters'] == job_request['parameters']


def test_generate_rejects_mutation_between_stages(job_request, tmp_path):
    job = tmp_path / 'mutated'
    job.mkdir()
    worker.mask_stage(job, job_request)
    Image.new('RGB', (60, 40), 'red').save(job_request['source_path'])
    with pytest.raises(ValueError, match='changed'):
        worker.generate_stage(job, job_request)


def test_fresh_python_cache_only_generation_exports_glb_without_loading_model(job_request, tmp_path, monkeypatch):
    model_dir = tmp_path / 'fake-depth-weights'
    model_dir.mkdir()
    (model_dir / 'model.onnx').write_bytes(b'test-cache-identity-only-never-loaded')
    monkeypatch.setattr(worker, 'DEPTH_DIR', model_dir)
    monkeypatch.setattr(worker, 'DepthPredictor', FakePredictor)
    warm_job = tmp_path / 'warm-job'
    warm_job.mkdir()
    worker.mask_stage(warm_job, job_request)
    warm = worker.generate_stage(warm_job, job_request)
    assert warm['actual_inference_calls'] == 2

    cached_request = copy.deepcopy(job_request)
    cached_request['id'] = 'fresh-cache-only'
    cached_request['parameters']['amplitude'] = 0.48
    cached_job = tmp_path / 'fresh-cache-only'
    cached_job.mkdir()
    worker.mask_stage(cached_job, cached_request)
    (cached_job / 'request.json').write_text(json.dumps(cached_request), encoding='utf-8')
    script = r'''
import json, sys
from pathlib import Path
from experiments.subject_relief import lab_worker
lab_worker.DEPTH_DIR = Path(sys.argv[2])
class ForbiddenPredictor:
    def __init__(self):
        raise AssertionError('cache-only generation must not construct an inference session')
lab_worker.DepthPredictor = ForbiddenPredictor
record = lab_worker.generate_stage(Path(sys.argv[1]))
assert record['actual_inference_calls'] == 0
assert record['cache']['depth_hit'] is True
assert record['cache']['depth_view_hits'] == 2
assert (Path(sys.argv[1]) / 'model.glb').read_bytes()[:4] == b'glTF'
print(json.dumps({'cache_only_export': True, 'cache': record['cache']}))
'''
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', '-B', '-c', script, str(cached_job), str(model_dir)],
        cwd=worker.ROOT, capture_output=True, text=True, encoding='utf-8', timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['cache_only_export']
    report = json.loads((cached_job / 'report.json').read_text(encoding='utf-8'))
    assert report['parameters']['amplitude'] == 0.48
    np.testing.assert_allclose(np.load(cached_job / 'height.npy'), np.load(warm_job / 'height.npy') * 1.5, atol=1e-6)


def test_flatten_strength_is_linear_with_global_percentiles_and_preserved_interior():
    depth = np.linspace(1, 5, 120, dtype=np.float32).reshape(10, 12)
    valid = np.ones(depth.shape, bool)
    foreground = np.zeros(depth.shape, bool)
    foreground[2:8, 2:10] = True
    original, _ = whole_image_relief(depth, valid, foreground, background_flatten=0, low_percentile=0, high_percentile=100)
    flat, _ = whole_image_relief(depth, valid, foreground, background_flatten=1, low_percentile=0, high_percentile=100)
    half, stats = whole_image_relief(depth, valid, foreground, background_flatten=0.5, low_percentile=0, high_percentile=100)
    np.testing.assert_allclose(half, (original + flat) / 2, atol=1e-8)
    np.testing.assert_array_equal(half[3:7, 3:9], original[3:7, 3:9])
    assert np.all(flat[~foreground] == 0)
    assert stats['percentiles'] == [0, 100]


@pytest.mark.parametrize('options', [
    {'background_flatten': -1}, {'background_flatten': np.nan},
    {'low_percentile': 99, 'high_percentile': 10}, {'high_percentile': 101},
])
def test_geometry_new_parameters_reject_invalid_values(options):
    depth = np.arange(1, 26, dtype=np.float32).reshape(5, 5)
    with pytest.raises(ValueError):
        whole_image_relief(depth, np.ones((5, 5), bool), np.ones((5, 5), bool), **options)


def test_mean_fusion_uses_global_aligned_inverse_not_depth_average():
    from experiments.subject_relief.compare import fuse_predictions

    reference_inverse = np.linspace(0.3, 2, 100, dtype=np.float32).reshape(10, 10)
    a, b = reference_inverse.copy(), reference_inverse.copy()
    a[5, 5] += 0.1
    b[5, 5] += 0.2
    valid = np.ones((10, 10), bool)
    inputs = [(1 / inv, valid) for inv in (reference_inverse, a, b)]
    mean, supported, transforms = fuse_predictions(inputs, method='mean')
    aligned = [(inv * transform['scale'] + transform['offset']) for inv, transform in zip((reference_inverse, a, b), transforms)]
    np.testing.assert_allclose(mean[supported], (1 / np.mean(aligned, axis=0))[supported], rtol=1e-6)
    with pytest.raises(ValueError, match='fusion'):
        fuse_predictions(inputs, method='unknown')
