"""One local lab job, in independently cancellable mask/generate processes.

The server owns validation, job state and process termination. This module never
spawns descendants, downloads models, or writes outside the job/cache folders.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
import zipfile

import cv2
import numpy as np
from PIL import Image, ImageOps

# This shared runtime boundary also exposes the installed export dependencies.
# Cache-only processes need it even though they never create an ORT session.
from backend.anyimage_worker import create_cpu_session, prepare_texture

from .compare import fuse_predictions, resize_prediction, unique_views
from .geometry import export_heightfield, whole_image_relief
from .lab_config import validate_boxes, validate_parameters
from .run import ROOT, sha256
from .segmentation import _infer_semantics, _validate_prediction, automatic_foreground, grabcut_region

SEGMENTER_DIR = ROOT / '.cache/models/segformer-b0-ade'
DEPTH_DIR = ROOT / '.cache/models/moge-2-vits-normal-onnx'
CACHE_VERSION = 'lab-raw-v1'


def emit(stage, message, current=0, total=1, **details):
    print(json.dumps({'event': 'progress', 'stage': stage, 'message': message,
                      'current': current, 'total': total, **details}, ensure_ascii=False), flush=True)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def cache_key(description):
    return hashlib.sha256(canonical({'version': CACHE_VERSION, **description}).encode('utf-8')).hexdigest()


def pixels_hash(pixels):
    array = np.ascontiguousarray(pixels)
    digest = hashlib.sha256(canonical({'shape': list(array.shape), 'dtype': str(array.dtype)}).encode())
    digest.update(array.tobytes())
    return digest.hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(canonical(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ArrayCache:
    """A single validated NPZ is the ready marker; partial files never hit."""

    def __init__(self, root):
        self.root = Path(root)

    def _path(self, kind, key):
        if kind not in ('semantic', 'depth') or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid cache identity')
        return self.root / kind / (key + '.npz')

    def get(self, kind, key, validate):
        path = self._path(kind, key)
        if not path.is_file():
            return None
        try:
            with np.load(path, allow_pickle=False) as archive:
                metadata = json.loads(str(archive['metadata'].item()))
                arrays = {name: archive[name].copy() for name in archive.files if name != 'metadata'}
            if metadata.get('key') != key or metadata.get('version') != CACHE_VERSION:
                return None
            validate(arrays, metadata)
            return arrays, metadata
        except (OSError, ValueError, KeyError, TypeError, EOFError, zipfile.BadZipFile):
            # An interrupted or corrupt entry must never be treated as a hit.
            return None

    def put(self, kind, key, arrays, metadata, validate):
        metadata = {**metadata, 'key': key, 'version': CACHE_VERSION}
        validate(arrays, metadata)
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
        try:
            with temporary.open('xb') as stream:
                np.savez_compressed(stream, **arrays, metadata=np.asarray(canonical(metadata)))
                stream.flush()
                os.fsync(stream.fileno())
            # Validate serialized bytes too, before making this an eligible hit.
            with np.load(temporary, allow_pickle=False) as archive:
                validate({name: archive[name] for name in archive.files if name != 'metadata'},
                         json.loads(str(archive['metadata'].item())))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return metadata


def validate_depth(arrays, metadata):
    depth, valid = arrays['depth'], arrays['valid']
    if (depth.ndim != 2 or min(depth.shape) < 2 or valid.shape != depth.shape or valid.dtype != bool
            or not np.issubdtype(depth.dtype, np.floating) or valid.sum() < 16
            or not np.isfinite(depth[valid]).all() or np.any(depth[valid] <= 0)):
        raise ValueError('invalid cached depth geometry')


def validate_semantic(arrays, metadata):
    labels = {int(key): value for key, value in metadata['labels'].items()}
    _validate_prediction(arrays['semantic'], arrays['confidence'], labels, tuple(metadata['shape']))


def model_hashes(directory, names):
    return {name: sha256(directory / name) for name in names}


def load_provided_mask(path, oriented_size, working_size):
    """EXIF-orient a same-aspect mask; nearest resize, luminance AND alpha >=128."""
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert('RGBA')
    try:
        width, height = oriented_size
        if image.width * height != image.height * width:
            raise ValueError('provided mask must have the EXIF-oriented source aspect ratio')
        mask = (np.asarray(image.convert('L')) >= 128) & (np.asarray(image)[..., 3] >= 128)
        mask = cv2.resize(mask.astype(np.uint8), working_size, interpolation=cv2.INTER_NEAREST).astype(bool)
        if not mask.any():
            raise ValueError('provided mask contains no selected foreground after resizing')
        return mask
    finally:
        image.close()


def configure(parameters):
    threads = str(parameters['threads'])
    os.environ['MEMOVERSE_ORT_THREADS'] = threads
    os.environ['OMP_NUM_THREADS'] = threads
    os.environ['MKL_NUM_THREADS'] = threads
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HOME'] = str(ROOT / '.cache/subject-relief/hf')
    os.environ['TORCH_HOME'] = str(ROOT / '.cache/subject-relief/torch')
    cv2.setNumThreads(parameters['threads'])
    cv2.setRNGSeed(0)


def request_for(job_dir, request):
    value = json.loads((job_dir / 'request.json').read_text(encoding='utf-8')) if request is None else dict(request)
    value['parameters'] = validate_parameters(value['parameters'])
    value['boxes'] = validate_boxes(value.get('boxes'))
    return value


def prepare_source(path):
    with Image.open(path) as opened:
        oriented_size = opened.size[::-1] if opened.getexif().get(274) in (5, 6, 7, 8) else opened.size
    return prepare_texture(path), oriented_size


def mask_stage(job_dir, request=None):
    job_dir = Path(job_dir)
    request = request_for(job_dir, request)
    parameters = request['parameters']
    configure(parameters)
    started = time.monotonic()
    emit('mask', '准备转正后的图片')
    source_hash = sha256(request['source_path'])
    texture, oriented_size = prepare_source(Path(request['source_path']))
    working = texture.copy()
    working.thumbnail((parameters['mask_side'], parameters['mask_side']), Image.Resampling.BICUBIC)
    texture.save(job_dir / 'source.png')
    texture.close()
    rgb = np.array(Image.alpha_composite(Image.new('RGBA', working.size, (127, 127, 127, 255)), working).convert('RGB'))
    opaque = np.asarray(working)[..., 3] >= 128
    manual = np.zeros(rgb.shape[:2], dtype=bool)
    if parameters['background_flatten'] > 0:
        for index, box in enumerate(request.get('boxes', [])):
            scaled = tuple(np.rint(np.asarray(box) * [working.width, working.height, working.width, working.height]).astype(int))
            manual |= grabcut_region(rgb, scaled, f'manual-{index + 1}').mask
    cache_record = {'hit': False, 'status': 'not-used', 'actual_segmentation_calls': 0}
    hashes = {}
    inference_seconds = 0.0
    if parameters['background_flatten'] == 0:
        foreground = np.ones(rgb.shape[:2], dtype=bool)
        diagnostics = {'mode': 'skipped', 'reason': 'background_flatten=0; original whole-image geometry retained'}
        emit('mask', '压平程度为零，跳过分割', 1, 1, cache_hit=False)
    elif parameters['mask_mode'] == 'provided':
        if not request.get('mask_path'):
            raise ValueError('provided mask mode requires an uploaded mask')
        foreground = (load_provided_mask(request['mask_path'], oriented_size, working.size) | manual) & opaque
        diagnostics = {'mode': 'provided', 'mask_sha256': sha256(request['mask_path']),
                       'threshold': 'luminance>=128 and alpha>=128', 'resize': 'nearest, matching oriented source aspect'}
    else:
        hashes = model_hashes(SEGMENTER_DIR, ('model.safetensors', 'config.json', 'preprocessor_config.json'))
        key = cache_key({'kind': 'semantic', 'source_sha256': source_hash, 'model': hashes,
                         'pixels_sha256': pixels_hash(rgb), 'mask_side': parameters['mask_side'],
                         'preprocessing': 'oriented-rgba-neutral-gray-bicubic-v1'})
        cache = ArrayCache(request['cache_root'])
        cached = cache.get('semantic', key, validate_semantic) if parameters['use_cache'] else None
        if cached is None:
            emit('segmentation', '正在执行本地语义分割', 0, 1, cache_hit=False)
            tick = time.monotonic()
            semantic, confidence, labels, metadata = _infer_semantics(rgb, SEGMENTER_DIR)
            inference_seconds = time.monotonic() - tick
            arrays = {'semantic': semantic, 'confidence': confidence}
            provenance = cache.put('semantic', key, arrays, {
                'labels': labels, 'shape': list(rgb.shape[:2]), 'model': metadata,
                'source_job': request['id'], 'model_hashes': hashes,
                'elapsed_seconds': inference_seconds,
            }, validate_semantic)
            cache_record = {'hit': False, 'status': 'computed', 'actual_segmentation_calls': 1}
        else:
            arrays, provenance = cached
            cache_record = {'hit': True, 'status': 'cached', 'actual_segmentation_calls': 0}
        cache_record.update(key=key, source_job=provenance['source_job'], source_elapsed_seconds=provenance['elapsed_seconds'])
        emit('segmentation', '使用已缓存的语义预测' if cached is not None else '语义预测完成', 1, 1, cache_hit=cached is not None)
        prediction = (arrays['semantic'], arrays['confidence'],
                      {int(key): value for key, value in provenance['labels'].items()}, provenance['model'])
        foreground, diagnostics = automatic_foreground(
            rgb, SEGMENTER_DIR, min_component_fraction=parameters['min_area_fraction'],
            preserve_mask=manual if manual.any() else None, refine=parameters['mask_refine'],
            refine_radius=parameters['mask_refine_radius'], refine_iterations=parameters['mask_refine_iterations'],
            prediction=prediction,
        )
        diagnostics.pop('semantic_map', None)
        diagnostics['mode'] = 'auto'
        diagnostics['boxes_normalized'] = request.get('boxes', [])
        foreground &= opaque
        Image.fromarray(arrays['semantic'].astype(np.uint8)).save(job_dir / 'semantic-labels.png')
    if not foreground.any():
        raise ValueError('no foreground remains after masking transparent pixels')
    diagnostics['boxes_normalized'] = request.get('boxes', [])
    Image.fromarray(foreground.astype(np.uint8) * 255).save(job_dir / 'foreground-mask.png')
    overlay = rgb.copy()
    overlay[foreground] = (overlay[foreground] * 0.62 + np.array([40, 175, 130]) * 0.38).astype(np.uint8)
    Image.fromarray(overlay).save(job_dir / 'overlay.png')
    working.close()
    elapsed = time.monotonic() - started
    record = {'parameters': parameters, 'input_sha256': source_hash, 'source_name': request['source_name'],
              'source_preview_sha256': sha256(job_dir / 'source.png'),
              'mask_sha256': sha256(job_dir / 'foreground-mask.png'), 'original_oriented_size': list(oriented_size),
              'mask_shape': list(foreground.shape), 'foreground_pixels': int(foreground.sum()),
              'cache': cache_record, 'model_hashes': hashes, 'diagnostics': diagnostics,
              'elapsed_seconds': elapsed, 'stage_timings': {'segmentation': inference_seconds, 'mask': elapsed - inference_seconds}}
    write_json(job_dir / 'mask-stage.json', record)
    emit('mask', '前景蒙版已准备', 1, 1, cache_hit=cache_record['hit'])
    return record


class DepthPredictor:
    def __init__(self):
        from server.models.onnx_moge2 import infer

        self.session = create_cpu_session(DEPTH_DIR)
        self.infer = infer

    def __call__(self, pixels, level):
        result = self.infer(self.session, np.asarray(pixels, dtype=np.float32) / 255, level)
        depth = np.asarray(result['depth'], dtype=np.float32)
        valid = np.isfinite(depth) & (depth > 0) & (result['mask'] > 0.5)
        return np.where(valid, depth, 0), valid

    def close(self):
        self.session = None
        gc.collect()


def cached_depth_views(texture, request, cache, hashes, *, predictor_factory=None):
    """Return unmirrored raw predictions; load ORT only on the first miss."""
    parameters = request['parameters']
    predictor_factory = predictor_factory or DepthPredictor
    rgb = texture.convert('RGB')
    views = list(unique_views(rgb, {'side': parameters['input_side']},
                              scales=parameters['scales'], mirror=parameters['mirror']))
    rgb.close()
    predictions, records = [], []
    predictor = None
    calls = 0
    source_hash = sha256(request['source_path'])
    try:
        for index, (pixels, scale, flipped) in enumerate(views, 1):
            key = cache_key({'kind': 'depth', 'source_sha256': source_hash, 'model': hashes,
                             'level': parameters['depth_level'], 'pixels_sha256': pixels_hash(pixels),
                             'threads': parameters['threads'], 'mirrored': flipped,
                             'preprocessing': 'production-rgb-bicubic-v1'})
            tick = time.monotonic()
            cached = cache.get('depth', key, validate_depth) if parameters['use_cache'] else None
            if cached is None:
                emit('inference', f'正在真实推理第 {index}/{len(views)} 个视图', index - 1, len(views), cache_hit=False)
                if predictor is None:
                    predictor = predictor_factory()
                depth, valid = predictor(pixels, parameters['depth_level'])
                arrays = {'depth': np.asarray(depth, dtype=np.float32), 'valid': np.asarray(valid, dtype=bool)}
                provenance = cache.put('depth', key, arrays, {
                    'source_job': request['id'], 'model_hashes': hashes, 'threads': parameters['threads'],
                    'elapsed_seconds': time.monotonic() - tick,
                }, validate_depth)
                calls += 1
            else:
                arrays, provenance = cached
            depth, valid = arrays['depth'], arrays['valid']
            if flipped:
                depth, valid = depth[:, ::-1].copy(), valid[:, ::-1].copy()
            predictions.append((depth, valid))
            records.append({'key': key, 'cache_hit': cached is not None, 'source_job': provenance['source_job'],
                            'source_elapsed_seconds': provenance['elapsed_seconds'], 'elapsed_seconds': time.monotonic() - tick,
                            'input_size': [pixels.shape[1], pixels.shape[0]], 'scale': scale, 'mirrored': flipped,
                            'output_shape': list(depth.shape), 'valid_fraction': float(valid.mean())})
            emit('inference', f'视图 {index}/{len(views)} ' + ('读取缓存完成' if cached is not None else '推理完成'),
                 index, len(views), cache_hit=cached is not None)
    finally:
        if predictor is not None:
            predictor.close()
    return predictions, records, calls


def generate_stage(job_dir, request=None):
    job_dir = Path(job_dir)
    request = request_for(job_dir, request)
    parameters = request['parameters']
    configure(parameters)
    started = time.monotonic()
    mask_record = json.loads((job_dir / 'mask-stage.json').read_text(encoding='utf-8'))
    if (mask_record['input_sha256'] != sha256(request['source_path'])
            or mask_record['source_preview_sha256'] != sha256(job_dir / 'source.png')
            or mask_record['mask_sha256'] != sha256(job_dir / 'foreground-mask.png')
            or mask_record['parameters'] != parameters):
        raise ValueError('source, parameters or mask changed after mask preparation')
    hashes = model_hashes(DEPTH_DIR, ('model.onnx',))
    with Image.open(job_dir / 'source.png') as opened:
        texture = opened.convert('RGBA')
    with Image.open(job_dir / 'foreground-mask.png') as opened:
        foreground = np.asarray(opened.convert('L')) >= 128
    tick = time.monotonic()
    predictions, records, calls = cached_depth_views(texture, request, ArrayCache(request['cache_root']), hashes)
    inference_seconds = time.monotonic() - tick
    emit('fusion', f'使用 {parameters["fusion"]} 融合 {len(predictions)} 个有效视图', 0, 1)
    tick = time.monotonic()
    depth, valid, alignment = fuse_predictions(predictions, method=parameters['fusion'])
    del predictions
    np.save(job_dir / 'depth.npy', depth, allow_pickle=False)
    Image.fromarray(valid.astype(np.uint8) * 255).save(job_dir / 'valid.png')
    fusion_seconds = time.monotonic() - tick
    emit('fusion', '融合完成', 1, 1)
    emit('mesh', '生成连续网格与照片纹理', 0, 1)
    tick = time.monotonic()
    ratio = min(1, parameters['grid_side'] / max(depth.shape))
    shape = tuple(max(2, round(size * ratio)) for size in depth.shape)
    grid_depth, grid_valid = resize_prediction(depth, valid, shape)
    grid_foreground = cv2.resize(foreground.astype(np.uint8), shape[::-1], interpolation=cv2.INTER_NEAREST).astype(bool)
    heights, stats = whole_image_relief(
        grid_depth, grid_valid, grid_foreground, amplitude=parameters['amplitude'],
        transition_pixels=parameters['edge_transition_fraction'] * max(shape),
        low_percentile=parameters['low_percentile'], high_percentile=parameters['high_percentile'],
        background_flatten=parameters['background_flatten'],
    )
    np.save(job_dir / 'height.npy', heights, allow_pickle=False)
    geometry = export_heightfield(heights, texture, job_dir / 'model.glb', z_offset=-parameters['amplitude'] / 2)
    texture.close()
    mesh_seconds = time.monotonic() - tick
    requested = len(parameters['scales']) * (2 if parameters['mirror'] else 1)
    elapsed = mask_record['elapsed_seconds'] + time.monotonic() - started
    record = {
        'id': request['id'], 'image_id': request['image_id'], 'source_name': request['source_name'],
        'parameters': parameters, 'boxes': request.get('boxes', []), 'model': 'model.glb',
        'original': 'source.png', 'overlay': 'overlay.png', 'foreground_mask': 'foreground-mask.png',
        'cache': {'semantic_hit': mask_record['cache']['hit'] if mask_record['cache']['status'] != 'not-used' else None,
                  'depth_hit': calls == 0, 'depth_view_hits': sum(view['cache_hit'] for view in records),
                  'depth_view_misses': calls, 'depth_view_total': len(records)},
        'depth_views': records,
        'actual_inference_calls': calls, 'requested_inference_passes': requested,
        'unique_inference_views': len(records), 'deduplicated_passes': requested - len(records),
        'elapsed_seconds': elapsed,
        'stage_timings': {**mask_record['stage_timings'], 'inference': inference_seconds,
                          'fusion': fusion_seconds, 'mesh': mesh_seconds},
        'mask': mask_record, 'height_stats': stats, 'geometry': geometry,
        'alignment': alignment, 'fusion': parameters['fusion'],
        'valid_votes_required': math.ceil(len(records) / 2),
        'model_hashes': {'depth': hashes, 'segmentation': mask_record['model_hashes']},
        'source_sha256': mask_record['input_sha256'], 'num_tokens': int(1200 + parameters['depth_level'] / 9 * 2400),
        'limitations': ['Height is an artistic relief, not metric depth.',
                        'Cached predictions are reused; actual_inference_calls counts only new model executions.',
                        'SegFormer semantics may need manual correction; touching objects share one foreground union.'],
    }
    write_json(job_dir / 'parameters.json', parameters)
    write_json(job_dir / 'report.json', record)
    emit('mesh', '模型已生成', 1, 1)
    return record


def main():
    # Windows redirected pipes otherwise use the active ANSI code page.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-dir', required=True, type=Path)
    parser.add_argument('--stage', required=True, choices=('mask', 'generate'))
    args = parser.parse_args()
    if not args.job_dir.is_absolute() or not args.job_dir.is_dir():
        parser.error('job-dir must be an existing absolute job directory')
    (mask_stage if args.stage == 'mask' else generate_stage)(args.job_dir)


if __name__ == '__main__':
    main()
