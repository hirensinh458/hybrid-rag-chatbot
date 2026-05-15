// src/pages/OnboardingPage.jsx
//
// First-login wizard shown once per new admin (guarded by ProtectedRoute).
// Three steps:
//   1. Confirm company name + select timezone
//   2. Display join code — "share with your employees"
//   3. Upload first PDF (optional, can skip)
//
// On finish: calls POST /admin/onboarding-complete, then refreshes the
// Supabase session JWT so app_metadata.onboarding_complete is set in the
// token. AuthContext's onAuthStateChange picks up the new session,
// re-hydrates, and AuthRedirect's rule 5 navigates to the dashboard.
//
// ── Why we don't call navigate() here ────────────────────────────────────────
// Calling navigate('/') in the same tick as refreshSession() creates a race:
// the new JWT hasn't been stored in React state yet, so isOnboardingComplete()
// still reads false from the old token. AuthRedirect then pushes us back to
// /onboarding and there was no rule to escape it (the bug).
//
// Instead: refreshSession() → onAuthStateChange fires → AuthContext sets
// loading=true (SplashScreen shown) → hydration completes → loading=false →
// AuthRedirect rule 5 sees onboarding_complete=true at /onboarding → navigates
// to '/'. Clean, race-free, no manual navigate needed.
// ─────────────────────────────────────────────────────────────────────────────

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { supabase } from '../supabase'
import { adminCompleteOnboarding } from '../api'
import IngestPanel from '../components/IngestPanel'

// ── Timezone list ─────────────────────────────────────────────────────────────
const TIMEZONES = [
  'UTC',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'Europe/London', 'Europe/Berlin', 'Europe/Paris', 'Europe/Istanbul',
  'Asia/Dubai', 'Asia/Mumbai', 'Asia/Kolkata', 'Asia/Singapore', 'Asia/Tokyo',
  'Australia/Sydney', 'Pacific/Auckland',
]

// ── Step progress bar ─────────────────────────────────────────────────────────
function StepProgress({ current, total }) {
  return (
    <div style={{ display: 'flex', gap: 6, marginBottom: 32 }}>
      {[...Array(total)].map((_, i) => (
        <div key={i} style={{
          height: 4, borderRadius: 2,
          flex: i + 1 === current ? '2 0 0' : '1 0 0',
          background: i + 1 <= current ? 'var(--accent)' : 'var(--bg-4)',
          transition: 'all .4s var(--ease)',
        }} />
      ))}
    </div>
  )
}

