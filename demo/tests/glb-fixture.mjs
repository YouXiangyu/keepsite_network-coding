export function glb() {
  const vertices = new Float32Array([-1, -.7, 0, 1, -.7, 0, 1, .7, .1, -1, .7, 0]);
  const indices = new Uint16Array([0, 1, 2, 0, 2, 3]);
  const binary = Buffer.concat([Buffer.from(vertices.buffer), Buffer.from(indices.buffer)]);
  const document = { asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0 }, indices: 1, material: 0 }] }],
    materials: [{ doubleSided: true, pbrMetallicRoughness: { baseColorFactor: [.75, .8, .68, 1], metallicFactor: 0, roughnessFactor: 1 } }],
    buffers: [{ byteLength: binary.length }], bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: vertices.byteLength, target: 34962 },
      { buffer: 0, byteOffset: vertices.byteLength, byteLength: indices.byteLength, target: 34963 }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 4, type: 'VEC3', min: [-1, -.7, 0], max: [1, .7, .1] },
      { bufferView: 1, componentType: 5123, count: 6, type: 'SCALAR', min: [0], max: [3] }] };
  const json = Buffer.from(JSON.stringify(document));
  const padded = Buffer.concat([json, Buffer.alloc((4 - json.length % 4) % 4, 32)]);
  const output = Buffer.alloc(12 + 8 + padded.length + 8 + binary.length);
  output.writeUInt32LE(0x46546c67, 0); output.writeUInt32LE(2, 4); output.writeUInt32LE(output.length, 8);
  output.writeUInt32LE(padded.length, 12); output.writeUInt32LE(0x4e4f534a, 16); padded.copy(output, 20);
  output.writeUInt32LE(binary.length, 20 + padded.length); output.writeUInt32LE(0x004e4942, 24 + padded.length); binary.copy(output, 28 + padded.length);
  return output;
}
