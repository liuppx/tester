import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig } from 'vite';

export default defineConfig({
  base: '/',
  plugins: [{
    name: 'wallet-sdk',
    configureServer(server) {
      server.middlewares.use('/web3-bs.js', (_request, response) => {
        response.setHeader('Content-Type', 'application/javascript');
        response.end(readFileSync(resolve(import.meta.dirname, 'node_modules/@yeying-community/web3-bs/dist/web3-bs.umd.js')));
      });
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'web3-bs.js', source: readFileSync(resolve(import.meta.dirname, 'node_modules/@yeying-community/web3-bs/dist/web3-bs.umd.js')) });
    },
  }],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:18082',
    },
  },
  build: {
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, 'index.html'),
        cases: resolve(import.meta.dirname, 'cases.html'),
        architecture: resolve(import.meta.dirname, 'architecture.html'),
        report: resolve(import.meta.dirname, 'report.html'),
      },
    },
  },
});
