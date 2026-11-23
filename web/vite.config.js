import { resolve } from 'node:path';
import { defineConfig } from 'vite';

export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/report/control/' : '/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:18082',
      '/web3-bs.js': 'http://127.0.0.1:18082',
    },
  },
  build: {
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, 'index.html'),
        cases: resolve(import.meta.dirname, 'cases.html'),
        architecture: resolve(import.meta.dirname, 'architecture.html'),
      },
    },
  },
}));
