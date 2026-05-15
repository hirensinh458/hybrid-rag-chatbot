// rag-super-admin/vite.config.js
//
// ── Proxy rules ───────────────────────────────────────────────────────────────
//
// Every path listed under `proxy` is intercepted by Vite's dev server and
// forwarded server-side to http://localhost:8000.
//
// WHY THIS IS REQUIRED:
//   The browser enforces CORS — it blocks cross-origin requests (different port
//   = different origin). By serving the React app on port 5175 and routing API
//   calls through Vite's proxy, every fetch hits the SAME origin (5175) and
//   Vite transparently forwards it to FastAPI (8000) server-side.
//   No CORS preflight fires. The Authorization header is preserved intact.
//
// PATHS PROXIED:
//   /super-admin  — all super admin API endpoints (tenant/plan/alert management)
//   /auth         — login endpoint (POST /auth/admin/login, POST /auth/refresh)
//
// changeOrigin: true
//   Rewrites the Host header to match the target. Required when FastAPI checks
//   the Host header or when running behind a virtual host. Always set this.
//
// PRODUCTION:
//   This proxy only runs in `vite dev`. In production (`vite build`), configure
//   your nginx / caddy / cloud load balancer to forward /super-admin and /auth
//   to your backend service. The relative BASE='' in superAdmin.js works in
//   both environments.

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    proxy: {
      // All super-admin API calls
      '/super-admin': {
        target      : 'http://localhost:8000',
        changeOrigin: true,
      },
      // Auth endpoints — login, refresh, logout
      '/auth': {
        target      : 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})