import * as esbuild from 'esbuild';
import * as path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const electronRedirectPlugin = {
  name: 'electron-redirect',
  setup(build) {
    build.onResolve({ filter: /^electron$/ }, () => {
      return { path: path.join(__dirname, 'src', 'electron-stub.ts') };
    });
    build.onResolve({ filter: /^electron-store$/ }, () => {
      return { path: path.join(__dirname, 'src', 'electron-stub.ts'), namespace: 'stub' };
    });
  },
};

const electronStoreStubPlugin = {
  name: 'electron-store-stub',
  setup(build) {
    build.onLoad({ filter: /.*/, namespace: 'stub' }, () => {
      return { contents: 'module.exports = {};', loader: 'js' };
    });
  },
};

await esbuild.build({
  entryPoints: ['extract_key_cli.ts'],
  bundle: true,
  platform: 'node',
  target: 'node18',
  outfile: 'extract_key_cli.js',
  format: 'cjs',
  external: ['koffi', 'fzstd', 'jieba-wasm', 'silk-wasm', 'sherpa-onnx-node', 'ffmpeg-static', 'worker_threads'],
  plugins: [electronRedirectPlugin, electronStoreStubPlugin],
});
console.log('[esbuild] extract_key_cli.js built');
