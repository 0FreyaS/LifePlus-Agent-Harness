import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = process.env.LIFEPLUS_API_URL || env.LIFEPLUS_API_URL
    || process.env.CAR_AGENT_API_URL || env.CAR_AGENT_API_URL
    || 'http://127.0.0.1:8000'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: { '/api': apiTarget },
      // Polling also detects atomic file replacements missed by native watchers.
      watch: { usePolling: true, interval: 500 },
    },
  }
})
