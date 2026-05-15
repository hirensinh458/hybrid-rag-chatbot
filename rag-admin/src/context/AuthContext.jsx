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
//
// ── WHY WE DECODE THE JWT ──────────────────────────────────────────────────
// Supabase Auth Hooks (custom access token hooks / Edge Functions) inject
// custom claims (tenant_id, role, onboarding_complete) into the SIGNED JWT
// token itself — they do NOT write them back to the auth.users database row.
// Therefore session.user.app_metadata (which comes from the DB) will NOT
// have these fields. We must decode session.access_token to read them.
// ──────────────────────────────────────────────────────────────────────────

import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { supabase } from '../supabase'
import { adminGetUsage, adminGetJoinCode } from '../api'

// ── JWT decode helper ─────────────────────────────────────────────────────────
function decodeJwtPayload(token) {
    try {
        const part = token.split('.')[1]
        const b64 = part.replace(/-/g, '+').replace(/_/g, '/')
        return JSON.parse(atob(b64))
    } catch {
        return {}
    }
}

function getJwtAppMeta(session) {
    if (!session?.access_token) return {}
    const payload = decodeJwtPayload(session.access_token)
    return payload.app_metadata ?? {}
}

// ── Context ───────────────────────────────────────────────────────────────────
const AuthContext = createContext(null)

// ── Provider ──────────────────────────────────────────────────────────────────
export function AuthProvider({ children }) {
    const [session, setSession] = useState(null)
    const [tenant, setTenant] = useState(null)
    const [plan, setPlan] = useState(null)
    const [usage, setUsage] = useState(null)
    const [joinCode, setJoinCode] = useState(null)
    const [loading, setLoading] = useState(true)
    // Separate state for onboarding flag – updated synchronously from fresh JWT
    const [onboardingComplete, setOnboardingComplete] = useState(false)

    // ── Helper to extract onboarding flag from a session ────────────────────
    const updateOnboardingFlag = useCallback((sess) => {
        if (!sess) {
            setOnboardingComplete(false)
            return
        }
        const meta = getJwtAppMeta(sess)
        const isComplete = meta.onboarding_complete === true
        console.log('[AuthContext] updateOnboardingFlag:', isComplete)
        setOnboardingComplete(isComplete)
    }, [])

    // ── Hydrate tenant data once we have a valid session ──────────────────────
    const hydrateTenant = useCallback(async (sess) => {
        if (!sess) {
            setTenant(null); setPlan(null); setUsage(null); setJoinCode(null)
            return
        }

        const meta = getJwtAppMeta(sess)
        if (!meta.tenant_id) {
            console.log('[AuthContext] No tenant_id in JWT — skipping hydrate')
            return
        }

        console.log('[AuthContext] JWT has tenant_id=%s role=%s', meta.tenant_id, meta.role)

        let tenantData = null
        let planData = null

        try {
            const [usageData, joinCodeData] = await Promise.all([
                adminGetUsage(),
                adminGetJoinCode(),
            ])
            setUsage(usageData)
            setJoinCode(joinCodeData?.join_code ?? null)

            tenantData = {
                display_name: usageData?.display_name ?? '',
                slug: usageData?.slug ?? '',
                status: usageData?.status ?? 'active',
            }
            planData = {
                name: usageData?.plan ?? '',
                max_vectors: usageData?.vectors?.limit ?? 0,
                max_users: usageData?.users?.limit ?? 0,
            }
        } catch (err) {
            console.warn('[AuthContext] Failed to hydrate tenant data from API:', err.message)
            // Fallback to JWT claims (only tenant_id is guaranteed)
            tenantData = {
                display_name: meta.tenant_name || 'My Workspace',
                slug: meta.tenant_slug || meta.tenant_id,
                status: 'active',
            }
            // planData remains null
        }

        if (tenantData) setTenant(tenantData)
        if (planData) setPlan(planData)
    }, [])

    // ── Initial session restore + auth state listener ─────────────────────────
    useEffect(() => {
        let mounted = true
        let timeoutId = null

        const handleAuthChange = async (sess) => {
            if (!mounted) return
            setLoading(true)
            setSession(sess)
            updateOnboardingFlag(sess)
            try {
                await hydrateTenant(sess)
            } catch (err) {
                console.error('[AuthContext] hydrateTenant error:', err)
            } finally {
                if (mounted) setLoading(false)
            }
        }

        const debouncedHandle = (sess) => {
            if (timeoutId) clearTimeout(timeoutId)
            timeoutId = setTimeout(() => handleAuthChange(sess), 50)
        }

        const { data: { subscription } } = supabase.auth.onAuthStateChange(
            async (event, sess) => {
                if (!mounted) return
                console.log('[AuthContext] Auth event (debounced):', event)
                debouncedHandle(sess)
            }
        )

        const init = async () => {
            const { data: { session: sess } } = await supabase.auth.getSession()
            if (!mounted) return
            setSession(sess)
            updateOnboardingFlag(sess)
            try {
                await hydrateTenant(sess)
            } finally {
                if (mounted) setLoading(false)
            }
        }
        init()

        return () => {
            mounted = false
            subscription.unsubscribe()
            if (timeoutId) clearTimeout(timeoutId)
        }
    }, [hydrateTenant, updateOnboardingFlag])

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
        return await res.json()
    }

    const logout = async () => {
        await supabase.auth.signOut()
        setSession(null)
        setTenant(null)
        setPlan(null)
        setUsage(null)
        setJoinCode(null)
        setOnboardingComplete(false)
    }

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

    // Now just returns the derived state (no JWT decode on every call)
    const isOnboardingComplete = useCallback(() => onboardingComplete, [onboardingComplete])

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

export function useAuth() {
    const ctx = useContext(AuthContext)
    if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
    return ctx
}