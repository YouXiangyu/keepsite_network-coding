"""Synthetic template tests: no model inference, network, or original-data writes."""
import importlib.util
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('initial_template', ROOT / 'scripts/prepare-initial-template.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fake_model(directory):
    directory.mkdir(parents=True, exist_ok=True)
    png = io.BytesIO()
    Image.new('RGBA', (8, 8), (90, 140, 190, 150)).save(png, format='PNG')
    positions = np.asarray([[-1, -1, -.1], [1, -1, .1], [0, 1, 0]], dtype='<f4').tobytes()
    binary = positions + png.getvalue()
    document = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': len(binary)}],
        'bufferViews': [{'buffer': 0, 'byteOffset': 0, 'byteLength': len(positions)},
                        {'buffer': 0, 'byteOffset': len(positions), 'byteLength': len(png.getvalue())}],
        'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3'}],
        'meshes': [{'primitives': [{'attributes': {'POSITION': 0, 'NORMAL': 0, 'TEXCOORD_0': 0}, 'material': 0}]}],
        'materials': [{'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}}}],
        'textures': [{'source': 0}], 'images': [{'bufferView': 1, 'mimeType': 'image/png'}]}
    encoded = json.dumps(document).encode()
    encoded += b' ' * (-len(encoded) % 4)
    binary += b'\0' * (-len(binary) % 4)
    raw = struct.pack('<III', 0x46546C67, 2, 28 + len(encoded) + len(binary))
    raw += struct.pack('<II', len(encoded), 0x4E4F534A) + encoded
    raw += struct.pack('<II', len(binary), 0x004E4942) + binary
    (directory / 'model.glb').write_bytes(raw)
    np.save(directory / 'depth.npy', np.asarray([[1., 2.], [2., 3.]], dtype=np.float32), allow_pickle=False)
    (directory / 'geometry.json').write_text(json.dumps({'camera': {'type': 'orthographic'}, 'model_path': '/private/machine/model.glb'}))


class InitialTemplateTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/initial-template-tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / 'source'
        self.source_dir.mkdir()
        self.source = self.source_dir / 'old.sqlite3'
        self.output = self.root / 'output'
        self.references = self.root / 'references'
        self.references.mkdir()
        image = Image.new('RGB', (16, 16), 'red')
        image.paste('blue', (8, 0, 16, 16))
        image.save(self.references / 'ref.png')
        self.catalog = {'left': {'file': 'ref.png', 'rect': [0, 0, 8, 16]},
                        'right': {'file': 'ref.png', 'rect': [8, 0, 8, 16]}}
        original = io.BytesIO()
        Image.new('RGB', (7, 9), 'green').save(original, format='PNG')
        original = original.getvalue()
        self.asset_id = module.sha(original)
        (self.source_dir / 'media').mkdir()
        (self.source_dir / 'media' / (self.asset_id + '.png')).write_bytes(original)
        fake_model(self.source_dir / 'models/old-job')
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript('''
                CREATE TABLE collections(id TEXT,payload TEXT,version INTEGER,asset_id TEXT,job_id TEXT,input_revision INTEGER);
                CREATE TABLE assets(id TEXT,filename TEXT,size INTEGER);
                CREATE TABLE jobs(id TEXT,collection_id TEXT,asset_id TEXT,input_revision INTEGER,status TEXT);
                CREATE TABLE shares(id TEXT,payload TEXT);
                CREATE TABLE community(id TEXT,payload TEXT);
                CREATE TABLE likes(id TEXT);
                CREATE TABLE meta(key TEXT,value TEXT);
            ''')
            old = {'id': 'old', 'title': 'Old original', 'art': 'left', 'note': 'preserve edited note'}
            new = {'id': 'new', 'title': 'Static reference', 'art': 'left'}
            db.execute('INSERT INTO collections VALUES (?,?,?,?,?,?)', ('old', json.dumps(old), 3, self.asset_id, 'old-job', 2))
            db.execute('INSERT INTO collections VALUES (?,?,?,?,?,?)', ('new', json.dumps(new), 2, None, None, 1))
            db.execute('INSERT INTO assets VALUES (?,?,?)', (self.asset_id, self.asset_id + '.png', len(original)))
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', ('old-job', 'old', self.asset_id, 2, 'ready'))
            db.execute('INSERT INTO shares VALUES (?,?)', ('share', json.dumps({'id': 'share', 'item': {**new, 'id': 'shared'}, 'unread': False})))
            db.execute('INSERT INTO community VALUES (?,?)', ('community', json.dumps({'id': 'community', 'art': 'right', 'title': 'Other crop'})))
            db.execute('INSERT INTO likes VALUES (?)', ('community',))
            db.execute('INSERT INTO meta VALUES (?,?)', ('profile', json.dumps({'name': 'Fixture', 'bio': 'preserved'})))
            db.commit()
        self.pipeline = {'modelSha256': 'weights-fixture', 'workerSha256': module.file_sha(module.WORKER), 'anyimageCommit': 'fixture'}
        self.generated = 0

    def plan(self):
        return module.plan(self.source, self.catalog, self.references)

    def runner(self, image, directory):
        self.assertTrue(image.is_file())
        self.generated += 1
        fake_model(directory)

    def build(self):
        return module.execute(self.plan(), self.source, self.output, self.pipeline, runner=self.runner)

    def test_plan_deduplicates_crops_without_writing_output(self):
        before = module.file_sha(self.source)
        prepared = self.plan()
        self.assertEqual(len(prepared['assets']), 3)
        self.assertEqual(len(prepared['models']), 3)
        self.assertFalse(self.output.exists())
        self.assertEqual(module.file_sha(self.source), before)

    def test_complete_build_preserves_source_and_reuses_verified_models(self):
        before = {str(p): module.file_sha(p) for p in self.source_dir.rglob('*') if p.is_file()}
        first = self.build()
        self.assertEqual(self.generated, 2)
        self.assertEqual(first['data']['collections'][0]['note'], 'preserve edited note')
        self.assertEqual(first['data']['collections'][0]['version'], 3)
        self.assertEqual(first['data']['collections'][1]['modelId'], first['data']['shares'][0]['item']['modelId'])
        self.assertEqual(first['data']['community'][0]['generation'], 'ready')
        module.validate_manifest(self.output, first)
        second = self.build()
        self.assertEqual(self.generated, 2)
        self.assertEqual(first['version'], second['version'])
        self.assertEqual(before, {str(p): module.file_sha(p) for p in self.source_dir.rglob('*') if p.is_file()})
        for model in first['models']:
            geometry = json.loads((self.output / model['geometryPath']).read_text())
            self.assertNotIn('model_path', geometry)

    def test_failure_does_not_publish_ready_manifest(self):
        def fail(image, directory):
            raise RuntimeError('Synthetic inference failure')
        with self.assertRaisesRegex(RuntimeError, 'Synthetic'):
            module.execute(self.plan(), self.source, self.output, self.pipeline, runner=fail)
        self.assertFalse((self.output / 'manifest.json').exists())
        self.build()
        self.assertEqual(self.generated, 2)

    def test_corrupt_cached_model_is_not_reused(self):
        manifest = self.build()
        (self.output / manifest['models'][0]['path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            self.build()

    def test_version_or_reference_tampering_fails(self):
        manifest = self.build()
        manifest['data']['profile']['name'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'version hash'):
            module.validate_manifest(self.output, manifest)
        manifest['data']['collections'][0]['assetId'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'reference'):
            module.validate_manifest(self.output, manifest)

    def test_nonempty_wal_blocks_immutable_read(self):
        Path(str(self.source) + '-wal').write_bytes(b'pending')
        with self.assertRaisesRegex(ValueError, 'Nonempty source WAL'):
            self.plan()

    def test_output_cannot_overlap_original_data(self):
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            module.execute(self.plan(), self.source, self.source_dir / 'template', self.pipeline, runner=self.runner)

    def test_output_parent_traversal_is_rejected_before_any_write(self):
        escaped = ROOT / '.cache' / '..' / '..' / ('template-escape-' + self.root.name)
        self.assertFalse(escaped.resolve().is_relative_to(ROOT.resolve()))
        prepared = self.plan()
        with patch.object(Path, 'mkdir', side_effect=AssertionError('Must reject before creating a directory')):
            with self.assertRaisesRegex(ValueError, 'inside this D-drive project'):
                module.execute(prepared, self.source, escaped, self.pipeline, runner=self.runner)

    def test_changed_pipeline_cannot_overwrite_same_template_build(self):
        self.build()
        changed = {**self.pipeline, 'modelSha256': 'other-weights'}
        with self.assertRaisesRegex(ValueError, 'different source/pipeline'):
            module.execute(self.plan(), self.source, self.output, changed, runner=self.runner)

    def test_invalid_crop_and_escape_are_rejected(self):
        self.catalog['left']['rect'] = [0, 0, 999, 999]
        with self.assertRaisesRegex(ValueError, 'crop leaves'):
            self.plan()
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            module.safe_path(self.output, '../escape')


if __name__ == '__main__':
    unittest.main()
