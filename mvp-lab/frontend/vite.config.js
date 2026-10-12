import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../mvp_app/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: process.env.MVP_API_URL || 'http://127.0.0.1:18090', changeOrigin: false },
      '/health': { target: process.env.MVP_API_URL || 'http://127.0.0.1:18090', changeOrigin: false },
    },
  },
});
