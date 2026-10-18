"""Four local whole-image depth presets with one frozen foreground mask."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import warnings

import cv2
import numpy as np
from PIL import Image

from .geometry import export_heightfield, whole_image_relief
from .run import ROOT, _parse_box, sha256
from .segmentation import automatic_foreground, grabcut_region

PRESETS = (
    {'id': 'average', 'label': '平均', 'level': 0, 'side': 256, 'passes': 1, 'grid': 256},
    {'id': 'fine', 'label': '精细', 'level': 5, 'side': 512, 'passes': 2, 'grid': 512},
    {'id': 'finer', 'label': '更精细', 'level': 7, 'side': 768, 'passes': 4, 'grid': 768},
    {'id': 'maximal', 'label': '最高', 'level': 9, 'side': 1536, 'passes': 8, 'grid': 1024},
)


def augmentations(passes):
    """Deterministic, distinct views; never count identical repeats as quality."""
    if passes == 1:
        return [(1.0, False)]
    scales = {2: (1.0,), 4: (1.0, 0.75), 8: (1.0, 0.875, 0.75, 0.625)}
    if passes not in scales:
        raise ValueError('supported pass counts are 1, 2, 4, 8')
    return [(scale, flipped) for scale in scales[passes] for flipped in (False, True)]


def unique_views(image, preset, *, scales=None, mirror=True):
    """Do not spend extra passes on duplicate pixels after bounded resizing."""
    seen = set()
    views = augmentations(preset['passes']) if scales is None else [
        (scale, flipped) for scale in scales for flipped in ((False, True) if mirror else (False,))
    ]
    for scale, flipped in views:
        sample = image.copy()
        side = max(32, round(preset['side'] * scale))
        sample.thumbnail((side, side), Image.Resampling.BICUBIC)
        pixels = np.array(sample, copy=True)
        if flipped:
            pixels = np.ascontiguousarray(pixels[:, ::-1])
        fingerprint = (pixels.shape, hashlib.sha256(pixels.tobytes()).digest())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        yield pixels, scale, flipped


def resize_prediction(depth, valid, shape):
    """Resize inverse depth with valid support, avoiding inf/zero contamination."""
    depth = np.asarray(depth, dtype=np.float32)
    valid = np.asarray(valid, dtype=bool) & np.isfinite(depth) & (depth > 0)
    if depth.shape == tuple(shape):
        return depth.copy(), valid.copy()
    inverse = np.zeros_like(depth)
    inverse[valid] = 1 / depth[valid]
    size = (shape[1], shape[0])
    weights = cv2.resize(valid.astype(np.float32), size, interpolation=cv2.INTER_LINEAR)
    numerator = cv2.resize(inverse, size, interpolation=cv2.INTER_LINEAR)
    resized_inverse = np.divide(numerator, weights, out=np.zeros_like(numerator), where=weights > 0)
    selected = (weights >= 0.5) & np.isfinite(resized_inverse) & (resized_inverse > 0)
    resized_depth = np.zeros_like(resized_inverse)
    resized_depth[selected] = 1 / resized_inverse[selected]
    return resized_depth, selected


def fuse_predictions(predictions, *, method='median'):
    """Align full-frame inverse-depth ranges, then fuse by median or mean.

    All images must already be unflipped. Alignment is global, never per object.
    A pixel needs at least half of the requested views to provide valid depth.
    """
    if method not in ('median', 'mean'):
        raise ValueError('fusion method must be median or mean')
    if not predictions:
        raise ValueError('at least one prediction is required')
    reference, reference_valid = resize_prediction(*predictions[0], predictions[0][0].shape)
    if reference_valid.sum() < 16:
        raise ValueError('reference prediction has insufficient valid geometry')
    if len(predictions) == 1:
        return reference, reference_valid, [{'scale': 1.0, 'offset': 0.0}]
    reference_inverse = np.zeros_like(reference)
    reference_inverse[reference_valid] = 1 / reference[reference_valid]
    aligned, transforms = [], []
    for index, (depth, valid) in enumerate(predictions):
        depth, valid = resize_prediction(depth, valid, reference.shape)
        inverse = np.zeros_like(depth)
        inverse[valid] = 1 / depth[valid]
        common = reference_valid & valid
        if common.sum() < 16:
            raise ValueError('augmented prediction has insufficient shared valid geometry')
        scale, offset = 1.0, 0.0
        if index:
            r0, r1 = np.percentile(reference_inverse[common], [2, 98])
            p0, p1 = np.percentile(inverse[common], [2, 98])
            if min(r1 - r0, p1 - p0) < 1e-8:
                raise ValueError('cannot align an effectively flat augmented prediction')
            scale = float((r1 - r0) / (p1 - p0))
            offset = float(r0 - scale * p0)
        inverse = inverse * scale + offset
        valid &= np.isfinite(inverse) & (inverse > 0)
        aligned.append(np.where(valid, inverse, np.nan))
        transforms.append({'scale': scale, 'offset': offset, 'valid_pixels': int(valid.sum())})
    stack = np.stack(aligned)
    valid = np.isfinite(stack).sum(axis=0) >= math.ceil(len(predictions) / 2)
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message='All-NaN slice encountered', category=RuntimeWarning)
        if method == 'median':
            inverse = np.nanmedian(stack, axis=0)
        else:
            counts = np.isfinite(stack).sum(axis=0)
            inverse = np.divide(np.nansum(stack, axis=0), counts,
                                out=np.full(reference.shape, np.nan), where=counts > 0)
    valid &= np.isfinite(inverse) & (inverse > 0)
    depth = np.zeros_like(inverse)
    depth[valid] = 1 / inverse[valid]
    return depth, valid, transforms


def prepare_mask_worker(source, output, model_dir, minimum, boxes):
    """A short-lived process releases torch before the heavier depth stages."""
    from backend.anyimage_worker import prepare_texture

    cv2.setRNGSeed(0)
    texture = prepare_texture(source)
    working = texture.copy()
    working.thumbnail((1024, 1024), Image.Resampling.BICUBIC)
    with Image.open(source) as opened:
        original_size = opened.size[::-1] if opened.getexif().get(274) in (5, 6, 7, 8) else opened.size
    rgb = np.array(Image.alpha_composite(Image.new('RGBA', working.size, (127, 127, 127, 255)), working).convert('RGB'))
    manual = np.zeros(rgb.shape[:2], dtype=bool)
    for name, box in boxes:
        factors = np.array([working.width / original_size[0], working.height / original_size[1]] * 2)
        scaled = tuple(np.round(np.array(box) * factors).astype(int))
        manual |= grabcut_region(rgb, scaled, name).mask
    foreground, diagnostics = automatic_foreground(
        rgb, model_dir, min_component_fraction=minimum,
        preserve_mask=manual if manual.any() else None,
    )
    foreground &= np.asarray(working)[..., 3] >= 128
    if not foreground.any():
        raise ValueError('no opaque foreground remains')
    semantic = diagnostics.pop('semantic_map')
    working.save(output / 'original.png')
    Image.fromarray(semantic.astype(np.uint8)).save(output / 'semantic-labels.png')
    Image.fromarray(foreground.astype(np.uint8) * 255).save(output / 'foreground-mask.png')
    overlay = rgb.copy()
    overlay[foreground] = (overlay[foreground] * 0.62 + np.array([40, 175, 130]) * 0.38).astype(np.uint8)
    Image.fromarray(overlay).save(output / 'overlay.png')
    diagnostics['manual_boxes_original_pixels'] = [{'name': name, 'box': list(box)} for name, box in boxes]
    diagnostics['random_seed'] = 0
    (output / 'segmentation.json').write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding='utf-8')


def run_preset(texture, foreground, output, model_dir, preset, *, amplitude=0.32, transition_fraction=2 / 256):
    from backend.anyimage_worker import create_cpu_session
    from server.models.onnx_moge2 import infer

    started = time.monotonic()
    output.mkdir()
    session = create_cpu_session(model_dir)
    rgb = texture.convert('RGB')
    predictions, records = [], []
    try:
        for index, (pixels, scale, flipped) in enumerate(unique_views(rgb, preset), 1):
            array = np.asarray(pixels, dtype=np.float32) / 255
            input_size = [pixels.shape[1], pixels.shape[0]]
            print(json.dumps({'preset': preset['id'], 'pass': index, 'requested': preset['passes'], 'level': preset['level'], 'input_size': input_size, 'mirrored': flipped}), flush=True)
            tick = time.monotonic()
            result = infer(session, array, preset['level'])
            depth = np.asarray(result['depth'], dtype=np.float32)
            valid = np.isfinite(depth) & (depth > 0) & (result['mask'] > 0.5)
            if flipped:
                depth, valid = depth[:, ::-1].copy(), valid[:, ::-1].copy()
            predictions.append((depth, valid))
            records.append({'pass': index, 'scale': scale, 'mirrored': flipped, 'input_size': input_size, 'output_shape': list(depth.shape), 'elapsed_seconds': round(time.monotonic() - tick, 3), 'valid_fraction': float(valid.mean())})
            np.savez_compressed(output / f'pass-{index:02d}.npz', depth=depth, valid=valid)
            del result
    finally:
        del session
        gc.collect()
    depth, valid, alignment = fuse_predictions(predictions)
    del predictions
    np.save(output / 'depth.npy', depth, allow_pickle=False)
    Image.fromarray(valid.astype(np.uint8) * 255).save(output / 'valid.png')
    grid_ratio = min(1, preset['grid'] / max(depth.shape))
    grid_shape = tuple(max(2, round(size * grid_ratio)) for size in depth.shape)
    grid_depth, grid_valid = resize_prediction(depth, valid, grid_shape)
    grid_foreground = cv2.resize(foreground.astype(np.uint8), grid_shape[::-1], interpolation=cv2.INTER_NEAREST).astype(bool)
    transition = transition_fraction * max(grid_shape)
    heights, stats = whole_image_relief(grid_depth, grid_valid, grid_foreground, amplitude=amplitude, transition_pixels=transition)
    np.save(output / 'height.npy', heights, allow_pickle=False)
    Image.fromarray(grid_foreground.astype(np.uint8) * 255).save(output / 'foreground-grid.png')
    geometry = export_heightfield(heights, texture, output / 'model.glb', z_offset=-amplitude / 2)
    record = {
        'id': preset['id'], 'label': preset['label'], 'model': f"{preset['id']}/model.glb",
        'depth_level': preset['level'], 'num_tokens': int(1200 + preset['level'] / 9 * 2400),
        'input_side': preset['side'], 'requested_inference_passes': preset['passes'],
        'inference_passes': len(records), 'deduplicated_passes': preset['passes'] - len(records),
        'grid_shape': list(grid_shape),
        'elapsed_seconds': round(time.monotonic() - started, 3), 'passes': records,
        'alignment': alignment, 'fusion': 'global affine inverse-depth alignment followed by median; at least half valid votes',
        'background_deviation': stats['background_max_abs_height'],
        'foreground_error': stats['stable_foreground_max_error'],
        'amplitude': amplitude, 'height_stats': stats, 'geometry': geometry,
        'model_bytes': (output / 'model.glb').stat().st_size,
    }
    (output / 'report.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'completed': preset['id'], 'elapsed_seconds': record['elapsed_seconds'], 'model_bytes': record['model_bytes']}), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--baseline-model', type=Path, required=True)
    parser.add_argument('--segmenter-dir', type=Path, default=ROOT / '.cache/models/segformer-b0-ade')
    parser.add_argument('--depth-model-dir', type=Path, default=ROOT / '.cache/models/moge-2-vits-normal-onnx')
    parser.add_argument('--min-area-fraction', type=float, default=0.02)
    parser.add_argument('--box', action='append', type=_parse_box, default=[])
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be a new directory')
    if not 0 < args.min_area_fraction <= 1 or not 1 <= args.threads <= 32:
        parser.error('min-area-fraction must be in (0,1] and threads in 1..32')
    for path in (args.input, args.baseline_model, args.depth_model_dir / 'model.onnx'):
        if not path.is_file():
            parser.error(f'missing required file: {path}')
    os.environ['MEMOVERSE_ORT_THREADS'] = str(args.threads)
    os.environ.setdefault('OMP_NUM_THREADS', str(args.threads))
    os.environ.setdefault('MKL_NUM_THREADS', str(args.threads))
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('HF_HOME', str(ROOT / '.cache/subject-relief/hf'))
    os.environ.setdefault('TORCH_HOME', str(ROOT / '.cache/subject-relief/torch'))
    source_hashes = {name: sha256(Path(__file__).with_name(name)) for name in ('compare.py', 'geometry.py', 'segmentation.py')}
    args.output.mkdir(parents=True)
    child = multiprocessing.get_context('spawn').Process(
        target=prepare_mask_worker,
        args=(args.input, args.output, args.segmenter_dir, args.min_area_fraction, args.box),
    )
    child.start()
    try:
        child.join()
        if child.exitcode != 0:
            raise RuntimeError('foreground preparation failed; see child diagnostics')
    finally:
        if child.is_alive():
            child.terminate()
            child.join()
        child.close()
    from backend.anyimage_worker import prepare_texture
    texture = prepare_texture(args.input)
    with Image.open(args.output / 'foreground-mask.png') as image:
        foreground = np.asarray(image.convert('L')) >= 128
    (args.output / 'original').mkdir()
    shutil.copy2(args.baseline_model, args.output / 'original/model.glb')
    variants = [{'id': 'original', 'label': '原流程（背景保留起伏）', 'model': 'original/model.glb', 'depth_level': 0, 'input_side': 256, 'inference_passes': 1, 'elapsed_seconds': None, 'background_deviation': None, 'foreground_error': None}]
    for preset in PRESETS:
        variants.append(run_preset(texture, foreground, args.output / preset['id'], args.depth_model_dir, preset))
        gc.collect()
    segmentation = json.loads((args.output / 'segmentation.json').read_text(encoding='utf-8'))
    report = {
        'title': '保留整图形状，只铺平背景', 'original': 'original.png', 'overlay': 'overlay.png',
        'baseline_model': 'original/model.glb', 'model': 'fine/model.glb', 'variants': variants,
        'subjects': [{'name': '连续前景联合区域', 'source': 'semantic-union-with-manual-correction' if args.box else 'semantic-union'}],
        'metrics': {'background_max_abs_height': 0, 'minimum_subject_height': 0, 'depth_inference_calls': sum(variant['inference_passes'] for variant in variants if variant['id'] != 'original')},
        'input_sha256': sha256(args.input), 'mask_sha256': sha256(args.output / 'foreground-mask.png'),
        'baseline_sha256': sha256(args.baseline_model), 'model_sha256': sha256(args.depth_model_dir / 'model.onnx'),
        'source_sha256': source_hashes, 'segmenter_sha256': sha256(args.segmenter_dir / 'model.safetensors'),
        'threads': args.threads, 'minimum_component_fraction': args.min_area_fraction,
        'segmentation': segmentation,
        'limitations': [
            '所有档位共用同一前景蒙版；颜色只表示保留范围，不表示凹凸。内部语义部件不再分别估深或归一化。',
            '小岛阈值按图片面积计算，不代表物理物体大小；人工指定区域保留。',
            '四档同时改变输入尺寸、token预算和融合次数，因此比较的是整档效果，不能单独归因于某一参数。',
            '多尺度/镜像融合是实验性稳健化，也可能平滑细节；高档位不保证更好，重复次数不等于新的模型训练。',
            '最高是本机实验预设上限：精度9、输入1536、8次推理、网格最长边1024，不是无限分辨率。',
            '所有档位幅度固定0.32。模型背景位于z=-0.16，以便与原流程坐标一致；报告中的height是相对该平面的高度。',
            '分割权重仅用于本地研究评估；结果仍是2.5D浮雕，不补全被遮挡背面。',
        ],
    }
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()), 'completed_presets': 4, 'inference_passes': report['metrics']['depth_inference_calls']}), flush=True)


if __name__ == '__main__':
    main()
