// src/components/JoinCodeManager.jsx
//
// Displays the tenant join code in a large monospace panel.
// Features: one-click copy, regenerate with inline confirmation.
// Reads joinCode + refreshJoinCode from AuthContext.
// Calls adminRegenJoinCode() from api.js on regeneration.

import { useState } from 'react'
import { useAuth } from '../context/AuthContext'
import { adminRegenJoinCode } from '../api'

export default function JoinCodeManager() {
  const { joinCode, refreshJoinCode } = useAuth()

  const [copied,     setCopied]     = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [loading,    setLoading]    = useState(false)
  const [error,      setError]      = useState(null)
  const [success,    setSuccess]    = useState(false)

  // ── Copy to clipboard ─────────────────────────────────────────────────────
  const handleCopy = async () => {
    if (!joinCode) return
    try {
      await navigator.clipboard.writeText(joinCode)
      setCopied(true)
      setTimeout(() => setCopied(false), 2200)
    } catch {
      // Fallback for browsers without clipboard API
      const el = document.createElement('textarea')
      el.value = joinCode
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
      setCopied(true)
      setTimeout(() => setCopied(false), 2200)
    }
  }

  // ── Regenerate ────────────────────────────────────────────────────────────
  const handleRegen = async () => {
    setLoading(true)
    setError(null)
    setSuccess(false)
    try {
      await adminRegenJoinCode()
      await refreshJoinCode()
      setConfirming(false)
      setSuccess(true)
      setTimeout(() => setSuccess(false), 3000)
    } catch (err) {
      setError(err.message ?? 'Failed to regenerate join code.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Description */}
      <div>
        <h3 style={{
          fontFamily: 'var(--font-display)', fontWeight: 700,
          fontSize: '.88rem', color: 'var(--text-0)', marginBottom: 6,
        }}>
          Employee Join Code
        </h3>
        <p style={{ fontSize: '.78rem', color: 'var(--text-2)', lineHeight: 1.65 }}>
          Share this code with your employees. They enter it during mobile app signup to join
          your organisation's knowledge base automatically.
        </p>
      </div>

      {/* Code card */}
      <div style={{
        background: 'var(--bg-1)',
        border: '1px solid var(--border-md)',
        borderRadius: 'var(--r-xl)',
        padding: '36px 28px',
        display: 'flex', flexDirection: 'column',
        alignItems: 'center', gap: 24,
      }}>
        {/* The code itself */}
        <div style={{
          fontFamily: 'var(--font-mono)', fontWeight: 500,
          fontSize: '2.6rem', letterSpacing: '.3em',
          color: 'var(--accent-text)',
          textShadow: '0 0 32px rgba(124,106,247,.25)',
          userSelect: 'all',
          cursor: 'text',
        }}>
          {joinCode ?? '— — — —'}
        </div>

        {/* Helper text */}
        <p style={{
          fontSize: '.68rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.06em',
          textAlign: 'center',
        }}>
          e.g. SHIP-4829 · Ask your employees to enter this during mobile signup
        </p>

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', justifyContent: 'center' }}>
          {/* Copy */}
          <button
            onClick={handleCopy}
            style={{
              background: copied ? 'var(--success-bg)' : 'var(--bg-3)',
              border: `1px solid ${copied ? 'var(--success-border)' : 'var(--border-md)'}`,
              borderRadius: 'var(--r-md)',
              color: copied ? 'var(--success)' : 'var(--text-1)',
              fontFamily: 'var(--font-mono)', fontSize: '.75rem',
              padding: '9px 20px', cursor: 'pointer',
              transition: 'all .15s',
              display: 'flex', alignItems: 'center', gap: 7,
            }}
          >
            <span>{copied ? '✓' : '⎘'}</span>
            {copied ? 'Copied!' : 'Copy code'}
          </button>

          {/* Regenerate or Confirm */}
          {!confirming ? (
            <button
              onClick={() => setConfirming(true)}
              style={{
                background: 'var(--bg-3)',
                border: '1px solid var(--border-md)',
                borderRadius: 'var(--r-md)',
                color: 'var(--text-2)',
                fontFamily: 'var(--font-mono)', fontSize: '.75rem',
                padding: '9px 20px', cursor: 'pointer',
                transition: 'all .15s',
                display: 'flex', alignItems: 'center', gap: 7,
              }}
            >
              <span>↻</span> Regenerate
            </button>
          ) : (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 8,
              background: 'var(--danger-bg)',
              border: '1px solid var(--danger-border)',
              borderRadius: 'var(--r-md)',
              padding: '7px 12px',
            }}>
              <span style={{
                fontSize: '.7rem', color: 'var(--danger)',
                fontFamily: 'var(--font-mono)',
              }}>
                This will invalidate the current code.
              </span>
              <button
                onClick={handleRegen}
                disabled={loading}
                style={{
                  background: 'var(--danger)', color: '#fff', border: 'none',
                  borderRadius: 'var(--r-sm)', padding: '4px 11px',
                  fontSize: '.7rem', fontFamily: 'var(--font-mono)',
                  cursor: loading ? 'wait' : 'pointer',
                  display: 'flex', alignItems: 'center', gap: 5,
                }}
              >
                {loading && (
                  <span style={{
                    width: 10, height: 10, borderRadius: '50%',
                    border: '2px solid rgba(255,255,255,.3)', borderTopColor: '#fff',
                    animation: 'spin .7s linear infinite', display: 'inline-block',
                  }} />
                )}
                {loading ? '…' : 'Confirm'}
              </button>
              <button
                onClick={() => { setConfirming(false); setError(null) }}
                style={{
                  background: 'none', border: 'none',
                  cursor: 'pointer', color: 'var(--text-3)',
                  fontSize: '1.1rem', lineHeight: 1, padding: 0,
                }}
              >
                ×
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Success message */}
      {success && (
        <div style={{
          background: 'var(--success-bg)', border: '1px solid var(--success-border)',
          borderRadius: 'var(--r-md)', padding: '10px 14px',
          fontSize: '.75rem', color: 'var(--success)',
          fontFamily: 'var(--font-mono)', animation: 'fadeUp .2s var(--ease)',
        }}>
          ✓ New join code generated. Old code is now invalid.
        </div>
      )}

      {/* Error */}
      {error && (
        <div style={{
          background: 'var(--danger-bg)', border: '1px solid var(--danger-border)',
          borderRadius: 'var(--r-md)', padding: '10px 14px',
          fontSize: '.75rem', color: 'var(--danger)',
          fontFamily: 'var(--font-mono)',
        }}>
          {error}
        </div>
      )}

      {/* Usage hint */}
      <div style={{
        background: 'var(--bg-2)', border: '1px solid var(--border)',
        borderRadius: 'var(--r-md)', padding: '14px 16px',
        display: 'flex', gap: 12,
      }}>
        <span style={{ fontSize: '1rem', flexShrink: 0 }}>💡</span>
        <div style={{ fontSize: '.75rem', color: 'var(--text-2)', lineHeight: 1.65 }}>
          <strong style={{ color: 'var(--text-1)' }}>How it works:</strong> When an employee signs up
          on the mobile app, they enter this code. The app verifies it against your account and adds
          them to your organisation automatically — no manual approval needed.
        </div>
      </div>
    </div>
  )
}