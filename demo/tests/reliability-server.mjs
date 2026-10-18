import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sampleData } from './web-fixtures.mjs';
import { glb } from './glb-fixture.mjs';

const root = path.resolve(fileURLToPath(new URL('../dist/client/', import.meta.url)));
const model = glb();
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
const backup = Buffer.from('504b0506000000000000000000000000000000000000', 'hex');
let accounts = new Map();
let requests = [];
let stalls = [];
let hang;

function account(user) {
  if (!accounts.has(user)) {
    const data = sampleData();
    data.collections[0].title = `${user.toUpperCase()} 的海边`;
    data.collections.push({ ...data.collections[0], id: 'item-2', title: '第二件收藏', modelUrl: '/api/models/item-2/model.glb' });
    accounts.set(user, { revision: 1, data, generationRecovery: [] });
  }
  return accounts.get(user);
}
const snapshot = user => ({ initialized: true, serverId: 'reliability', userId: user, ...account(user), generationEngine: { available: true } });
const session = user => ({ authenticated: !!user, configured: true, providers: { email: true, google: false },
  ...(user ? { user: { id: user, email: `${user}@example.test`, name: user }, csrfToken: `csrf-${user}` } : {}) });

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
      accounts = new Map(); requests = []; stalls = []; hang = undefined;
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
      requests.push({ path: pathname, method, body: Buffer.isBuffer(body) ? undefined : body });
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
      if (method !== 'GET' && request.headers['x-csrf-token'] !== `csrf-${user}`) return respond(response, pathname, method, { detail: 'CSRF' }, 403);
      if (pathname === '/api/state') return respond(response, pathname, method, snapshot(user));
      if (pathname.startsWith('/api/models/')) return respond(response, pathname, method, model, 200, 'model/gltf-binary');
      if (pathname.startsWith('/api/media/')) return respond(response, pathname, method, image, 200, 'image/png');
      if (pathname === '/api/backup') return respond(response, pathname, method, backup, 200, 'application/zip');
      if (pathname === '/api/media') return respond(response, pathname, method, { assetId: 'uploaded', url: '/api/media/uploaded' });
      const workspace = account(user);
      if (pathname === '/api/collections' && method === 'POST') {
        if (!workspace.data.collections.some(item => item.id === body.id)) workspace.data.collections.push({
          ...body, tags: body.tags ?? [], addedAt: 100, source: 'mine', version: 1,
          generation: 'queued', generationJobId: 'job-new', customImage: '/api/media/uploaded',
        });
      } else if (pathname.startsWith('/api/collections/') && method === 'PATCH') {
        const item = workspace.data.collections.find(item => item.id === pathname.split('/')[3]);
        if (item.version !== body.version) return respond(response, pathname, method, { detail: '版本已更新，草稿仍保留。' }, 409);
        Object.assign(item, body, { version: item.version + 1 });
      } else return respond(response, pathname, method, { detail: 'Not found' }, 404);
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
