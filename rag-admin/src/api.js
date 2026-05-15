// src/api.js
// All admin API calls go to /admin/* on the FastAPI backend.
// The Vite dev server proxies /admin → http://localhost:8000/admin.
//
// PHASE 6 CHANGE: Auth header now reads the JWT from the active Supabase
//   session instead of a static localStorage token. The token is fetched
//   from supabase.auth.getSession() on every request so it's always fresh
//   (Supabase auto-refreshes tokens in the background).
//
// New functions added:
//   adminGetUsage()         — GET /admin/usage
//   adminGetDocuments()     — GET /admin/documents
//   adminDeleteDocument(id) — DELETE /admin/documents/{id}
//   adminGetJoinCode()      — GET /admin/join-code
//   adminRegenJoinCode()    — POST /admin/join-code/regenerate

import { supabase } from './supabase'

const BASE = '/admin'

// ── Auth helpers ──────────────────────────────────────────────────────────────

async function getAuthHeaders(extra = {}) {
  const { data: { session } } = await supabase.auth.getSession()
  const token = session?.access_token ?? ''
  return {
    ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
    ...extra,
  }
}

async function handleResponse(res) {
  if (res.status === 401) {
    // Don't auto sign-out — just throw, let the caller decide
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail ?? 'Unauthorized')
  }
  if (res.status === 402) {
    const err = await res.json().catch(() => ({}))
    const code = err.detail?.code ?? 'payment_required'
    const msg  = err.detail?.message ?? err.detail ?? 'Access restricted'
    const error = new Error(msg)
    error.code = code
    throw error
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message ?? err.detail ?? `Request failed (${res.status})`)
  }
  return res.json()
}

// ── Usage ─────────────────────────────────────────────────────────────────────

/**
 * GET /admin/usage
 * Returns: { vectors: { used, limit, percent }, users: { used, limit, percent },
 *            status, plan, display_name, slug }
 */
export async function adminGetUsage() {
  const res = await fetch(`${BASE}/usage`, { headers: await getAuthHeaders() })
  return handleResponse(res)
}

// ── Documents ─────────────────────────────────────────────────────────────────

/**
 * GET /admin/documents
 * Returns: { documents: [{ id, filename, chunk_count, file_size, status, ingested_at }] }
 */
export async function adminGetDocuments() {
  const res = await fetch(`${BASE}/documents`, { headers: await getAuthHeaders() })
  return handleResponse(res)
}

/**
 * DELETE /admin/documents/{id}
 * Returns: { deleted: true, vectors_freed: N }
 */
export async function adminDeleteDocument(id) {
  const res = await fetch(`${BASE}/documents/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: await getAuthHeaders(),
  })
  return handleResponse(res)
}

// ── Join code ─────────────────────────────────────────────────────────────────

/**
 * GET /admin/join-code
 * Returns: { join_code: "SHIP-4829" }
 */
export async function adminGetJoinCode() {
  const res = await fetch(`${BASE}/join-code`, { headers: await getAuthHeaders() })
  return handleResponse(res)
}

/**
 * POST /admin/join-code/regenerate
 * Returns: { join_code: "NEWX-1234" }
 */
export async function adminRegenJoinCode() {
  const res = await fetch(`${BASE}/join-code/regenerate`, {
    method: 'POST',
    headers: await getAuthHeaders(),
  })
  return handleResponse(res)
}

// ── Stats (legacy — used by StatsPanel) ──────────────────────────────────────

export async function adminStats() {
  const res = await fetch(`${BASE}/stats`, { headers: await getAuthHeaders() })
  return handleResponse(res)
}

// ── Legacy file list (kept for backward compat with StatsPanel) ───────────────

export async function adminListFiles() {
  const res = await fetch(`${BASE}/files`, { headers: await getAuthHeaders() })
  return handleResponse(res)
}

// ── Ingest ────────────────────────────────────────────────────────────────────

export async function adminIngest(files) {
  const fd = new FormData()
  for (const f of files) fd.append('files', f)
  const res = await fetch(`${BASE}/ingest`, {
    method: 'POST',
    headers: await getAuthHeaders(),  // no Content-Type — browser sets multipart boundary
    body: fd,
  })
  return handleResponse(res)
}

// ── Delete file (legacy, by filename) ────────────────────────────────────────

export async function adminDeleteFile(filename) {
  const res = await fetch(`${BASE}/file/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
    headers: await getAuthHeaders(),
  })
  return handleResponse(res)
}

// ── Wipe collection ───────────────────────────────────────────────────────────

export async function adminWipe() {
  const res = await fetch(`${BASE}/collection`, {
    method: 'DELETE',
    headers: await getAuthHeaders(),
  })
  return handleResponse(res)
}

// ── Sync status ───────────────────────────────────────────────────────────────

export async function fetchSyncStatus() {
  const res = await fetch(`/sync/status`, { headers: await getAuthHeaders() })
  if (!res.ok) throw new Error('Failed to fetch sync status')
  return res.json()
}

export async function triggerSync() {
  const res = await fetch(`/sync/trigger`, {
    method: 'POST',
    headers: await getAuthHeaders(),
  })
  if (!res.ok) throw new Error('Sync trigger failed')
  return res.json()
}

export async function adminCompleteOnboarding() {
  const res = await fetch(`${BASE}/onboarding-complete`, {
    method: 'POST',
    headers: await getAuthHeaders(),
  })
  return handleResponse(res)
}