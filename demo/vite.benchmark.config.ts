import { defineConfig } from 'vite';
import { existsSync, lstatSync, realpathSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const workspace = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(workspace, '.cache/m6-bench-client');
// Vite may empty only this dedicated build directory, never production dist.
for (let location = output; location.startsWith(workspace); location = dirname(location)) {
  if (existsSync(location) && (lstatSync(location).isSymbolicLink() || realpathSync(location) !== resolve(location))) throw new Error('Benchmark build paths must remain inside the real workspace');
  if (resolve(location) === resolve(workspace)) break;
}
if (dirname(output) !== resolve(workspace, '.cache')) throw new Error('Unexpected benchmark output directory');

export default defineConfig({
  root: fileURLToPath(new URL('./benchmark/', import.meta.url)),
  build: { outDir: output, emptyOutDir: true },
});
