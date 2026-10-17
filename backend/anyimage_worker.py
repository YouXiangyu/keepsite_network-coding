"""AnyImage official MoGe2 inference; project-owned relief mesh/GLB export.

This is not the Blender Bake Mesh operator. The orthographic relief keeps
source-image XY/UV alignment and uses learned inverse depth for local parallax.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.cache/anyimage-deps'), str(ROOT / '.cache/anyimage/src/anyimage')]


def generate(source: Path, output: Path):
    started = time.monotonic()
    import numpy as np
    import trimesh
    from PIL import Image, ImageOps
    from server.models.onnx_moge2 import create_session, infer

    output.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        texture = ImageOps.exif_transpose(opened).convert('RGBA')
    texture.thumbnail((1536, 1536))
    sample = texture.copy()
    sample.thumbnail((256, 256))
    session = create_session(ROOT / '.cache/models/moge-2-vits-normal-onnx', 'cpu')
    result = infer(session, np.asarray(sample.convert('RGB'), dtype=np.float32) / 255.0, 0)
    depth = result['depth']
    valid = np.isfinite(depth) & (depth > 0) & (result['mask'] > 0.5)
    if valid.sum() < 16:
        raise RuntimeError('AnyImage returned insufficient valid geometry')
    np.save(output / 'depth.npy', depth, allow_pickle=False)
    inverse = np.zeros_like(depth)
    inverse[valid] = 1.0 / depth[valid]
    low, high = np.percentile(inverse[valid], [2, 98])
    if high - low < 1e-8:
        raise RuntimeError('AnyImage returned effectively flat depth')
    height, width = depth.shape
    aspect = width / height
    x, y = np.meshgrid(np.linspace(-aspect, aspect, width), np.linspace(1, -1, height))
    z = (np.clip((inverse - low) / (high - low), 0, 1) - 0.5) * 0.32
    vertices = np.stack((x, y, z), axis=-1).reshape(-1, 3)
    ids = np.arange(width * height).reshape(height, width)
    a, b, c, d = ids[:-1, :-1], ids[:-1, 1:], ids[1:, :-1], ids[1:, 1:]
    faces = np.concatenate((np.stack((a, c, b), -1).reshape(-1, 3), np.stack((b, c, d), -1).reshape(-1, 3)))
    # Unknown geometry (commonly sky) stays at the far plane, preserving the
    # complete source image. This fill is ours, not an AnyImage prediction.
    z[~valid] = -0.16
    vertices[:, 2] = z.reshape(-1)
    if not len(faces):
        raise RuntimeError('AnyImage geometry contains no valid triangles')
    uv = np.stack(((x / aspect + 1) / 2, (y + 1) / 2), -1).reshape(-1, 2)
    has_alpha = texture.getextrema()[3][0] < 255
    material = trimesh.visual.material.PBRMaterial(baseColorTexture=texture, metallicFactor=0.0, roughnessFactor=1.0, doubleSided=True, alphaMode='BLEND' if has_alpha else 'OPAQUE')
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, visual=trimesh.visual.texture.TextureVisuals(uv=uv, material=material), process=False)
    model = output / 'model.glb'
    mesh.export(model, include_normals=True)
    metadata = {'pipeline': 'AnyImage official MoGe2 inference + project relief mesh/GLB export', 'commit': '2e2ab3eb9b5242deadca097c929c40329dbb5012', 'model': 'MOGE2_VITS_NORMAL', 'vertices': len(vertices), 'triangles': len(faces), 'depth_range': [float(depth[valid].min()), float(depth[valid].max())], 'relief_range': [float(z[valid].min()), float(z[valid].max())], 'aspect': aspect, 'camera': {'type': 'orthographic', 'position': [0, 0, 4], 'target': [0, 0, 0], 'up': [0, 1, 0], 'half_height': 1.05}, 'model_path': str(model.resolve()), 'depth_path': str((output / 'depth.npy').resolve())}
    metadata.update({'elapsed_seconds': round(time.monotonic() - started, 3), 'invalid_depth_fill': 'project-owned far plane z=-0.16', 'valid_fraction': float(valid.mean()), 'transparent_texture': has_alpha, 'coordinates': {'front': '+Z', 'up': '+Y', 'uv': 'bottom-left origin; GLTFLoader manages texture orientation', 'texture_flip_y': False}})
    (output / 'geometry.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(json.dumps(metadata))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    generate(args.input, args.output)
