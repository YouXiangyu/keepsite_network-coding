import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sampleData, sampleItem, media, pngHash, otherHash, otherPng } from './web-fixtures.mjs';

const root = path.resolve(fileURLToPath(new URL('../dist/client/', import.meta.url)));
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
let accounts = new Map();
let csrfTokens = new Map();
let uploads = new Map();
let createFingerprints = new Map();
let requests = [];
let stalls = [];
let hang;
let rejectNext;

function account(user) {
  if (!accounts.has(user)) {
    const data = sampleData();
    data.tickets[0].fields.title = `${user.toUpperCase()} 的电影票`;
    accounts.set(user, { revision: 1, data });
  }
  return accounts.get(user);
}
const snapshot = user => ({ initialized: true, serverId: 'reliability', userId: user, ...account(user), capabilities: { processing: false, deviceSync: false, cloudStorage: true } });
const session = user => ({ authenticated: !!user, configured: true, providers: { email: true, google: false },
  ...(user ? { user: { id: user, email: `${user}@example.test`, name: user }, csrfToken: csrfTokens.get(user) ?? `csrf-${user}` } : {}) });

async function bodyOf(request) {
  const buffers = [];
  for await (const chunk of request) buffers.push(chunk);
  const body = Buffer.concat(buffers);
  return request.headers['content-type']?.includes('application/json') ? JSON.parse(body.toString() || '{}') : body;
}

function respond(response, pathname, method, body, status = 200, type = 'application/json') {
  const bytes = Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body));
  response.writeHead(status, { 'Content-Type': type, 'Content-Length': bytes.length, 'Cache-Control': 'no-store' });
  if (hang?.path === pathname && hang.method === method && hang.remaining > 0) {
    hang.remaining -= 1;
    const stalled = { pathname, response, bytes, closed: false };
    stalls.push(stalled);
    response.on('close', () => { stalled.closed = true; });
    // Send real successful headers and a partial body, then leave the HTTP stream open.
    response.flushHeaders();
    response.write(bytes.subarray(0, 1));
    return;
  }
  response.end(bytes);
}

