#!/usr/bin/env python3
"""Build a validated immutable initialization template without changing old data.

Default: inspect/plan only. --execute copies a read-only SQLite snapshot, imports
existing ready models, and serially runs the existing AnyImage worker for crops.
source.sqlite3 is local audit material, NEVER part of server distribution.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
COMMIT = '2e2ab3eb9b5242deadca097c929c40329dbb5012'
WEIGHT_SHA = '24eacb5dc7a2c54c7bc98f7de085ffbed79ad006ea5b664c2c2cdc02ff3a52f0'
WEIGHT = ROOT / '.cache/models/moge-2-vits-normal-onnx/model.onnx'
WORKER = ROOT / 'backend/anyimage_worker.py'
TEMPLATE_ID = 'initial-template-v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, relative):
    name = PurePosixPath(relative)
    if not name.parts or name.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')) or '\\' in relative or ':' in relative:
        raise ValueError('Unsafe template-relative path')
    root = root.absolute()
    path = root
    for part in name.parts:
        path /= part
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Symlink/junction in template path')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes template root')
    return path


def atomic_json(path, value):
    atomic_bytes(path, canonical(value) + b'\n')


def atomic_bytes(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def portable(value):
    if isinstance(value, dict):
        return {key: portable(item) for key, item in value.items() if not key.endswith('_path')}
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def read_source(path):
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Nonempty source WAL: freeze/checkpoint the workspace separately before building')
    before = file_sha(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA integrity_check').fetchall()[0][0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Source database integrity failed')
        tables = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                  for name in ('collections', 'shares', 'community', 'assets', 'jobs', 'likes', 'meta')}
    if before != file_sha(path) or (wal.exists() and wal.stat().st_size):
        raise ValueError('Source changed during read-only inspection')
    return before, tables


def artwork_catalog(path):
    text = path.read_text(encoding='utf-8')
    return {name: {'file': filename, 'rect': [int(v) for v in rect.split(',')]}
            for name, filename, rect in re.findall(r"(\w+):crop\('([^']+)',\[([^\]]+)\]", text)}


def content_records(data):
    yield from data['collections']
    yield from (share['item'] for share in data['shares'])
    yield from data['community']


def image_asset(raw):
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        width, height, fmt = image.width, image.height, image.format
    formats = {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'), 'WEBP': ('image/webp', '.webp')}
    if fmt not in formats:
        raise ValueError('Unsupported source image')
    digest = sha(raw)
    mime, suffix = formats[fmt]
    return {'id': digest, 'sha256': digest, 'path': f'media/{digest}{suffix}', 'mimeType': mime,
            'size': len(raw), 'width': width, 'height': height}


def plan(source, catalog, references):
    from PIL import Image
    source_hash, tables = read_source(source)
    asset_rows = {row['id']: row for row in tables['assets']}
    jobs = {row['id']: row for row in tables['jobs']}
    data = {'collections': [], 'shares': [json.loads(row['payload']) for row in tables['shares']],
            'community': [json.loads(row['payload']) for row in tables['community']],
            'profile': json.loads(next(row['value'] for row in tables['meta'] if row['key'] == 'profile')),
            'likedCommunityIds': [row['id'] for row in tables['likes']]}
    existing = {}
    for row in tables['collections']:
        item = json.loads(row['payload'])
        item['version'] = row['version']
        if row['asset_id']:
            item['assetId'] = row['asset_id']
        if row['job_id']:
            job = jobs[row['job_id']]
            if job['collection_id'] != row['id'] or job['asset_id'] != row['asset_id'] or job['input_revision'] != row['input_revision']:
                raise ValueError('Current source job is inconsistent')
            if job['status'] == 'ready':
                existing[item['id']] = job
        data['collections'].append(item)
    assets, inputs, models = {}, {}, {}
    crops = {}
    for item in content_records(data):
        source_job = existing.get(item['id'])
        if item.get('assetId'):
            original = asset_rows[item['assetId']]
            path = safe_path(source.parent, 'media/' + original['filename'])
            raw = path.read_bytes()
            if sha(raw) != original['id'] or len(raw) != original['size']:
                raise ValueError('Original asset hash/size mismatch')
        else:
            art = item['art']
            if art not in crops:
                entry = catalog[art]
                path = safe_path(references, entry['file'])
                x, y, width, height = entry['rect']
                with Image.open(path) as image:
                    if min(x, y) < 0 or min(width, height) < 1 or x + width > image.width or y + height > image.height:
                        raise ValueError('Artwork crop leaves its reference image')
                    buffer = io.BytesIO()
                    image.crop((x, y, x + width, y + height)).save(buffer, format='PNG')
                    crops[art] = buffer.getvalue()
            raw = crops[art]
        asset = image_asset(raw)
        assets[asset['id']] = asset
        inputs[asset['id']] = raw
        item['assetId'] = asset['id']
        for key in ('customImage', 'modelUrl', 'generationJobId', 'generationError', 'modelCamera', 'generation'):
            item.pop(key, None)
        if source_job:
            folder = safe_path(source.parent, 'models/' + source_job['id'])
            original_hashes = {name: file_sha(safe_path(folder, name)) for name in ('model.glb', 'depth.npy', 'geometry.json')}
            recipe = {'kind': 'existing', 'assetId': asset['id'], 'sourceJobId': source_job['id'], 'sourceFiles': original_hashes}
            key = sha(canonical(recipe))
            models[key] = {'recipe': recipe, 'folder': folder}
        else:
            # Resolve the full generated recipe only after the pipeline is checked.
            key = 'generate-' + asset['id']
            models[key] = {'recipe': {'kind': 'generated', 'assetId': asset['id']}}
        item['modelId'] = key
    return {'sourceHash': source_hash, 'data': data, 'assets': assets, 'inputs': inputs, 'models': models}


def pipeline():
    if not WEIGHT.is_file() or WEIGHT.stat().st_size != 140852051 or file_sha(WEIGHT) != WEIGHT_SHA:
        raise ValueError('Pinned geometry model is missing/corrupt; no automatic download')
    commit = subprocess.run(['git', '-C', str(ROOT / '.cache/anyimage'), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    if commit != COMMIT:
        raise ValueError('AnyImage checkout does not match the reviewed commit')
    dirty = subprocess.run(['git', '-C', str(ROOT / '.cache/anyimage'), 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        raise ValueError('Pinned AnyImage source has local modifications')
    sys.path.insert(0, str(ROOT / '.cache/anyimage-deps'))
    import onnxruntime
    import trimesh
    import numpy
    from PIL import Image
    if 'CPUExecutionProvider' not in onnxruntime.get_available_providers():
        raise ValueError('CPUExecutionProvider unavailable')
    return {'anyimageCommit': COMMIT, 'modelSha256': WEIGHT_SHA, 'workerSha256': file_sha(WORKER),
            'dependencies': {'onnxruntime': onnxruntime.__version__, 'trimesh': trimesh.__version__,
                             'numpy': numpy.__version__, 'Pillow': Image.__version__}}


def file_record(root, path):
    relative = path.relative_to(root).as_posix()
    checked = safe_path(root, relative)
    return {'path': relative, 'size': checked.stat().st_size, 'sha256': file_sha(checked)}


def validate_geometry(folder):
    import numpy as np
    from PIL import Image
    raw = safe_path(folder, 'model.glb').read_bytes()
    if len(raw) < 28 or struct.unpack_from('<III', raw) != (0x46546C67, 2, len(raw)):
        raise ValueError('Invalid GLB header')
    length, kind = struct.unpack_from('<II', raw, 12)
    if kind != 0x4E4F534A:
        raise ValueError('GLB JSON chunk missing')
    doc = json.loads(raw[20:20 + length])
    offset = 20 + length
    bin_length, bin_kind = struct.unpack_from('<II', raw, offset)
    binary = raw[offset + 8:]
    if bin_kind != 0x004E4942 or len(binary) != bin_length:
        raise ValueError('GLB binary length mismatch')
    primitive = doc['meshes'][0]['primitives'][0]
    if not {'POSITION', 'NORMAL', 'TEXCOORD_0'} <= primitive['attributes'].keys():
        raise ValueError('GLB geometry/UV/normals missing')
    accessor = doc['accessors'][primitive['attributes']['POSITION']]
    view = doc['bufferViews'][accessor['bufferView']]
    if accessor['type'] != 'VEC3' or accessor['componentType'] != 5126:
        raise ValueError('Unexpected position accessor')
    positions = np.ndarray((accessor['count'], 3), dtype='<f4', buffer=binary,
        offset=view.get('byteOffset', 0) + accessor.get('byteOffset', 0), strides=(view.get('byteStride', 12), 4))
    if not positions.size or not np.isfinite(positions).all() or np.ptp(positions[:, 2]) < 1e-6:
        raise ValueError('GLB geometry is empty, nonfinite or flat')
    material = doc['materials'][primitive['material']]
    texture = doc['textures'][material['pbrMetallicRoughness']['baseColorTexture']['index']]
    image = doc['images'][texture['source']]
    if 'uri' in image:
        raise ValueError('GLB texture must be embedded')
    view = doc['bufferViews'][image['bufferView']]
    start = view.get('byteOffset', 0)
    with Image.open(io.BytesIO(binary[start:start + view['byteLength']])) as decoded:
        decoded.load()
        if min(decoded.size) < 1:
            raise ValueError('Invalid texture')
    depth = np.load(safe_path(folder, 'depth.npy'), allow_pickle=False)
    if depth.ndim != 2 or not np.isfinite(depth).any():
        raise ValueError('Invalid depth artifact')
    geometry = json.loads(safe_path(folder, 'geometry.json').read_text(encoding='utf-8'))
    if geometry != portable(geometry) or not isinstance(geometry.get('camera'), dict):
        raise ValueError('Geometry metadata is not portable/complete')
    return geometry


def verify_files(root, records):
    for record in records:
        path = safe_path(root, record['path'])
        if not path.is_file() or path.stat().st_size != record['size'] or file_sha(path) != record['sha256']:
            raise ValueError('Template artifact integrity mismatch: ' + record['path'])


def validate_manifest(root, manifest):
    if manifest['schemaVersion'] != 1 or manifest['templateId'] != TEMPLATE_ID:
        raise ValueError('Wrong template schema')
    assets = {item['id']: item for item in manifest['assets']}
    models = {item['id']: item for item in manifest['models']}
    if len(assets) != len(manifest['assets']) or len(models) != len(manifest['models']):
        raise ValueError('Duplicate template identities')
    asset_refs, model_refs = set(), set()
    for item in content_records(manifest['data']):
        if item['generation'] != 'ready' or item['assetId'] not in assets or item['modelId'] not in models:
            raise ValueError('Incomplete content artifact reference')
        if models[item['modelId']]['assetId'] != item['assetId']:
            raise ValueError('Model belongs to another image')
        asset_refs.add(item['assetId']); model_refs.add(item['modelId'])
    if asset_refs != set(assets) or model_refs != set(models):
        raise ValueError('Unreferenced assets/models in template')
    verify_files(root, manifest['assets'])
    for model in models.values():
        verify_files(root, model['files'])
        if {record['path'] for record in model['files']} != {model['path'], model['depthPath'], model['geometryPath']}:
            raise ValueError('Model file manifest is incomplete')
        validate_geometry(safe_path(root, model['path']).parent)
    content = {key: value for key, value in manifest.items() if key != 'version'}
    if manifest['version'] != sha(canonical(content)):
        raise ValueError('Template version hash mismatch')


def execute(prepared, source, output, pipeline_info, runner=None):
    output = output.absolute()
    if not output.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('Output must stay inside this D-drive project workspace')
    for part in [output, *output.parents]:
        if part == ROOT:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Output parent is a symlink/junction')
    if output.is_symlink() or output.resolve().is_relative_to(source.parent.resolve()) or source.parent.resolve().is_relative_to(output.resolve()):
        raise ValueError('Output overlaps original workspace or is a symlink')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = {'sourceDatabaseSha256': prepared['sourceHash'], 'pipeline': pipeline_info}
    identity_path = output / 'build-identity.json'
    if identity_path.exists() and json.loads(identity_path.read_text(encoding='utf-8')) != identity:
        raise ValueError('Output belongs to a different source/pipeline; choose a new output directory')
    atomic_json(identity_path, identity)
    if file_sha(source) != prepared['sourceHash']:
        raise ValueError('Original database changed before snapshot')
    snapshot = safe_path(output, 'source.sqlite3')
    if not snapshot.exists():
        temporary = safe_path(output, 'source.sqlite3.pending')
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as src, closing(sqlite3.connect(temporary)) as dst:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
        os.replace(temporary, snapshot)
    _, snapshot_tables = read_source(snapshot)
    _, source_tables = read_source(source)
    if snapshot_tables != source_tables:
        raise ValueError('Existing audit snapshot differs from source data')
    if file_sha(source) != prepared['sourceHash']:
        raise ValueError('Original database changed during snapshot')
    for asset_id, asset in prepared['assets'].items():
        path = safe_path(output, asset['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            verify_files(output, [asset])
        else:
            atomic_bytes(path, prepared['inputs'][asset_id])
    output_models = []
    replacements = {}
    jobs = list(prepared['models'].items())
    for index, (old_key, entry) in enumerate(jobs, 1):
        recipe = entry['recipe'].copy()
        if recipe['kind'] == 'generated':
            recipe['pipeline'] = pipeline_info
        model_id = sha(canonical(recipe))
        replacements[old_key] = model_id
        folder = safe_path(output, 'models/' + model_id)
        receipt = safe_path(output, 'models/' + model_id + '/receipt.json')
        if receipt.exists():
            model = json.loads(receipt.read_text(encoding='utf-8'))
            if model['recipeSha256'] != model_id:
                raise ValueError('Cached model recipe mismatch')
            verify_files(output, model['files'])
            validate_geometry(folder)
            print(f'[{index}/{len(jobs)}] verified cached {recipe["kind"]} {model_id[:12]}', flush=True)
        else:
            if folder.exists():
                raise ValueError('Unverified output directory exists; inspect before retry')
            work = safe_path(output, '.work/' + uuid.uuid4().hex)
            work.mkdir(parents=True)
            try:
                if recipe['kind'] == 'existing':
                    for name, expected in recipe['sourceFiles'].items():
                        original = safe_path(entry['folder'], name)
                        if file_sha(original) != expected:
                            raise ValueError('Original model changed after planning')
                        shutil.copyfile(original, work / name)
                else:
                    if file_sha(WORKER) != pipeline_info['workerSha256']:
                        raise ValueError('Worker changed after pipeline planning')
                    source_image = safe_path(output, prepared['assets'][recipe['assetId']]['path'])
                    if runner:
                        runner(source_image, work)
                    else:
                        subprocess.run([sys.executable, '-B', str(WORKER), str(source_image), str(work)],
                            cwd=ROOT, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                geometry = portable(json.loads((work / 'geometry.json').read_text(encoding='utf-8')))
                atomic_json(work / 'geometry.json', geometry)
                validate_geometry(work)
                folder.parent.mkdir(parents=True, exist_ok=True)
                os.replace(work, folder)
            except BaseException:
                if work.exists():
                    shutil.rmtree(work)
                raise
            model = {'id': model_id, 'recipeSha256': model_id, 'assetId': recipe['assetId'],
                     'path': f'models/{model_id}/model.glb', 'depthPath': f'models/{model_id}/depth.npy',
                     'geometryPath': f'models/{model_id}/geometry.json', 'camera': geometry['camera'],
                     'provenance': {'kind': recipe['kind'], **({'sourceJobId': recipe['sourceJobId']} if 'sourceJobId' in recipe else {})},
                     'files': [file_record(output, folder / name) for name in ('model.glb', 'depth.npy', 'geometry.json')]}
            atomic_json(receipt, model)
            print(f'[{index}/{len(jobs)}] validated {recipe["kind"]} {model_id[:12]}', flush=True)
        output_models.append(model)
    data = json.loads(canonical(prepared['data']))
    for item in content_records(data):
        item['modelId'] = replacements[item['modelId']]
        item['generation'] = 'ready'
    manifest = {'schemaVersion': 1, 'templateId': TEMPLATE_ID,
                'source': {'databaseSha256': prepared['sourceHash'], 'snapshotPath': 'source.sqlite3', 'snapshotSha256': file_sha(snapshot)},
                'pipeline': pipeline_info, 'data': data, 'assets': sorted(prepared['assets'].values(), key=lambda item: item['id']),
                'models': sorted(output_models, key=lambda item: item['id'])}
    manifest['version'] = sha(canonical(manifest))
    validate_manifest(output, manifest)
    atomic_json(output / 'manifest.json', manifest)
    print(f'Complete: {len(manifest["assets"])} images, {len(manifest["models"])} models; immutable version {manifest["version"]}', flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'backend/data/memoverse.sqlite3')
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/initial-template-v1')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        validate_manifest(args.output, json.loads((args.output / 'manifest.json').read_text(encoding='utf-8')))
        print('Manifest version, references, images and all model artifacts verified')
        return
    prepared = plan(args.source, artwork_catalog(ROOT / 'demo/src/art.tsx'), ROOT / 'demo/public/assets/reference')
    generated = sum(entry['recipe']['kind'] == 'generated' for entry in prepared['models'].values())
    print(json.dumps({'mode': 'execute' if args.execute else 'plan', 'collections': len(prepared['data']['collections']),
        'shares': len(prepared['data']['shares']), 'community': len(prepared['data']['community']),
        'uniqueImages': len(prepared['assets']), 'existingModels': len(prepared['models']) - generated,
        'newModels': generated, 'output': str(args.output)}, ensure_ascii=False), flush=True)
    if args.execute:
        execute(prepared, args.source, args.output, pipeline())
    else:
        print('No files changed or models generated. Review and add --execute to build.')


if __name__ == '__main__':
    main()
