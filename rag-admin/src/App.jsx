// src/App.jsx
// Router shell only — no UI here.
//
// Routing logic:
//   - No session              → /login or /signup or /verify (public routes)
//   - Session, no tenant      → /plans  (new user, pick a plan)
//   - Session, not onboarded  → /onboarding
//   - Session, onboarded      → dashboard (/)

import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useEffect } from 'react'
import { useAuth } from './context/AuthContext'

import LoginPage from './pages/LoginPage'
import SignupPage from './pages/SignupPage'
import VerifyEmailPage from './pages/VerifyEmailPage'
import PlanSelectionPage from './pages/PlanSelectionPage'
import PaymentPage from './pages/PaymentPage'
import OnboardingPage from './pages/OnboardingPage'
import DashboardPage from './pages/DashboardPage'

// ── Spinner ────────────────────────────────────────────────────────────────────
function SplashScreen() {
  return (
    <div style={{
      minHeight: '100vh', display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center', gap: 16,
    }}>
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        border: '2px solid var(--bg-3)',
        borderTopColor: 'var(--accent)',
        animation: 'spin .8s linear infinite',
      }} />
      <span style={{
        color: 'var(--text-3)', fontFamily: 'var(--font-mono)',
        fontSize: '.72rem', letterSpacing: '.12em',
      }}>INITIALISING…</span>
    </div>
  )
}

// ── Auth-driven redirect component ────────────────────────────────────────────
// Mounted on every protected route. Watches auth state and redirects
// reactively so navigation always reflects the current session state.
//
// Rule evaluation order matters — every branch must `return` so only one
// redirect fires per render cycle.
function AuthRedirect() {
  const { isAuthenticated, isOnboardingComplete, loading, tenant } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    // While AuthContext is hydrating (initial load OR after any auth event),
    // do nothing. App renders <SplashScreen /> during this window anyway.
    if (loading) return

    console.log('[AuthRedirect]', {
      path: location.pathname,
      isAuthenticated,
      tenantSlug: tenant?.slug,
      onboardingComplete: isOnboardingComplete(),
      loading,
    })

    const path = location.pathname

    // 1. Not authenticated → only public routes allowed
    if (!isAuthenticated) {
      if (!['/login', '/signup', '/verify', '/plans', '/payment'].includes(path)) {
        navigate('/login', { replace: true })
      }
      return
    }

    // 2. Authenticated – redirect away from auth/pre-auth pages
    if (['/login', '/signup', '/verify', '/payment'].includes(path)) {
      if (!tenant?.slug) {
        navigate('/plans', { replace: true })
      } else if (!isOnboardingComplete()) {
        navigate('/onboarding', { replace: true })
      } else {
        navigate('/', { replace: true })
      }
      return
    }

    // 3. Authenticated but no tenant – only /plans allowed
    if (!tenant?.slug && path !== '/plans') {
      navigate('/plans', { replace: true })
      return
    }

    // 4. Authenticated + tenant but onboarding incomplete – only /onboarding allowed
    if (tenant?.slug && !isOnboardingComplete() && path !== '/onboarding') {
      navigate('/onboarding', { replace: true })
      return
    }

    // 5. ── FIX: Onboarding complete but still on /onboarding → go to dashboard ──
    // This fires after OnboardingPage calls refreshSession(). The new JWT has
    // onboarding_complete=true, AuthContext re-hydrates and sets loading=false,
    // then this rule fires and completes the navigation to the dashboard.
    // Without this rule the user was permanently stuck on /onboarding because
    // AuthRedirect had no path to navigate *away* from it once complete.
    if (path === '/onboarding' && tenant?.slug && isOnboardingComplete()) {
      navigate('/', { replace: true })
      return
    }

  }, [isAuthenticated, isOnboardingComplete, loading, tenant, location.pathname, navigate])

  return null
}

// ── Protected route wrapper ────────────────────────────────────────────────────
function ProtectedRoute({ children }) {
  const { isAuthenticated, loading } = useAuth()
  const location = useLocation()

  if (loading) return <SplashScreen />
  if (!isAuthenticated) return <Navigate to="/login" state={{ from: location }} replace />
  return children
}

// ── App ───────────────────────────────────────────────────────────────────────
export default function App() {
  const { loading } = useAuth()

  if (loading) return <SplashScreen />

  return (
    <>
      {/* AuthRedirect watches state changes and navigates reactively */}
      <AuthRedirect />

      <Routes>
        {/* Public */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/verify" element={<VerifyEmailPage />} />
        <Route path="/plans" element={<PlanSelectionPage />} />
        <Route path="/payment" element={<PaymentPage />} />

        {/* Protected */}
        <Route
          path="/onboarding"
          element={
            <ProtectedRoute>
              <OnboardingPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/*"
          element={
            <ProtectedRoute>
              <DashboardPage />
            </ProtectedRoute>
          }
        />
      </Routes>
    </>
  )
}