// src/api.js
// All admin API calls go directly to /admin/* on the FastAPI backend.
// The Vite dev server proxies /admin → http://localhost:8000/admin.
//
// Auth: every request includes Authorization: Bearer <token>
// Token is read from localStorage key 'admin_token'.
// If ADMIN_TOKEN is empty on the server, the header is still sent
// but the server ignores it (dev mode — no token required).
//
// CHANGE: Added fetchSyncStatus() and triggerSync().
//   Sync endpoints live at /sync/status and /sync/trigger — NOT under /admin,
//   so they use a separate BASE_SYNC constant and require no auth header.
//   This mirrors exactly what rag-frontend/src/api.js does.

const BASE = '/admin'

function getToken() {
  return localStorage.getItem('admin_token') || ''
}

function authHeaders(extra = {}) {
  const token = getToken()
  return {
    ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
    ...extra,
  }
}

async function handleResponse(res) {
  if (res.status === 401) {
    throw new Error('Invalid or missing admin token. Check your token in Settings.')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Request failed (${res.status})`)
  }
  return res.json()
}

// ── Files ─────────────────────────────────────────────────────
export async function adminListFiles() {
  const res = await fetch(`${BASE}/files`, {
    headers: authHeaders(),
  })
  return handleResponse(res)   // { files: string[] }
}

// ── Stats ──────────────────────────────────────────────────────
export async function adminStats() {
  const res = await fetch(`${BASE}/stats`, {
    headers: authHeaders(),
  })
  return handleResponse(res)
}

// ── Ingest ────────────────────────────────────────────────────
export async function adminIngest(files) {
  const fd = new FormData()
  for (const f of files) fd.append('files', f)

  const res = await fetch(`${BASE}/ingest`, {
    method : 'POST',
    headers: authHeaders(),   // no Content-Type — browser sets multipart boundary
    body   : fd,
  })
  return handleResponse(res)   // IngestResponse
}

// ── Delete file ───────────────────────────────────────────────
export async function adminDeleteFile(filename) {
  const res = await fetch(`${BASE}/file/${encodeURIComponent(filename)}`, {
    method : 'DELETE',
    headers: authHeaders(),
  })
  return handleResponse(res)   // DeleteFileResponse
}

// ── Wipe collection ───────────────────────────────────────────
export async function adminWipe() {
  const res = await fetch(`${BASE}/collection`, {
    method : 'DELETE',
    headers: authHeaders(),
  })
  return handleResponse(res)   // WipeResponse
}

// ── Sync status ───────────────────────────────────────────────
// GET /sync/status — returns { last_synced, is_syncing, pending_count, message }
// No auth required — matches rag-frontend/src/api.js exactly.
export async function fetchSyncStatus() {
  const res = await fetch(`/sync/status`)
  if (!res.ok) throw new Error('Failed to fetch sync status')
  return res.json()
}

// ── Trigger sync ──────────────────────────────────────────────
// POST /sync/trigger — fires background sync, returns immediately.
// No auth required — matches rag-frontend/src/api.js exactly.
export async function triggerSync() {
  const res = await fetch(`/sync/trigger`, { method: 'POST' })
  if (!res.ok) throw new Error('Sync trigger failed')
  return res.json()
}