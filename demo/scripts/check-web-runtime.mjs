#!/usr/bin/env node
import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failures = [];
const visited = new Set();
const queue = [path.join(root, 'src/main.tsx')];
while (queue.length) {
  const filename = queue.pop();
  if (visited.has(filename)) continue;
  visited.add(filename);
  const source = readFileSync(filename, 'utf8');
  const relative = path.relative(root, filename).replaceAll('\\', '/');
  if (relative.startsWith('src/mobile/')) failures.push(`${relative}: simulated device runtime is reachable from the web entry`);
  if (/\b(indexedDB|readMigrationSource|markMigrationCompleted|makeSeedData)\b/.test(source)) failures.push(`${relative}: browser boot must not migrate or create account data`);
  if (relative.endsWith('.ts') || relative.endsWith('.tsx')) {
    if (/\bfetch\s*\(/.test(source) && relative !== 'src/api.ts') failures.push(`${relative}: requests must use the session-scoped transport`);
    const imports = source.matchAll(/(?:\b(?:import|export)\s+(?:[^;]*?\s+from\s*)?|\bimport\s*\(\s*)['"]([^'"]+)['"]/g);
    for (const [, specifier] of imports) {
      if (!specifier.startsWith('.')) continue;
      const base = path.resolve(path.dirname(filename), specifier);
      const resolved = [base, ...['.ts', '.tsx', '.js', '.css', '/index.ts', '/index.tsx'].map(extension => base + extension)].find(candidate => existsSync(candidate) && statSync(candidate).isFile());
      if (!resolved) failures.push(`${relative}: unresolved local import ${specifier}`);
      else queue.push(resolved);
    }
  }
}
const html = readFileSync(path.join(root, 'index.html'), 'utf8');
if (!/name="viewport"\s+content="width=device-width, initial-scale=1\.0"/.test(html) || /user-scalable\s*=\s*no|maximum-scale\s*=/.test(html)) failures.push('index.html: browser viewport must allow native scaling');
const api = readFileSync(path.join(root, 'src/api.ts'), 'utf8');
if (!api.includes("credentials: 'same-origin'") || !api.includes('X-CSRF-Token')) failures.push('api.ts: same-origin session/CSRF contract is missing');
const app = readFileSync(path.join(root, 'src/App.tsx'), 'utf8');
if (!app.includes('<AuthGate>') || !app.includes('key={session.user.id}')) failures.push('App.tsx: authenticated account boundary is missing');
const css = readFileSync(path.join(root, 'src/web.css'), 'utf8');
if (/body\s*\{[^}]*overflow\s*:\s*hidden/.test(css)) failures.push('web.css: document scrolling must not be disabled');
if (!visited.has(path.join(root, 'src/AuthGate.tsx')) || !visited.has(path.join(root, 'src/WebDialog.tsx'))) failures.push('Web auth/dialog modules are not in the actual entry graph');
if (failures.length) {
  console.error('Web runtime contract failed:\n' + failures.map(item => '- ' + item).join('\n'));
  process.exit(1);
}
console.log(`Web runtime contract passed (${visited.size} reachable local modules; native viewport, auth boundary, isolated requests).`);
