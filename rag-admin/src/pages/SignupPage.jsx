// src/pages/SignupPage.jsx
//
// Admin signup: Company Name + Email + Password → POST /auth/admin/signup
//
// Two outcomes after submit:
//   A) Email confirmation OFF in Supabase → signup() signs in automatically
//      → session set → AuthRedirect sends user to /plans
//   B) Email confirmation ON → signInWithPassword fails with "Email not confirmed"
//      → we catch it and navigate to /verify manually

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { cardStyle, inputStyle, ErrorBanner, SubmitButton } from './LoginPage'

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

export default function SignupPage() {
  const { signup } = useAuth()
  const navigate = useNavigate()

  const [email,      setEmail]      = useState('')
  const [password,   setPassword]   = useState('')
  const [company,    setCompany]    = useState('')
  const [error,      setError]      = useState('')
  const [loading,    setLoading]    = useState(false)
  const [pwStrength, setPwStrength] = useState(0)

  const checkStrength = (pw) => {
    let s = 0
    if (pw.length >= 8) s++
    if (/[A-Z]/.test(pw)) s++
    if (/[0-9!@#$%^&*]/.test(pw)) s++
    setPwStrength(s)
  }

  const handleSubmit = async (e) => {
  e.preventDefault()
  setError('')
  if (password.length < 8) { setError('Password must be at least 8 characters.'); return }
  if (!company.trim()) { setError('Company name is required.'); return }

  setLoading(true)
  try {
    await signup(email.trim(), password, company.trim())
    // Signup done — send them to login (or /plans if you want plan selection first)
    navigate('/login', { replace: true })
  } catch (err) {
    setError(err.message)
  } finally {
    setLoading(false)
  }
}

  const strengthColors = ['var(--danger)', 'var(--warn)', '#22c55e']
  const strengthLabels = ['Weak', 'Fair', 'Strong']

  return (
    <div style={{
      minHeight: '100vh', display: 'flex',
      alignItems: 'center', justifyContent: 'center', padding: 24,
    }}>
      <div style={{ ...cardStyle, maxWidth: 440 }}>
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
          <Logo />
          <div>
            <div style={{ fontFamily: 'var(--font-display)', fontWeight: 800, fontSize: '1.1rem', color: 'var(--text-0)' }}>
              DocMind Admin
            </div>
            <div style={{ fontSize: '.65rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)', letterSpacing: '.1em', textTransform: 'uppercase', marginTop: 2 }}>
              Create your admin account
            </div>
          </div>
        </div>

        <p style={{ fontSize: '.78rem', color: 'var(--text-2)', marginBottom: 24, lineHeight: 1.6 }}>
          You'll manage your organisation's knowledge base from this account.
        </p>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* Company name */}
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={labelStyle}>Company Name</span>
            <input
              type="text"
              value={company}
              onChange={e => setCompany(e.target.value)}
              placeholder="Acme Shipping Co."
              required
              autoFocus
              style={inputStyle}
            />
          </label>

          {/* Email */}
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={labelStyle}>Work Email</span>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="you@company.com"
              required
              style={inputStyle}
            />
          </label>

          {/* Password */}
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={labelStyle}>Password</span>
            <input
              type="password"
              value={password}
              onChange={e => { setPassword(e.target.value); checkStrength(e.target.value) }}
              placeholder="Min. 8 characters"
              required
              style={inputStyle}
            />
            {password && (
              <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
                {[0, 1, 2].map(i => (
                  <div key={i} style={{
                    flex: 1, height: 3, borderRadius: 2,
                    background: i < pwStrength ? strengthColors[pwStrength - 1] : 'var(--border-md)',
                    transition: 'background .2s',
                  }} />
                ))}
                <span style={{ fontSize: '.65rem', color: strengthColors[pwStrength - 1] ?? 'var(--text-3)', fontFamily: 'var(--font-mono)', marginLeft: 4 }}>
                  {pwStrength > 0 ? strengthLabels[pwStrength - 1] : ''}
                </span>
              </div>
            )}
          </label>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <SubmitButton loading={loading}>Create Account</SubmitButton>
        </form>

        <div style={{ marginTop: 20, textAlign: 'center', fontSize: '.75rem', color: 'var(--text-3)' }}>
          Already have an account?{' '}
          <Link to="/login" style={{ color: 'var(--accent-text)', textDecoration: 'none', fontWeight: 600 }}>
            Sign in
          </Link>
        </div>
      </div>
    </div>
  )
}

const labelStyle = {
  fontSize: '.72rem', color: 'var(--text-2)',
  fontFamily: 'var(--font-mono)', letterSpacing: '.08em', textTransform: 'uppercase',
}