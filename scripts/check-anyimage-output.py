"""Validate a generated GLB artifact, its embedded texture and learned depth."""
import io
import json
from pathlib import Path
import struct
import sys

import numpy as np
from PIL import Image

directory = Path(sys.argv[1])
metadata = json.loads((directory / 'geometry.json').read_text(encoding='utf-8'))
raw = (directory / 'model.glb').read_bytes()
magic, version, size = struct.unpack_from('<III', raw)
assert magic == 0x46546C67 and version == 2 and size == len(raw)
json_size, json_type = struct.unpack_from('<II', raw, 12)
assert json_type == 0x4E4F534A
document = json.loads(raw[20:20 + json_size])
binary_start = 20 + json_size + 8
primitive = document['meshes'][0]['primitives'][0]
assert {'POSITION', 'NORMAL', 'TEXCOORD_0'} <= primitive['attributes'].keys()
positions = document['accessors'][primitive['attributes']['POSITION']]
assert positions['max'][2] - positions['min'][2] > 0.1
image = document['images'][0]
assert 'uri' not in image and image['mimeType'] == 'image/png'
view = document['bufferViews'][image['bufferView']]
start = binary_start + view.get('byteOffset', 0)
with Image.open(io.BytesIO(raw[start:start + view['byteLength']])) as texture:
    texture.load()
    assert texture.width > 16 and texture.height > 16
    if metadata['transparent_texture']:
        assert texture.mode == 'RGBA' and texture.getextrema()[3][0] < 255
        assert document['materials'][0]['alphaMode'] == 'BLEND'
depth = np.load(directory / 'depth.npy', allow_pickle=False)
assert depth.ndim == 2 and np.isfinite(depth).any()
assert metadata['triangles'] == 2 * (depth.shape[0] - 1) * (depth.shape[1] - 1)
print(json.dumps({'valid': True, 'bytes': size, 'vertices': positions['count'], 'triangles': metadata['triangles'], 'elapsed_seconds': metadata['elapsed_seconds']}))
