// src/App.jsx
//
// CHANGE: Added SyncPanel component in the left column (below StatsPanel).
//
//   Behaviour is identical to the Sync section in rag-frontend/src/components/Sidebar.jsx:
//     - Fetches sync status from GET /sync/status on mount and after each refresh.
//     - Shows: last synced timestamp, pending_count badge, is_syncing indicator.
//     - "Sync now" button calls POST /sync/trigger (fires background sync).
//     - Button is disabled while a sync is in progress or the backend is unreachable.
//     - After triggering, polls /sync/status every 3 s for up to 30 s so the
//       "Last synced" timestamp updates live once the background job finishes.
//       (rag-frontend does a single 3 s setTimeout; we do the same but also poll
//       a few extra times so the admin panel always shows the final result.)
//
// Everything else (auth, StatsPanel, FileManager, topbar) is UNCHANGED.

import { useState, useEffect, useCallback, useRef } from 'react'
import { adminStats, adminListFiles, fetchSyncStatus, triggerSync } from './api'
import FileManager from './components/FileManager'
import StatsPanel  from './components/StatsPanel'

export default function App() {
  const [authed,      setAuthed]      = useState(false)
  const [authChecked, setAuthChecked] = useState(false)
  const [tokenInput,  setTokenInput]  = useState('')
  const [loginError,  setLoginError]  = useState('')
  const [stats,       setStats]       = useState(null)
  const [files,       setFiles]       = useState([])
  const [loading,     setLoading]     = useState(false)

  // ── Sync state ──────────────────────────────────────────────────
  const [syncStatus,   setSyncStatus]   = useState(null)   // { last_synced, is_syncing, pending_count }
  const [syncing,      setSyncing]      = useState(false)   // local button-busy flag
  const [syncError,    setSyncError]    = useState('')
  const [syncSuccess,  setSyncSuccess]  = useState('')
  const pollRef = useRef(null)          // stores interval id for polling cleanup

  // ── Fetch stats + files ────────────────────────────────────────
  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const [s, f] = await Promise.all([adminStats(), adminListFiles()])
      setStats(s)
      setFiles(f.files || [])
    } catch (e) {
      if (e.message.includes('401') || e.message.toLowerCase().includes('token')) {
        setAuthed(false)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  // ── Fetch sync status (standalone, non-fatal) ──────────────────
  const refreshSyncStatus = useCallback(async () => {
    try {
      const s = await fetchSyncStatus()
      setSyncStatus(s)
    } catch {
      // sync not configured or backend not reachable — silently ignore
    }
  }, [])

  // ── Check auth on mount ────────────────────────────────────────
  useEffect(() => {
    const check = async () => {
      try {
        await adminStats()
        setAuthed(true)
        setAuthChecked(true)
      } catch {
        setAuthed(false)
        setAuthChecked(true)
      }
    }
    check()
  }, [])

  // ── Load data once authed ──────────────────────────────────────
  useEffect(() => {
    if (authed) {
      fetchData()
      refreshSyncStatus()
    }
  }, [authed, fetchData, refreshSyncStatus])

  // ── Stop any pending poll on unmount ──────────────────────────
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  // ── Token submit ───────────────────────────────────────────────
  const handleLogin = async (e) => {
    e.preventDefault()
    setLoginError('')
    localStorage.setItem('admin_token', tokenInput.trim())
    try {
      await adminStats()
      setAuthed(true)
    } catch {
      localStorage.removeItem('admin_token')
      setLoginError('Token rejected — check your ADMIN_TOKEN value.')
    }
  }

  const handleLogout = () => {
    localStorage.removeItem('admin_token')
    setAuthed(false)
    setStats(null)
    setFiles([])
    setSyncStatus(null)
  }

  // ── Sync now ──────────────────────────────────────────────────
  // Mirrors rag-frontend Sidebar.doSync() exactly, with an added polling
  // loop so the "Last synced" timestamp updates once the job finishes.
  const handleSyncNow = async () => {
    if (syncing || syncStatus?.is_syncing) return
    setSyncing(true)
    setSyncError('')
    setSyncSuccess('')

    // Clear any previous poll
    if (pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }

    try {
      await triggerSync()

      // Poll /sync/status every 3 s for up to 30 s (10 attempts).
      // Once is_syncing flips to false we refresh stats and stop polling.
      let attempts = 0
      pollRef.current = setInterval(async () => {
        attempts++
        try {
          const s = await fetchSyncStatus()
          setSyncStatus(s)

          // Sync finished or max attempts reached
          if (!s.is_syncing || attempts >= 10) {
            clearInterval(pollRef.current)
            pollRef.current = null
            setSyncing(false)
            if (!s.is_syncing) {
              setSyncSuccess('Sync complete')
              // Refresh stats so vector count updates immediately
              fetchData()
              // Clear success message after 4 s
              setTimeout(() => setSyncSuccess(''), 4000)
            }
          }
        } catch {
          // Poll failed — stop and surface nothing (non-fatal)
          clearInterval(pollRef.current)
          pollRef.current = null
          setSyncing(false)
        }
      }, 3000)

    } catch (e) {
      setSyncing(false)
      setSyncError(e.message || 'Sync trigger failed')
    }
  }

  // ── Loading spinner (initial auth check) ──────────────────────
  if (!authChecked) {
    return (
      <div style={centerStyle}>
        <Spinner />
        <span style={{ marginTop: 16, color: 'var(--text-3)', fontFamily: 'var(--font-mono)', fontSize: '.78rem' }}>
          Connecting…
        </span>
      </div>
    )
  }

  // ── Login screen ───────────────────────────────────────────────
  if (!authed) {
    return (
      <div style={centerStyle}>
        <div style={{
          width: '100%', maxWidth: 380,
          background: 'var(--bg-1)', border: '1px solid var(--border-md)',
          borderRadius: 'var(--r-xl)', padding: '36px 32px',
          animation: 'fadeUp .25s var(--ease)',
        }}>
          {/* Logo + title */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 28 }}>
            <Logo />
            <div>
              <div style={{ fontFamily: 'var(--font-display)', fontWeight: 800, fontSize: '1.1rem', color: 'var(--text-0)' }}>
                DocMind Admin
              </div>
              <div style={{ fontSize: '.65rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)', letterSpacing: '.1em', textTransform: 'uppercase', marginTop: 2 }}>
                RAG Management Panel
              </div>
            </div>
          </div>

          <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <label style={{ fontSize: '.76rem', color: 'var(--text-2)', fontFamily: 'var(--font-mono)' }}>
              Admin Token
            </label>
            <input
              type="password"
              value={tokenInput}
              onChange={e => setTokenInput(e.target.value)}
              placeholder="Paste your ADMIN_TOKEN here"
              autoFocus
              style={{
                background: 'var(--bg-3)', border: '1px solid var(--border-md)',
                borderRadius: 'var(--r-md)', padding: '10px 14px',
                color: 'var(--text-0)', fontFamily: 'var(--font-mono)',
                fontSize: '.82rem', outline: 'none', width: '100%',
                transition: 'border-color .15s',
              }}
            />
            {loginError && (
              <div style={{
                fontSize: '.74rem', color: 'var(--danger)',
                fontFamily: 'var(--font-mono)',
                background: 'var(--danger-bg)', border: '1px solid var(--danger-border)',
                borderRadius: 'var(--r-sm)', padding: '6px 10px',
              }}>
                {loginError}
              </div>
            )}
            <button
              type="submit"
              disabled={!tokenInput.trim()}
              style={{
                marginTop: 4, padding: '11px 0',
                background: 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
                border: 'none', borderRadius: 'var(--r-md)',
                color: '#fff', cursor: tokenInput.trim() ? 'pointer' : 'not-allowed',
                fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: '.82rem',
                opacity: tokenInput.trim() ? 1 : .5, transition: 'opacity .15s',
              }}
            >
              Enter Admin Panel
            </button>
          </form>

          <div style={{
            marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--border)',
            fontSize: '.68rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)',
            lineHeight: 1.6,
          }}>
            If <span style={{ color: 'var(--accent-text)' }}>ADMIN_TOKEN</span> is empty
            in <span style={{ color: 'var(--accent-text)' }}>.env</span>, leave this blank
            and submit to enter dev mode.
          </div>
        </div>
      </div>
    )
  }

  // ── Main panel ─────────────────────────────────────────────────
  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>

      {/* ── Topbar ── */}
      <header style={{
        height: 56, padding: '0 28px',
        background: 'var(--bg-1)', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        flexShrink: 0, position: 'sticky', top: 0, zIndex: 10,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Logo />
          <div>
            <span style={{ fontFamily: 'var(--font-display)', fontWeight: 800, fontSize: '.95rem', color: 'var(--text-0)' }}>
              DocMind Admin
            </span>
            <span style={{
              marginLeft: 10, fontSize: '.62rem', fontFamily: 'var(--font-mono)',
              letterSpacing: '.1em', color: 'var(--text-3)', textTransform: 'uppercase',
            }}>
              RAG Management
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* KB ready indicator */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6,
            background: files.length > 0 ? 'var(--teal-dim)' : 'rgba(255,255,255,.03)',
            border: `1px solid ${files.length > 0 ? 'rgba(45,212,191,.2)' : 'var(--border)'}`,
            borderRadius: 20, padding: '4px 12px',
            fontSize: '.68rem', color: files.length > 0 ? 'var(--teal)' : 'var(--text-3)',
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
              background: files.length > 0 ? 'var(--teal)' : 'var(--text-3)',
              animation: files.length > 0 ? 'pulse 2.5s ease infinite' : 'none',
            }} />
            {files.length > 0 ? `${files.length} file${files.length > 1 ? 's' : ''} indexed` : 'No documents'}
          </div>

          {/* Refresh */}
          <button
            onClick={fetchData}
            disabled={loading}
            title="Refresh stats and file list"
            style={{
              width: 32, height: 32, borderRadius: 'var(--r-md)',
              border: '1px solid var(--border-md)', background: 'transparent',
              cursor: loading ? 'not-allowed' : 'pointer',
              color: 'var(--text-2)', fontSize: '.9rem',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              opacity: loading ? .5 : 1, transition: 'all .15s',
            }}
          >
            <span style={{ display: 'inline-block', animation: loading ? 'spin 1s linear infinite' : 'none' }}>↻</span>
          </button>

          {/* Logout */}
          <button
            onClick={handleLogout}
            title="Sign out"
            style={{
              height: 32, padding: '0 12px', borderRadius: 'var(--r-md)',
              border: '1px solid var(--border-md)', background: 'transparent',
              cursor: 'pointer', color: 'var(--text-2)',
              fontFamily: 'var(--font-mono)', fontSize: '.7rem',
              transition: 'all .15s',
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      {/* ── Content ── */}
      <main style={{
        flex: 1, padding: '28px',
        display: 'grid',
        gridTemplateColumns: '280px 1fr',
        gap: 24,
        alignItems: 'start',
        maxWidth: 1100, width: '100%', margin: '0 auto',
      }}>

        {/* Left column — Stats + Sync */}
        <aside style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <StatsPanel stats={stats} />

          {/* ── SYNC PANEL — new, mirrors rag-frontend Sidebar sync section ── */}
          <SyncPanel
            syncStatus={syncStatus}
            syncing={syncing}
            syncError={syncError}
            syncSuccess={syncSuccess}
            onSyncNow={handleSyncNow}
            onRefreshStatus={refreshSyncStatus}
          />

          {/* Connection info (unchanged) */}
          <div style={{
            background: 'var(--bg-2)', border: '1px solid var(--border)',
            borderRadius: 'var(--r-lg)', padding: '14px 16px',
          }}>
            <div style={{
              fontFamily: 'var(--font-mono)', fontSize: '.62rem', fontWeight: 500,
              letterSpacing: '.12em', textTransform: 'uppercase', color: 'var(--text-3)',
              marginBottom: 10, paddingBottom: 6, borderBottom: '1px solid var(--border)',
            }}>Backend</div>
            <div style={{ fontSize: '.74rem', color: 'var(--text-2)', lineHeight: 1.7 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span>URL</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: '.7rem', color: 'var(--accent-text)' }}>
                  localhost:8000
                </span>
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4 }}>
                <span>Auth</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: '.7rem', color: localStorage.getItem('admin_token') ? 'var(--teal)' : 'var(--warn)' }}>
                  {localStorage.getItem('admin_token') ? 'Token set' : 'Dev mode'}
                </span>
              </div>
            </div>
          </div>
        </aside>

        {/* Right column — File manager (unchanged) */}
        <section style={{
          background: 'var(--bg-1)', border: '1px solid var(--border)',
          borderRadius: 'var(--r-xl)', padding: '24px',
          animation: 'fadeUp .2s var(--ease)',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            marginBottom: 24,
          }}>
            <div>
              <div style={{ fontFamily: 'var(--font-display)', fontWeight: 800, fontSize: '1.05rem', color: 'var(--text-0)' }}>
                Knowledge Base
              </div>
              <div style={{ fontSize: '.74rem', color: 'var(--text-2)', marginTop: 2 }}>
                Upload, manage, and delete indexed documents
              </div>
            </div>
          </div>

          <FileManager
            files={files}
            onRefresh={fetchData}
            disabled={loading}
          />
        </section>
      </main>
    </div>
  )
}