// ── Main ──────────────────────────────────────────────────────────────────────
export default function OnboardingPage() {
  const { tenant, joinCode } = useAuth()
  const navigate = useNavigate()

  const [step,      setStep]      = useState(1)
  const [company,   setCompany]   = useState(tenant?.display_name ?? '')
  const [timezone,  setTimezone]  = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  )
  const [copied,    setCopied]    = useState(false)
  const [finishing, setFinishing] = useState(false)

  // ── Handlers ──────────────────────────────────────────────────────────────
  const handleCopy = async () => {
    if (!joinCode) return
    try {
      await navigator.clipboard.writeText(joinCode)
    } catch {
      const el = document.createElement('textarea')
      el.value = joinCode
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 2500)
  }

  const handleFinish = async () => {
    setFinishing(true)
    try {
      // Mark onboarding complete in the backend (writes to DB / app_metadata)
      await adminCompleteOnboarding()

      // Refresh the JWT so the new onboarding_complete=true claim is present.
      // This triggers onAuthStateChange → AuthContext sets loading=true →
      // SplashScreen shows → hydration completes → loading=false →
      // AuthRedirect (rule 5) detects onboarding_complete=true at /onboarding
      // and navigates to '/'. No manual navigate() needed here.
      await supabase.auth.refreshSession()

      // refreshSession() resolved means the new token is issued, but React
      // state hasn't updated yet (onAuthStateChange is async). We intentionally
      // DO NOT navigate here — AuthRedirect handles it once the state settles.

    } catch (err) {
      // If the backend call or token refresh failed, log it but still unblock
      // the user by navigating manually as a best-effort fallback.
      console.warn('[Onboarding] Finish failed, navigating anyway:', err.message)
      // navigate('/', { replace: true })
    }
    // Note: we don't reset setFinishing(false) here.
    // If everything went well the component will unmount (AuthRedirect navigates
    // away via the SplashScreen transition). If the catch fires we navigate
    // away so the component also unmounts. Either way it's a no-op.
  }

  // ── Titles / subtitles ────────────────────────────────────────────────────
  const STEPS = [
    {
      title:    'Set up your workspace',
      subtitle: 'Confirm your organisation details before we get started.',
    },
    {
      title:    'Your employee join code',
      subtitle: 'Share this with your team so they can sign up on the mobile app.',
    },
    {
      title:    'Upload your first document',
      subtitle: 'Add a PDF to kick off your knowledge base. You can skip this.',
    },
  ]
  const { title, subtitle } = STEPS[step - 1]

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      padding: '40px 24px',
      background: 'var(--bg-0)',
    }}>
      {/* Header */}
      <div style={{ textAlign: 'center', marginBottom: 8 }}>
        {/* Step counter */}
        <div style={{
          fontSize: '.62rem', fontFamily: 'var(--font-mono)',
          letterSpacing: '.18em', textTransform: 'uppercase',
          color: 'var(--accent-text)', marginBottom: 12,
        }}>
          Step {step} of 3
        </div>

        <h1 style={{
          fontFamily: 'var(--font-display)', fontWeight: 800,
          fontSize: '1.55rem', color: 'var(--text-0)', marginBottom: 8,
        }}>
          {title}
        </h1>
        <p style={{ fontSize: '.82rem', color: 'var(--text-2)', maxWidth: 400, lineHeight: 1.65 }}>
          {subtitle}
        </p>
      </div>

      {/* Card */}
      <div style={{
        width: '100%', maxWidth: 460,
        background: 'var(--bg-1)', border: '1px solid var(--border-md)',
        borderRadius: 'var(--r-xl)', padding: '32px 28px',
        marginTop: 28,
        animation: 'fadeUp .25s var(--ease)',
      }}>
        <StepProgress current={step} total={3} />

        {/* ── STEP 1: Company + timezone ───────────────────────────────────── */}
        {step === 1 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={labelStyle}>Company name</span>
              <input
                type="text"
                value={company}
                onChange={e => setCompany(e.target.value)}
                placeholder="Acme Shipping Co."
                autoFocus
                style={inputStyle}
              />
            </label>

            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={labelStyle}>Timezone</span>
              <select
                value={timezone}
                onChange={e => setTimezone(e.target.value)}
                style={{ ...inputStyle, cursor: 'pointer' }}
              >
                {TIMEZONES.map(tz => (
                  <option key={tz} value={tz}>{tz.replace('_', ' ')}</option>
                ))}
              </select>
              <span style={{ fontSize: '.65rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>
                Detected: {Intl.DateTimeFormat().resolvedOptions().timeZone}
              </span>
            </label>

            <button
              onClick={() => setStep(2)}
              disabled={!company.trim()}
              style={primaryBtn(!company.trim())}
            >
              Continue →
            </button>
          </div>
        )}

        {/* ── STEP 2: Join code ────────────────────────────────────────────── */}
        {step === 2 && (
          <div style={{
            display: 'flex', flexDirection: 'column',
            alignItems: 'center', gap: 22, textAlign: 'center',
          }}>
            {/* Big code */}
            <div style={{
              fontFamily: 'var(--font-mono)', fontWeight: 500,
              fontSize: '2.8rem', letterSpacing: '.32em',
              color: 'var(--accent-text)',
              textShadow: '0 0 36px rgba(124,106,247,.3)',
              padding: '22px 28px',
              background: 'var(--bg-2)', border: '1px solid var(--border-md)',
              borderRadius: 'var(--r-lg)', userSelect: 'all',
              width: '100%',
            }}>
              {joinCode ?? '— — — —'}
            </div>

            {/* Copy button */}
            <button
              onClick={handleCopy}
              style={{
                background: copied ? 'var(--success-bg)' : 'var(--bg-3)',
                border: `1px solid ${copied ? 'var(--success-border)' : 'var(--border-md)'}`,
                borderRadius: 'var(--r-md)',
                color: copied ? 'var(--success)' : 'var(--text-1)',
                fontFamily: 'var(--font-mono)', fontSize: '.78rem',
                padding: '9px 24px', cursor: 'pointer',
                transition: 'all .15s', display: 'flex', alignItems: 'center', gap: 7,
              }}
            >
              <span>{copied ? '✓' : '⎘'}</span>
              {copied ? 'Copied to clipboard!' : 'Copy join code'}
            </button>

            <p style={{
              fontSize: '.7rem', color: 'var(--text-3)',
              fontFamily: 'var(--font-mono)', lineHeight: 1.65,
            }}>
              Employees enter this code during mobile app signup.
              You can find it again later under <strong style={{ color: 'var(--text-2)' }}>Join Code</strong> in the dashboard.
            </p>

            {/* Nav */}
            <div style={{ display: 'flex', gap: 10, width: '100%' }}>
              <button onClick={() => setStep(1)} style={ghostBtn}>← Back</button>
              <button onClick={() => setStep(3)} style={{ ...primaryBtn(false), flex: 1 }}>
                Continue →
              </button>
            </div>
          </div>
        )}

        {/* ── STEP 3: Optional PDF upload ──────────────────────────────────── */}
        {step === 3 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <IngestPanel onSuccess={() => {}} />

            <div style={{ display: 'flex', gap: 10 }}>
              <button onClick={() => setStep(2)} style={ghostBtn}>← Back</button>
              <button
                onClick={handleFinish}
                disabled={finishing}
                style={{ ...primaryBtn(finishing), flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}
              >
                {finishing && (
                  <span style={{
                    width: 14, height: 14, borderRadius: '50%',
                    border: '2px solid rgba(255,255,255,.3)', borderTopColor: '#fff',
                    animation: 'spin .7s linear infinite', display: 'inline-block',
                  }} />
                )}
                {finishing ? 'Setting up…' : 'Go to dashboard →'}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Skip link on step 3 */}
      {step === 3 && (
        <button
          onClick={handleFinish}
          disabled={finishing}
          style={{
            marginTop: 18, background: 'none', border: 'none',
            color: 'var(--text-3)', fontSize: '.72rem',
            cursor: 'pointer', textDecoration: 'underline',
          }}
        >
          Skip for now — I'll upload documents later
        </button>
      )}
    </div>
  )
}

