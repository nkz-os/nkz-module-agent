import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Separate from vite.config.ts: tests do not need the federation preset.
export default defineConfig({
  plugins: [react()],
  test: { environment: 'jsdom', include: ['src/**/*.test.{ts,tsx}'] },
});
