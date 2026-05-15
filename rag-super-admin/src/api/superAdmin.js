// api/superAdmin.js
// All calls to /super-admin/* backend endpoints.
// Automatically injects the Supabase JWT from session storage.
//
// ── FIX: BASE changed from absolute URL to empty string ──────────────────────
//
// BEFORE (broken):
//   const BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'
//
//   This built URLs like:
//     http://localhost:8000/super-admin/plans
//
//   The browser sees a cross-origin request (port 5175 → port 8000) and fires
//   a CORS preflight. FastAPI's CORSMiddleware is set to allow_origins=["*"]
//   BUT allow_credentials=False, which means the JWT Authorization header is
//   stripped — the request arrives with no token and FastAPI returns 500/401.
//   The Vite proxy in vite.config.js is COMPLETELY BYPASSED because the fetch
//   uses an absolute URL with a hostname — Vite only intercepts relative paths.
//
// AFTER (fixed):
//   const BASE = ''
//
//   This builds URLs like:
//     /super-admin/plans
//
//   The browser sends this to the SAME origin (http://localhost:5175).
//   Vite's dev server intercepts it (because vite.config.js has the proxy rule:
//     '/super-admin' → 'http://localhost:8000'
//   ) and forwards it server-side to FastAPI — no CORS, no preflight, JWT intact.
//
// PRODUCTION NOTE:
//   In production you serve the built React files from a real server/CDN and
//   configure that server to reverse-proxy /super-admin → your backend.
//   The relative BASE path works there too — the browser sends /super-admin/*
//   to the same origin as the page, and your nginx/caddy/etc. proxies it on.
//   VITE_API_BASE should be left UNSET (or set to '') in your .env.production.
// ─────────────────────────────────────────────────────────────────────────────

import { supabase } from '../supabase'

// Empty string → all fetches use relative paths → Vite proxy intercepts them.
// Do NOT set this to 'http://localhost:8000' — that bypasses the proxy entirely.
const BASE = ''

async function authHeaders() {
  const { data: { session } } = await supabase.auth.getSession()
  const token = session?.access_token
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

async function apiFetch(path, opts = {}) {
  const headers = await authHeaders()
  const res = await fetch(`${BASE}${path}`, { ...opts, headers: { ...headers, ...opts.headers } })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail?.message || json.detail || detail
    } catch {}
    throw new Error(detail)
  }

  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return res.text()
}

// ── Tenants ─────────────────────────────────────────────────────────────────

export function listTenants({ page = 1, pageSize = 25, search = '', planId = '', status = '' } = {}) {
  const params = new URLSearchParams({
    page, page_size: pageSize,
    ...(search  ? { search }  : {}),
    ...(planId  ? { plan_id: planId } : {}),
    ...(status  ? { status }  : {}),
  })
  return apiFetch(`/super-admin/tenants?${params}`)
}

export function getTenant(tenantId) {
  return apiFetch(`/super-admin/tenants/${tenantId}`)
}

export function patchTenant(tenantId, body) {
  return apiFetch(`/super-admin/tenants/${tenantId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function reconcileTenant(tenantId) {
  return apiFetch(`/super-admin/tenants/${tenantId}/reconcile`, { method: 'POST' })
}

export function impersonateTenant(tenantId) {
  return apiFetch(`/super-admin/tenants/${tenantId}/impersonate`, { method: 'POST' })
}

export function deleteTenantDocument(tenantId, docId) {
  return apiFetch(`/super-admin/tenants/${tenantId}/documents/${docId}`, { method: 'DELETE' })
}

// ── Plans ────────────────────────────────────────────────────────────────────

export function listPlans() {
  return apiFetch('/super-admin/plans')
}

export function createPlan(body) {
  return apiFetch('/super-admin/plans', { method: 'POST', body: JSON.stringify(body) })
}

export function patchPlan(planId, body) {
  return apiFetch(`/super-admin/plans/${planId}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function retirePlan(planId) {
  return apiFetch(`/super-admin/plans/${planId}/retire`, { method: 'PATCH' })
}

// ── Members ──────────────────────────────────────────────────────────────────

export function listMembers(tenantId) {
  return apiFetch(`/super-admin/tenants/${tenantId}/members`)
}

export function removeMember(tenantId, userId) {
  return apiFetch(`/super-admin/tenants/${tenantId}/members/${userId}`, { method: 'DELETE' })
}

export function promoteMember(tenantId, userId, role) {
  return apiFetch(`/super-admin/tenants/${tenantId}/members/${userId}/promote`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  })
}

// ── Bulk operations ───────────────────────────────────────────────────────────

export function bulkPlanChange(tenantIds, planId) {
  return apiFetch('/super-admin/bulk/plan-change', {
    method: 'POST',
    body: JSON.stringify({ tenant_ids: tenantIds, plan_id: planId }),
  })
}

export function bulkTrialExtend(tenantIds, days) {
  return apiFetch('/super-admin/bulk/trial-extend', {
    method: 'POST',
    body: JSON.stringify({ tenant_ids: tenantIds, days }),
  })
}

export function bulkSuspend(tenantIds) {
  return apiFetch('/super-admin/bulk/suspend', {
    method: 'POST',
    body: JSON.stringify({ tenant_ids: tenantIds }),
  })
}

export function bulkConfigPush(planId, configPatch) {
  return apiFetch('/super-admin/bulk/config-push', {
    method: 'POST',
    body: JSON.stringify({ plan_id: planId, config_patch: configPatch }),
  })
}

// ── Activity & alerts ─────────────────────────────────────────────────────────

export function getActivity({ page = 1, pageSize = 25, tenantId = '', action = '' } = {}) {
  const params = new URLSearchParams({
    page, page_size: pageSize,
    ...(tenantId ? { tenant_id: tenantId } : {}),
    ...(action   ? { action }              : {}),
  })
  return apiFetch(`/super-admin/activity?${params}`)
}

export function getAlerts({ page = 1, pageSize = 50, unreadOnly = true } = {}) {
  const params = new URLSearchParams({ page, page_size: pageSize, unread_only: unreadOnly })
  return apiFetch(`/super-admin/alerts?${params}`)
}

export function markAlertRead(alertId) {
  return apiFetch(`/super-admin/alerts/${alertId}/read`, { method: 'PATCH' })
}