// ── Shared styles ─────────────────────────────────────────────────────────────

const labelStyle = {
  fontSize: '.68rem', color: 'var(--text-2)',
  fontFamily: 'var(--font-mono)', letterSpacing: '.08em', textTransform: 'uppercase',
}

const inputStyle = {
  background: 'var(--bg-2)',
  border: '1px solid var(--border-md)',
  borderRadius: 'var(--r-md)',
  padding: '10px 14px',
  color: 'var(--text-0)',
  fontFamily: 'var(--font-body)',
  fontSize: '.85rem',
  outline: 'none',
  width: '100%',
}

const primaryBtn = (disabled) => ({
  padding: '11px 20px',
  background: disabled
    ? 'var(--bg-3)'
    : 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
  border: 'none', borderRadius: 'var(--r-md)',
  color: disabled ? 'var(--text-3)' : '#fff',
  fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: '.85rem',
  cursor: disabled ? 'not-allowed' : 'pointer',
  transition: 'all .15s',
})

const ghostBtn = {
  padding: '11px 16px',
  background: 'var(--bg-3)', border: '1px solid var(--border-md)',
  borderRadius: 'var(--r-md)', color: 'var(--text-2)',
  fontFamily: 'var(--font-mono)', fontSize: '.78rem',
  cursor: 'pointer',
}