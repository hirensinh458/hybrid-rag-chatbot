// src/pages/LoginPage.jsx
// Email + password login using Supabase Auth.
// After login, AuthContext.onAuthStateChange fires → AuthRedirect in App.jsx
// handles navigation based on tenant/onboarding state. No manual navigate here.

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

function Logo() {
  return (
    <div style={{
      width: 36, height: 36, borderRadius: 10, flexShrink: 0,
      background: 'linear-gradient(135deg, var(--accent) 0%, #5b4dd4 100%)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: '1.1rem', color: '#fff',
    }}>✦</div>
  )
}

export default function LoginPage() {
  const { login, isAuthenticated } = useAuth()

  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [error,    setError]    = useState('')
  const [loading,  setLoading]  = useState(false)

  // If already authenticated, AuthRedirect will handle navigation — nothing to do here
  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(email.trim(), password)
      // No navigate() here — AuthRedirect watches onAuthStateChange and
      // redirects to /plans, /onboarding, or / based on tenant state
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={centerStyle}>
      <div style={cardStyle}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 28 }}>
          <Logo />
          <div>
            <div style={{ fontFamily: 'var(--font-display)', fontWeight: 800, fontSize: '1.1rem', color: 'var(--text-0)' }}>
              DocMind Admin
            </div>
            <div style={{ fontSize: '.65rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)', letterSpacing: '.1em', textTransform: 'uppercase', marginTop: 2 }}>
              Sign in to continue
            </div>
          </div>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <Field
            label="Email"
            type="email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            placeholder="admin@company.com"
            autoFocus
            required
          />
          <Field
            label="Password"
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="••••••••"
            required
          />

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <SubmitButton loading={loading}>Sign In</SubmitButton>
        </form>

        {/* Footer */}
        <div style={{ marginTop: 20, textAlign: 'center', fontSize: '.75rem', color: 'var(--text-3)' }}>
          No account?{' '}
          <Link to="/signup" style={{ color: 'var(--accent-text)', textDecoration: 'none', fontWeight: 600 }}>
            Create one
          </Link>
        </div>
      </div>
    </div>
  )
}

// ── Shared sub-components ─────────────────────────────────────────────────────

function Field({ label, type, value, onChange, placeholder, autoFocus, required }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <span style={{ fontSize: '.72rem', color: 'var(--text-2)', fontFamily: 'var(--font-mono)', letterSpacing: '.08em', textTransform: 'uppercase' }}>
        {label}
      </span>
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        autoFocus={autoFocus}
        required={required}
        style={inputStyle}
      />
    </label>
  )
}

export function ErrorBanner({ children }) {
  return (
    <div style={{
      background: 'var(--danger-bg)', border: '1px solid var(--danger-border)',
      borderRadius: 'var(--r-md)', padding: '10px 14px',
      fontSize: '.78rem', color: 'var(--danger)',
      fontFamily: 'var(--font-mono)',
    }}>
      {children}
    </div>
  )
}

export function SubmitButton({ loading, children, disabled, type = 'submit', onClick, style: extraStyle = {} }) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={loading || disabled}
      style={{
        padding: '11px 0',
        background: loading || disabled
          ? 'var(--bg-3)'
          : 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
        border: 'none', borderRadius: 'var(--r-md)',
        color: loading || disabled ? 'var(--text-3)' : '#fff',
        fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: '.85rem',
        cursor: loading || disabled ? 'not-allowed' : 'pointer',
        opacity: loading || disabled ? .7 : 1,
        transition: 'all .15s',
        display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
        ...extraStyle,
      }}
    >
      {loading && (
        <span style={{
          width: 14, height: 14, borderRadius: '50%',
          border: '2px solid rgba(255,255,255,.3)',
          borderTopColor: '#fff',
          animation: 'spin .7s linear infinite', display: 'inline-block',
        }} />
      )}
      {children}
    </button>
  )
}

// ── Styles ────────────────────────────────────────────────────────────────────

const centerStyle = {
  minHeight: '100vh',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  padding: 24,
}

export const cardStyle = {
  width: '100%', maxWidth: 400,
  background: 'var(--bg-1)',
  border: '1px solid var(--border-md)',
  borderRadius: 'var(--r-xl)',
  padding: '36px 32px',
  animation: 'fadeUp .25s var(--ease)',
}

export const inputStyle = {
  background: 'var(--bg-2)',
  border: '1px solid var(--border-md)',
  borderRadius: 'var(--r-md)',
  padding: '10px 14px',
  color: 'var(--text-0)',
  fontFamily: 'var(--font-body)',
  fontSize: '.85rem',
  outline: 'none',
  width: '100%',
  transition: 'border-color .15s',
}