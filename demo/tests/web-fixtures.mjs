import { deflateSync } from 'node:zlib';
import { createHash } from 'node:crypto';
export const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
export const otherPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
export const pngHash = createHash('sha256').update(png).digest('hex');
export const otherHash = createHash('sha256').update(otherPng).digest('hex');
export const signedSession = (id = 'a') => ({
  authenticated: true, configured: true, providers: { email: true, google: false },
  user: { id, email: `${id}@example.test`, name: `用户 ${id}` }, csrfToken: `test-csrf-${id}`,
});
export const media = (cloudStatus = 'saved') => ({ assetId: pngHash, sha256: pngHash, url: '/api/media/' + pngHash, mimeType: 'image/png', size: png.length, width: 1, height: 1, cloudStatus });
export const sampleItem = (id = 'item-1') => ({
  id, fields: { title: '星际穿越', cinema: '光影影院', hall: '3 号厅', seat: '5 排 6 座', showtime: '2026-10-22T19:30' },
  fieldSources: { title: 'user', cinema: 'user', hall: 'user', seat: 'user', showtime: 'user' },
  createdAt: 100, updatedAt: 100, version: 1, inputVersion: 1, deletedAt: null, operationId: 'initial',
  media: { original: media(), poster: null }, processing: { status: 'not_started', error: null },
});
export const sampleData = () => ({ tickets: [sampleItem()] });
export const snapshotPage = (id = 'a') => ({
  serverId: 'server', userId: id, snapshotId: 'snapshot-' + id, cursor: 7, total: 1, offset: 0,
  tickets: sampleData().tickets, nextPageToken: null, expiresAt: Date.now() + 600000,
  capabilities: { processing: false, processingUnavailableReason: '处理配置未就绪，请配置服务端识别。', cloudStorage: true, deviceSync: false },
});

export function imageFixture(seed) {
  const crc = bytes => { let value = 0xffffffff; for (const byte of bytes) { value ^= byte; for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0); } return (value ^ 0xffffffff) >>> 0; };
  const chunk = (name, data) => { const kind = Buffer.from(name), output = Buffer.alloc(data.length + 12); output.writeUInt32BE(data.length); kind.copy(output, 4); data.copy(output, 8); output.writeUInt32BE(crc(Buffer.concat([kind, data])), data.length + 8); return output; };
  const header = Buffer.alloc(13); header.writeUInt32BE(4); header.writeUInt32BE(4, 4); header[8] = 8; header[9] = 6;
  const pixels = Buffer.alloc(68); for (let row = 0; row < 4; row++) for (let col = 0; col < 4; col++) { const offset = row * 17 + col * 4 + 1; pixels[offset] = seed % 256; pixels[offset + 1] = Math.floor(seed / 256); pixels[offset + 2] = 100; pixels[offset + 3] = 255; }
  const bytes = Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)), chunk('IEND', Buffer.alloc(0))]);
  const hash = createHash('sha256').update(bytes).digest('hex');
  return { bytes, asset: { assetId: hash, sha256: hash, url: '/api/media/' + hash, mimeType: 'image/png', size: bytes.length, width: 4, height: 4, cloudStatus: 'saved' } };
}

export const posterSource = (title = 'Synthetic_movie_poster.png') => ({
  provider: 'wikimedia_commons', sourcePage: 'https://commons.wikimedia.org/wiki/File:' + title,
  sourceFile: 'File:' + title, licenseName: 'CC BY-SA 4.0', licenseUrl: 'https://creativecommons.org/licenses/by-sa/4.0/',
  author: 'Synthetic test author', fetchedAt: 1792620000000,
});
