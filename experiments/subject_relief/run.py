"""Local-only subject relief experiment. Run with ``python -m ...run --help``."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

import numpy as np
from PIL import Image

from .geometry import export_scene, soften_subject_edges, subject_heights
from .segmentation import Region, automatic_regions, grabcut_region, load_mask

ROOT = Path(__file__).resolve().parents[2]


def sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def resized(array, size, *, nearest=False):
    import cv2
    return cv2.resize(array, size, interpolation=cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR)


class MoGeDepth:
    def __init__(self, model_dir):
        from backend.anyimage_worker import create_cpu_session
        from server.models.onnx_moge2 import infer
        self.session = create_cpu_session(model_dir)
        self.infer = infer
        self.calls = 0

    def __call__(self, rgb):
        sample = Image.fromarray(rgb)
        sample.thumbnail((256, 256), Image.Resampling.LANCZOS)
        result = self.infer(self.session, np.asarray(sample, dtype=np.float32) / 255.0, 0)
        self.calls += 1
        depth = result['depth']
        valid = np.isfinite(depth) & (depth > 0) & (result['mask'] > 0.5)
        return depth, valid


def infer_subject(rgb, region, predict, *, lift, relief, padding=0.08):
    """Crop one subject, neutralize outside its mask, and infer it independently."""
    mask = region.mask
    yy, xx = np.nonzero(mask)
    if len(xx) < 16:
        raise ValueError(f'{region.name}: subject mask is too small')
    h, w = mask.shape
    margin = max(4, round(max(xx.max() - xx.min() + 1, yy.max() - yy.min() + 1) * padding))
    x0, y0 = max(0, int(xx.min()) - margin), max(0, int(yy.min()) - margin)
    x1, y1 = min(w, int(xx.max()) + margin + 1), min(h, int(yy.max()) + margin + 1)
    crop_mask = mask[y0:y1, x0:x1]
    crop = rgb[y0:y1, x0:x1].copy()
    crop[~crop_mask] = 127
    started = time.monotonic()
    depth, valid = predict(crop)
    sample_mask = resized(crop_mask.astype(np.uint8), (depth.shape[1], depth.shape[0]), nearest=True).astype(bool)
    local, stats = subject_heights(depth, valid, sample_mask, lift=lift, relief=relief)
    # Fill the *height* outside the inference mask before interpolation. This
    # prevents zero background values from dragging the cropped edge down.
    from scipy.ndimage import distance_transform_edt
    nearest = distance_transform_edt(~sample_mask, return_distances=False, return_indices=True)
    local = local[tuple(nearest)]
    local = resized(local, (x1 - x0, y1 - y0))
    bevel_pixels = max(2, round(min(rgb.shape[:2]) / 128))
    local = soften_subject_edges(local, crop_mask, lift=lift, bevel_pixels=bevel_pixels)
    full = np.zeros((h, w), dtype=np.float32)
    full[y0:y1, x0:x1] = np.where(crop_mask, np.clip(local, lift, lift + relief), 0)
    stats.update(
        name=region.name, source=region.source, score=region.score,
        area_pixels=int(mask.sum()), bbox=[x0, y0, x1, y1],
        depth_shape=list(depth.shape), elapsed_seconds=round(time.monotonic() - started, 3),
        edge_bevel_working_pixels=bevel_pixels,
    )
    return full, crop, depth, stats


def compose(rgb, regions, predict, output, *, lift=0.04, relief=0.18):
    owners = np.zeros(rgb.shape[:2], dtype=np.uint16)
    heights = np.zeros(rgb.shape[:2], dtype=np.float32)
    records = []
    names = {0: 'background'}
    for index, region in enumerate(regions, 1):
        if region.mask.shape != owners.shape or region.mask.dtype != bool:
            raise ValueError(f'{region.name}: invalid mask dimensions or dtype')
        if np.any(owners[region.mask]):
            raise ValueError('subject masks overlap; resolve ownership before inference')
        full, crop, depth, stats = infer_subject(rgb, region, predict, lift=lift, relief=relief)
        owners[region.mask] = index
        heights[region.mask] = full[region.mask]
        stem = f'subject-{index:02d}'
        Image.fromarray(region.mask.astype(np.uint8) * 255).save(output / f'{stem}-mask.png')
        Image.fromarray(crop).save(output / f'{stem}-input.png')
        np.save(output / f'{stem}-depth.npy', depth, allow_pickle=False)
        stats.update(mask=f'{stem}-mask.png', inference_input=f'{stem}-input.png', depth=f'{stem}-depth.npy')
        records.append(stats)
        names[index] = f'{stem}-{region.name}'
    return heights, owners, records, names


def _parse_region(value):
    name, separator, path = value.partition('=')
    if not separator or not name.strip() or not path:
        raise argparse.ArgumentTypeError('expected NAME=PATH')
    return name.strip(), Path(path)


def _parse_box(value):
    name, separator, coordinates = value.partition('=')
    try:
        box = tuple(int(v) for v in coordinates.split(','))
    except ValueError as exc:
        raise argparse.ArgumentTypeError('expected NAME=x0,y0,x1,y1') from exc
    if not separator or not name.strip() or len(box) != 4:
        raise argparse.ArgumentTypeError('expected NAME=x0,y0,x1,y1')
    return name.strip(), box


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path, help='new directory; existing results are never overwritten')
    parser.add_argument('--segmenter-dir', type=Path, default=ROOT / '.cache/models/segformer-b0-ade')
    parser.add_argument('--depth-model-dir', type=Path, default=ROOT / '.cache/models/moge-2-vits-normal-onnx')
    parser.add_argument('--labels', help='comma-separated semantic names; default keeps recognized object classes')
    parser.add_argument('--manual-only', action='store_true')
    parser.add_argument('--no-refine', action='store_true', help='explicitly skip automatic GraphCut edge refinement')
    parser.add_argument('--mask', action='append', type=_parse_region, default=[], help='NAME=mask.png, aligned to original oriented image')
    parser.add_argument('--box', action='append', type=_parse_box, default=[], help='NAME=x0,y0,x1,y1 in original oriented image pixels')
    parser.add_argument('--baseline-model', type=Path, help='existing whole-image model for comparison')
    parser.add_argument('--max-side', type=int, default=1024)
    parser.add_argument('--grid-side', type=int, default=384)
    parser.add_argument('--max-subjects', type=int, default=12)
    parser.add_argument('--lift', type=float, default=0.04)
    parser.add_argument('--relief', type=float, default=0.18)
    args = parser.parse_args()
    if not 256 <= args.max_side <= 1536 or not 64 <= args.grid_side <= 768:
        parser.error('max-side must be 256..1536 and grid-side must be 64..768')
    if not np.isfinite([args.lift, args.relief]).all() or args.lift <= 0 or args.relief < 0:
        parser.error('lift must be positive and relief must be nonnegative')
    if not 1 <= args.max_subjects <= 32:
        parser.error('max-subjects must be 1..32')
    if args.output.exists():
        parser.error('output directory already exists')
    from backend.anyimage_worker import prepare_texture
    started = time.monotonic()
    # Read the orientation metadata for user-supplied pixel coordinates, then
    # use the existing bounded decoder for actual image processing.
    with Image.open(args.input) as opened:
        original_size = opened.size[::-1] if opened.getexif().get(274) in (5, 6, 7, 8) else opened.size
    texture = prepare_texture(args.input)
    working = texture.copy()
    working.thumbnail((args.max_side, args.max_side), Image.Resampling.LANCZOS)
    opaque = np.asarray(working)[..., 3] >= 128
    rgb = np.asarray(Image.alpha_composite(Image.new('RGBA', working.size, (127, 127, 127, 255)), working).convert('RGB'))
    manual = []
    for name, path in args.mask:
        mask = load_mask(path, original_size)
        mask = resized(mask.astype(np.uint8), working.size, nearest=True).astype(bool)
        manual.append(Region(name, mask, source='manual-mask'))
    for name, box in args.box:
        scale = np.array([working.width / original_size[0], working.height / original_size[1]] * 2)
        scaled = tuple(np.round(np.array(box) * scale).astype(int))
        manual.append(grabcut_region(rgb, scaled, name))
    occupied = np.zeros(opaque.shape, dtype=bool)
    for region in manual:
        region.mask &= opaque
        if np.any(region.mask & occupied):
            parser.error('manual masks overlap; assign each pixel to only one subject')
        occupied |= region.mask
    diagnostics = {'limitations': []}
    automatic = []
    if not args.manual_only:
        automatic, diagnostics = automatic_regions(
            rgb, args.segmenter_dir, labels=args.labels.split(',') if args.labels else None,
            max_regions=args.max_subjects, refine=not args.no_refine,
        )
        for region in automatic:
            region.mask &= opaque & ~occupied
        automatic = [region for region in automatic if region.mask.sum() >= 16]
    regions = manual + automatic
    if not regions:
        parser.error('no subjects selected; inspect segmentation or supply a mask/box')
    if len(regions) > args.max_subjects:
        parser.error('too many subjects after manual correction; increase max-subjects explicitly')

    # Only this new result directory is written. No DB, user collection, or S3
    # state is touched. A failed run retains its intermediates for diagnosis.
    args.output.mkdir(parents=True, exist_ok=False)
    working.save(args.output / 'original.png')
    semantic = diagnostics.pop('semantic_map', None)
    if semantic is not None:
        Image.fromarray(semantic.astype(np.uint8)).save(args.output / 'semantic-labels.png')
    (args.output / 'segmentation.json').write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding='utf-8')
    predictor = MoGeDepth(args.depth_model_dir)
    heights, owners, records, names = compose(rgb, regions, predictor, args.output, lift=args.lift, relief=args.relief)
    inference_calls = predictor.calls
    del predictor
    np.save(args.output / 'height.npy', heights, allow_pickle=False)
    Image.fromarray(owners).save(args.output / 'owners.png')
    overlay = rgb.copy()
    palette = np.array([[35, 178, 153], [245, 161, 62], [102, 136, 220], [207, 100, 153], [139, 179, 59]])
    for index in names:
        if index:
            selected = owners == index
            overlay[selected] = (overlay[selected] * 0.5 + palette[(index - 1) % len(palette)] * 0.5).astype(np.uint8)
    Image.fromarray(overlay).save(args.output / 'overlay.png')
    Image.fromarray(np.uint8(np.clip(heights / (args.lift + args.relief), 0, 1) * 255)).save(args.output / 'height.png')
    grid_scale = min(1, args.grid_side / max(working.size))
    grid_size = (max(2, round(working.width * grid_scale)), max(2, round(working.height * grid_scale)))
    grid_owners = resized(owners, grid_size, nearest=True)
    grid_heights = resized(heights, grid_size, nearest=True)
    missing = set(names) - set(np.unique(grid_owners)) - {0}
    if missing:
        raise ValueError('a subject disappeared at mesh resolution; increase --grid-side')
    patches = export_scene(grid_heights, grid_owners, texture, args.output / 'model.glb', names)
    baseline_dir = args.output / 'baseline'
    baseline_dir.mkdir()
    if args.baseline_model:
        shutil.copy2(args.baseline_model, baseline_dir / 'model.glb')
        baseline = {'source': 'existing-whole-image-model', 'sha256': sha256(args.baseline_model), 'model_sha256': None}
    else:
        from backend.anyimage_worker import generate
        generate(args.input, baseline_dir)
        baseline = {
            'source': 'current-production-worker',
            'model_sha256': sha256(ROOT / '.cache/models/moge-2-vits-normal-onnx/model.onnx'),
        }
    background = owners == 0
    report = {
        'title': '主体浮雕 · 本地实验', 'original': 'original.png', 'overlay': 'overlay.png',
        'model': 'model.glb', 'baseline_model': 'baseline/model.glb',
        'input_sha256': sha256(args.input), 'input_oriented_size': list(original_size),
        'working_size': list(working.size), 'grid_size': list(grid_size),
        'parameters': {
            'lift': args.lift, 'relief': args.relief, 'max_subjects': args.max_subjects,
            'labels': args.labels, 'manual_only': args.manual_only, 'refine': not args.no_refine,
            'manual_boxes': [{'name': name, 'box_original_oriented_pixels': list(box)} for name, box in args.box],
            'manual_masks': [{'name': name, 'sha256': sha256(path)} for name, path in args.mask],
        },
        'model_hashes': {'moge2': sha256(args.depth_model_dir / 'model.onnx')},
        'subjects': records, 'patches': patches, 'baseline': baseline,
        'metrics': {
            'background_max_abs_height': float(np.abs(heights[background]).max()) if background.any() else None,
            'minimum_subject_height': float(heights[~background].min()),
            'depth_inference_calls': inference_calls,
            'subject_count': len(regions), 'elapsed_seconds': round(time.monotonic() - started, 3),
            'top_cell_coverage': sum(patch['cells'] for patch in patches) / grid_owners.size,
        },
        'limitations': diagnostics.get('limitations', []) + [
            'This is an artistic 2.5D relief, not a complete 3D reconstruction or a print-ready solid.',
            'Independent crop depth scales are not comparable; all subjects use the same configured lift/range.',
            'Occluded surfaces are not reconstructed. GraphCut/masks may need manual correction.',
            'SegFormer B0 weights are used for local research/evaluation only; see the upstream model license.',
        ],
    }
    if not args.manual_only:
        report['model_hashes']['segmenter'] = sha256(args.segmenter_dir / 'model.safetensors')
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()), **report['metrics']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
