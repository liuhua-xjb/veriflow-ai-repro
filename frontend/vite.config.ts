import { defineConfig } from 'vite';

export default defineConfig({
  envDir: '..',
  server: { host: '127.0.0.1', port: 15173, strictPort: true },
  preview: { host: '127.0.0.1', port: 15173, strictPort: true },
});