const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
    const method = request.method;
    const body = ['POST', 'PATCH', 'PUT', 'DELETE'].includes(method) ? await bodyOf(request) : undefined;
    if (pathname === '/__test/reset' && method === 'POST') {
      for (const stalled of stalls) stalled.response.destroy();
      accounts = new Map(); csrfTokens = new Map(); uploads = new Map(); createFingerprints = new Map(); requests = []; stalls = []; hang = undefined; rejectNext = undefined;
      return respond(response, pathname, method, { ok: true });
    }
    if (pathname === '/__test/reject' && method === 'POST') {
      rejectNext = body;
      return respond(response, pathname, method, { ok: true });
    }
    if (pathname === '/__test/renew' && method === 'POST') {
      csrfTokens.set(body.user ?? 'a', 'csrf-renewed');
      return respond(response, pathname, method, { ok: true });
    }
    if (pathname === '/__test/hang' && method === 'POST') {
      hang = { ...body, remaining: body.remaining ?? 1 };
      return respond(response, pathname, method, { ok: true });
    }
    if (pathname === '/__test/release' && method === 'POST') {
      for (const stalled of stalls) if (!stalled.response.destroyed) stalled.response.end(stalled.bytes.subarray(1));
      return respond(response, pathname, method, { ok: true });
    }
    if (pathname === '/__test/status') return respond(response, pathname, method, {
      requests, stalls: stalls.map(({ pathname, closed }) => ({ pathname, closed })),
    });
    if (pathname.startsWith('/api/')) {
      const cookie = request.headers.cookie?.match(/(?:^|;\s*)test-user=([^;]*)/)?.[1];
      const user = cookie === 'signed-out' ? '' : cookie || 'a';
      requests.push({ path: pathname, method, body: Buffer.isBuffer(body) ? { uploadId: /name="uploadId"\r\n\r\n([^\r]+)/.exec(body.toString())?.[1] } : body });
      if (pathname === '/api/auth/session') return respond(response, pathname, method, session(user));
      if (pathname === '/api/auth/login') {
        const next = body.email.startsWith('b') ? 'b' : 'a';
        response.setHeader('Set-Cookie', `test-user=${next}; Path=/; HttpOnly; SameSite=Lax`);
        return respond(response, pathname, method, session(next));
      }
      if (pathname === '/api/auth/logout') {
        response.setHeader('Set-Cookie', 'test-user=signed-out; Path=/; HttpOnly; SameSite=Lax');
        return respond(response, pathname, method, { ok: true });
      }
      if (!user) return respond(response, pathname, method, { detail: '请登录。' }, 401);
      if (method !== 'GET' && request.headers['x-csrf-token'] !== (csrfTokens.get(user) ?? `csrf-${user}`)) return respond(response, pathname, method, { detail: 'CSRF' }, 403);
      if (pathname === '/api/state') return respond(response, pathname, method, snapshot(user));
      if (pathname.startsWith('/api/operations/')) {
        const operationId = pathname.split('/')[3];
        return respond(response, pathname, method, { operationId, applied: createFingerprints.has(`${user}/${operationId}`) });
      }
      if (pathname.startsWith('/api/uploads/')) {
        const upload = uploads.get(`${user}/${pathname.split('/')[3]}`);
        return respond(response, pathname, method, upload ?? { detail: '上传不存在' }, upload ? 200 : 404);
      }
      if (pathname.endsWith('/download')) {
        const hash = pathname.split('/')[3];
        return respond(response, pathname, method, { ...media(), assetId: hash, sha256: hash, url: '/api/media/' + hash, size: hash === otherHash ? otherPng.length : image.length, transport: 'server', expiresAt: Date.now() + 60000 });
      }
      if (pathname.startsWith('/api/media/')) return respond(response, pathname, method, pathname.endsWith(otherHash) ? otherPng : image, 200, 'image/png');
      if (pathname === '/api/media') {
        const uploadId = /name="uploadId"\r\n\r\n([^\r]+)/.exec(body.toString())?.[1];
        if (!uploadId) return respond(response, pathname, method, { detail: 'uploadId required' }, 422);
        uploads.set(`${user}/${uploadId}`, { uploadId, status: 'stored', asset: media() });
        return respond(response, pathname, method, { ...media(), uploadId });
      }
      if (rejectNext?.path === pathname && rejectNext.method === method) {
        rejectNext = undefined;
        return respond(response, pathname, method, { detail: '暂时无法提交，请核对后重试。' }, 503);
      }
      const workspace = account(user);
      const key = `${user}/${body?.operationId}`;
      const fingerprint = JSON.stringify({ pathname, method, body });
      if (createFingerprints.has(key)) {
        if (createFingerprints.get(key) !== fingerprint) return respond(response, pathname, method, { detail: '操作标识已使用且内容不同。' }, 409);
        return respond(response, pathname, method, snapshot(user));
      }
      if (pathname === '/api/tickets' && method === 'POST') {
        if (workspace.data.tickets.some(item => item.id === body.id)) return respond(response, pathname, method, { detail: '票根已创建，请核对后编辑。' }, 409);
        const ticket = sampleItem(body.id);
        ticket.fields = { title: '', cinema: '', hall: '', seat: '', showtime: '', ...body.fields };
        workspace.data.tickets.push(ticket);
      } else if (pathname.startsWith('/api/tickets/') && method === 'PATCH') {
        const item = workspace.data.tickets.find(item => item.id === pathname.split('/')[3]);
        if (!item || item.deletedAt !== null) return respond(response, pathname, method, { detail: '票根不存在。' }, 404);
        Object.assign(item.fields, body.fields);
        if (body.originalAssetId) item.media.original = { ...media(), assetId: body.originalAssetId, sha256: body.originalAssetId, url: '/api/media/' + body.originalAssetId, size: body.originalAssetId === otherHash ? otherPng.length : image.length };
        item.version += 1;
      } else if (pathname.startsWith('/api/tickets/') && method === 'DELETE') {
        const item = workspace.data.tickets.find(item => item.id === pathname.split('/')[3]);
        if (item) item.deletedAt = Date.now();
      } else return respond(response, pathname, method, { detail: 'Not found' }, 404);
      if (body.originalAssetId) for (const [uploadKey, upload] of uploads) if (uploadKey.startsWith(user + '/') && upload.asset.assetId === body.originalAssetId) upload.status = 'attached';
      createFingerprints.set(key, fingerprint);
      workspace.revision += 1;
      return respond(response, pathname, method, snapshot(user));
    }
    let filename = path.resolve(root, '.' + pathname);
    if (filename !== root && !filename.startsWith(root + path.sep)) return response.writeHead(404).end();
    if (!path.extname(filename)) filename = path.join(root, 'index.html');
    try {
      const bytes = await readFile(filename);
      const type = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' }[path.extname(filename)] || 'application/octet-stream';
      response.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' }).end(bytes);
    } catch { response.writeHead(404).end(); }
  } catch (error) {
    console.error(error);
    if (!response.headersSent) response.writeHead(500);
    response.end();
  }
});
server.listen(4176, '127.0.0.1');
