// src/context/AuthContext.jsx
//
// Manages: Supabase session (JWT), tenant info, plan info, usage.
// Exposes: login(), signup(), logout(), session, tenant, plan, usage, loading.
//
// On mount: restores session from supabase.auth.getSession() (already
//   persisted in localStorage by the Supabase client).
// On session change: fetches /admin/usage and /admin/join-code to hydrate
//   context so every page has full tenant data without extra fetches.
// Auth state is driven by supabase.auth.onAuthStateChange — components
//   never need to poll or re-check manually.

import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { supabase } from '../supabase'
import { adminGetUsage, adminGetJoinCode } from '../api'

// ── Context ───────────────────────────────────────────────────────────────────

const AuthContext = createContext(null)

// ── Provider ──────────────────────────────────────────────────────────────────

export function AuthProvider({ children }) {
  const [session,  setSession]  = useState(null)   // Supabase session object
  const [tenant,   setTenant]   = useState(null)   // { display_name, slug, status, … }
  const [plan,     setPlan]     = useState(null)   // { name, max_vectors, max_users, … }
  const [usage,    setUsage]    = useState(null)   // { vectors, users, status, plan }
  const [joinCode, setJoinCode] = useState(null)   // "SHIP-4829"
  const [loading,  setLoading]  = useState(true)   // true during initial session restore

  // ── Hydrate tenant data once we have a valid session ──────────────────────
  const hydrateTenant = useCallback(async (sess) => {
    if (!sess) {
      setTenant(null); setPlan(null); setUsage(null); setJoinCode(null)
      return
    }
    try {
      const [usageData, joinCodeData] = await Promise.all([
        adminGetUsage(),
        adminGetJoinCode(),
      ])
      setUsage(usageData)
      setJoinCode(joinCodeData?.join_code ?? null)

      // Extract tenant/plan info from usage response (backend returns them merged)
      setTenant({
        display_name: usageData?.display_name ?? '',
        slug:         usageData?.slug         ?? '',
        status:       usageData?.status       ?? 'active',
      })
      setPlan({
        name:        usageData?.plan          ?? '',
        max_vectors: usageData?.vectors?.limit ?? 0,
        max_users:   usageData?.users?.limit   ?? 0,
      })
    } catch (err) {
      // Non-fatal — user might be signed in but tenant not fully set up yet
      console.warn('[AuthContext] Failed to hydrate tenant data:', err.message)
    }
  }, [])

  // ── Initial session restore + auth state listener ─────────────────────────
  useEffect(() => {
    let mounted = true

    supabase.auth.getSession().then(({ data: { session: sess } }) => {
      if (!mounted) return
      setSession(sess)
      hydrateTenant(sess).finally(() => { if (mounted) setLoading(false) })
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      async (_event, sess) => {
        if (!mounted) return
        setSession(sess)
        await hydrateTenant(sess)
      }
    )

    return () => {
      mounted = false
      subscription.unsubscribe()
    }
  }, [hydrateTenant])

  // ── Auth actions ──────────────────────────────────────────────────────────

  const login = async (email, password) => {
    const { data, error } = await supabase.auth.signInWithPassword({ email, password })
    if (error) throw new Error(error.message)
    return data
  }

  const signup = async (email, password, companyName) => {
  const res = await fetch('/auth/admin/signup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, company_name: companyName }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message ?? err.detail ?? 'Signup failed')
  }

  // Backend created the user + tenant. Now sign in to get a real session.
  const { data, error } = await supabase.auth.signInWithPassword({ email, password })
  if (error) throw new Error(error.message)
  return data
}

  const logout = async () => {
    await supabase.auth.signOut()
    setSession(null); setTenant(null); setPlan(null); setUsage(null); setJoinCode(null)
  }

  // ── Refresh just usage (called after ingest/delete) ───────────────────────
  const refreshUsage = useCallback(async () => {
    try {
      const usageData = await adminGetUsage()
      setUsage(usageData)
    } catch { /* non-fatal */ }
  }, [])

  const refreshJoinCode = useCallback(async () => {
    try {
      const data = await adminGetJoinCode()
      setJoinCode(data?.join_code ?? null)
    } catch { /* non-fatal */ }
  }, [])

  // ── Onboarding complete helper ────────────────────────────────────────────
  // Checks session JWT app_metadata to see if onboarding is done.
  // The backend sets config_overrides.onboarding_complete = true after
  // the wizard finishes.
  const isOnboardingComplete = useCallback(() => {
    if (!session) return false
    const meta = session.user?.app_metadata ?? {}
    // If onboarding_complete is explicitly false or missing → not complete
    return meta.onboarding_complete === true
  }, [session])

  const value = {
    session,
    tenant,
    plan,
    usage,
    joinCode,
    loading,
    isAuthenticated: !!session,
    login,
    signup,
    logout,
    refreshUsage,
    refreshJoinCode,
    isOnboardingComplete,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}