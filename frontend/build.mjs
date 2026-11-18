import { build } from 'esbuild';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { basename } from 'node:path';

await mkdir('dist', { recursive: true });
const { metafile } = await build({
  entryPoints: { app: 'frontend/app.js' },
  bundle: true,
  outdir: 'dist',
  entryNames: '[name]-[hash]',
  target: ['es2022'],
  loader: { '.woff2': 'file', '.woff': 'file' },
  assetNames: 'fonts/[name]-[hash]',
  metafile: true,
});
const outputs = Object.keys(metafile.outputs);
const script = outputs.find(path => path.endsWith('.js'));
const styles = outputs.find(path => path.endsWith('.css'));
const template = await readFile('frontend/index.html', 'utf8');
const html = template.replace('src="/app.js"', `src="/${basename(script)}"`)
  .replace('href="/app.css"', `href="/${basename(styles)}"`);
const next = `dist/.index.html.${process.pid}`;
await writeFile(next, html);
await rename(next, 'dist/index.html');