// ── SyncPanel ─────────────────────────────────────────────────────────────────
//
// Self-contained component that mirrors the Sync section in rag-frontend's
// Sidebar.jsx (lines 10710–10738 of that file) — same layout, same labels,
// same disabled logic, same "Syncing…" text while busy.
//
// Props:
//   syncStatus   — { last_synced: string|null, is_syncing: bool, pending_count: int }
//   syncing      — local button-busy flag (set while triggerSync() is in flight)
//   syncError    — error string to show, or ''
//   syncSuccess  — success string to show, or ''
//   onSyncNow    — callback: fires triggerSync and starts polling
//   onRefreshStatus — callback: re-fetches /sync/status

function SyncPanel({ syncStatus, syncing, syncError, syncSuccess, onSyncNow, onRefreshStatus }) {
  const isDisabled = syncing || syncStatus?.is_syncing

  const formatTime = (iso) => {
    if (!iso) return '—'
    try {
      return new Date(iso).toLocaleString()
    } catch {
      return iso
    }
  }

  return (
    <div style={{
      background: 'var(--bg-2)', border: '1px solid var(--border)',
      borderRadius: 'var(--r-lg)', overflow: 'hidden',
    }}>
      {/* ── Header ── */}
      <div style={{
        padding: '12px 16px',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: '1rem' }}>🔄</span>
          <span style={{
            fontFamily: 'var(--font-display)', fontWeight: 700,
            fontSize: '.85rem', color: 'var(--text-0)',
          }}>Cloud Sync</span>
        </div>

        {/* Small refresh icon to re-fetch status without triggering a sync */}
        <button
          onClick={onRefreshStatus}
          title="Refresh sync status"
          style={{
            width: 24, height: 24, borderRadius: 'var(--r-sm)',
            border: '1px solid var(--border)', background: 'transparent',
            cursor: 'pointer', color: 'var(--text-3)', fontSize: '.75rem',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            transition: 'color .15s',
          }}
          onMouseEnter={e => e.currentTarget.style.color = 'var(--text-1)'}
          onMouseLeave={e => e.currentTarget.style.color = 'var(--text-3)'}
        >
          ↻
        </button>
      </div>

      {/* ── Status rows — identical to rag-frontend Sidebar ── */}
      <div style={{ padding: '10px 16px 4px' }}>

        {/* Last synced timestamp */}
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 6, fontSize: '.72rem',
        }}>
          <span style={{ color: 'var(--text-3)' }}>Last synced</span>
          <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-text)', fontSize: '.68rem' }}>
            {syncStatus ? formatTime(syncStatus.last_synced) : '—'}
          </span>
        </div>

        {/* Pending docs badge — shown only when > 0 */}
        {syncStatus?.pending_count > 0 && (
          <div style={{
            fontSize: '.68rem', color: 'var(--warn)',
            background: 'var(--warn-bg)', border: '1px solid var(--warn-border)',
            borderRadius: 'var(--r-sm)', padding: '4px 8px',
            marginBottom: 6, fontFamily: 'var(--font-mono)',
          }}>
            {syncStatus.pending_count} doc{syncStatus.pending_count > 1 ? 's' : ''} pending sync
          </div>
        )}

        {/* "Syncing…" live indicator — shown when backend reports is_syncing */}
        {syncStatus?.is_syncing && (
          <div style={{
            fontSize: '.68rem', color: 'var(--teal)',
            display: 'flex', alignItems: 'center', gap: 6,
            marginBottom: 6,
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: 'var(--teal)',
              animation: 'pulse 1s ease infinite', flexShrink: 0,
            }} />
            Syncing…
          </div>
        )}

        {/* Error message */}
        {syncError && (
          <div style={{
            fontSize: '.68rem', color: 'var(--danger)',
            background: 'var(--danger-bg)', border: '1px solid var(--danger-border)',
            borderRadius: 'var(--r-sm)', padding: '4px 8px',
            marginBottom: 6, fontFamily: 'var(--font-mono)',
          }}>
            {syncError}
          </div>
        )}

        {/* Success message */}
        {syncSuccess && (
          <div style={{
            fontSize: '.68rem', color: 'var(--success)',
            background: 'var(--success-bg)', border: '1px solid var(--success-border)',
            borderRadius: 'var(--r-sm)', padding: '4px 8px',
            marginBottom: 6, fontFamily: 'var(--font-mono)',
            display: 'flex', alignItems: 'center', gap: 5,
          }}>
            <span>✓</span> {syncSuccess}
          </div>
        )}
      </div>

      {/* ── Sync Now button — same disabled logic as rag-frontend ── */}
      <div style={{ padding: '4px 16px 14px' }}>
        <button
          onClick={onSyncNow}
          disabled={isDisabled}
          title={
            syncStatus?.is_syncing
              ? 'Sync already running on the server'
              : syncing
              ? 'Waiting for sync to complete…'
              : 'Pull latest documents from the cloud store'
          }
          style={{
            width: '100%', padding: '9px 0',
            background: isDisabled
              ? 'var(--bg-3)'
              : 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
            border: isDisabled ? '1px solid var(--border-md)' : 'none',
            borderRadius: 'var(--r-md)',
            color: isDisabled ? 'var(--text-3)' : '#fff',
            cursor: isDisabled ? 'not-allowed' : 'pointer',
            fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: '.78rem',
            opacity: isDisabled ? .6 : 1,
            transition: 'all .15s',
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 7,
          }}
        >
          {/* Spinner while syncing, static icon otherwise */}
          {(syncing || syncStatus?.is_syncing) ? (
            <>
              <span style={{
                width: 12, height: 12, borderRadius: '50%',
                border: '2px solid rgba(255,255,255,.3)',
                borderTopColor: isDisabled ? 'var(--text-3)' : '#fff',
                animation: 'spin .7s linear infinite', display: 'inline-block',
              }} />
              Syncing…
            </>
          ) : (
            <>
              <span>⇄</span>
              Sync now
            </>
          )}
        </button>
      </div>
    </div>
  )
}


// ── Logo ──────────────────────────────────────────────────────
function Logo() {
  return (
    <div style={{
      width: 30, height: 30, borderRadius: 8, flexShrink: 0,
      background: 'linear-gradient(135deg, var(--accent) 0%, #5b4dd4 100%)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: '.9rem', color: '#fff',
    }}>✦</div>
  )
}

// ── Spinner ───────────────────────────────────────────────────
function Spinner() {
  return (
    <div style={{
      width: 28, height: 28, borderRadius: '50%',
      border: '2px solid var(--bg-3)',
      borderTopColor: 'var(--accent)',
      animation: 'spin .8s linear infinite',
    }} />
  )
}

// ── Center layout helper ──────────────────────────────────────
const centerStyle = {
  minHeight: '100vh',
  display: 'flex', flexDirection: 'column',
  alignItems: 'center', justifyContent: 'center',
  padding: 24,
}