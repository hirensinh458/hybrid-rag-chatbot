// src/App.jsx
// Router shell only — no UI here.
//
// Routing logic:
//   - No session              → /login or /signup or /verify (public routes)
//   - Session, no tenant      → /plans  (new user, pick a plan)
//   - Session, not onboarded  → /onboarding
//   - Session, onboarded      → dashboard (/)

import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useEffect }        from 'react'
import { useAuth }          from './context/AuthContext'

import LoginPage         from './pages/LoginPage'
import SignupPage        from './pages/SignupPage'
import VerifyEmailPage   from './pages/VerifyEmailPage'
import PlanSelectionPage from './pages/PlanSelectionPage'
import OnboardingPage    from './pages/OnboardingPage'
import DashboardPage     from './pages/DashboardPage'

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
function AuthRedirect() {
  const { isAuthenticated, isOnboardingComplete, loading, tenant } = useAuth()
  const navigate  = useNavigate()
  const location  = useLocation()

  useEffect(() => {
    if (loading) return

    const path = location.pathname

    // Public paths — don't interfere
    if (['/login', '/signup', '/verify', '/plans'].includes(path)) return

    if (!isAuthenticated) {
      navigate('/login', { replace: true })
      return
    }

    // Authenticated but no tenant yet (just confirmed email, no plan chosen)
    if (!tenant?.slug && path !== '/plans') {
      navigate('/plans', { replace: true })
      return
    }

    // Authenticated + tenant, but onboarding not done
    if (tenant?.slug && !isOnboardingComplete() && path !== '/onboarding') {
      navigate('/onboarding', { replace: true })
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
        <Route path="/login"  element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/verify" element={<VerifyEmailPage />} />
        <Route path="/plans"  element={<PlanSelectionPage />} />

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