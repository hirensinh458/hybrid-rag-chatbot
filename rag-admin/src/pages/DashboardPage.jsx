// src/pages/DashboardPage.jsx
//
// Main admin dashboard. Layout: collapsible sidebar + main content area.
//
// Sidebar sections:
//   Overview · Documents · Usage · Join Code · Settings
//
// Overview   — usage meters (vectors + users), UpgradePrompt banners, plan status
// Documents  — IngestPanel at top + DocumentTable below
// Usage      — detailed stats card from GET /admin/usage
// Join Code  — JoinCodeManager component
// Settings   — current plan, API base URL, logout
//
// Data fetching strategy:
//   - usage comes from AuthContext (already fetched on login)
//   - documents are fetched locally and refreshed after ingest / delete
//   - sidebar stays mounted so state persists across section switches

import { useState, useEffect, useCallback } from 'react'
import { useNavigate }   from 'react-router-dom'
import { useAuth }       from '../context/AuthContext'
import {
  adminGetDocuments,
  adminDeleteDocument,
}                        from '../api'

import UsageMeter        from '../components/UsageMeter'
import JoinCodeManager   from '../components/JoinCodeManager'
import DocumentTable     from '../components/DocumentTable'
import IngestPanel       from '../components/IngestPanel'
import StatsPanel        from '../components/StatsPanel'
import UpgradePrompt     from '../components/UpgradePrompt'

// ── Nav items ─────────────────────────────────────────────────────────────────

const NAV = [
  { id: 'overview',   label: 'Overview',  icon: '◈' },
  { id: 'documents',  label: 'Documents', icon: '📄' },
  { id: 'usage',      label: 'Usage',     icon: '📊' },
  { id: 'joincode',   label: 'Join Code', icon: '🔑' },
  { id: 'settings',   label: 'Settings',  icon: '⚙' },
]

// ── Sidebar ───────────────────────────────────────────────────────────────────

function Sidebar({ active, onSelect, tenant, plan, onLogout }) {
  return (
    <aside style={{
      width: 220, flexShrink: 0,
      background: 'var(--bg-1)',
      borderRight: '1px solid var(--border)',
      display: 'flex', flexDirection: 'column',
      minHeight: '100vh',
    }}>
      {/* Logo / brand */}
      <div style={{
        padding: '24px 20px 20px',
        borderBottom: '1px solid var(--border)',
      }}>
        <div style={{
          fontFamily: 'var(--font-display)', fontWeight: 800,
          fontSize: '1.05rem', color: 'var(--text-0)',
          letterSpacing: '-.01em',
        }}>
          MarineDoc
        </div>
        <div style={{
          fontSize: '.65rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', marginTop: 4,
          letterSpacing: '.08em',
        }}>
          ADMIN PORTAL
        </div>
      </div>

      {/* Tenant info */}
      {tenant && (
        <div style={{
          padding: '14px 20px',
          borderBottom: '1px solid var(--border)',
        }}>
          <div style={{
            fontSize: '.75rem', fontWeight: 600,
            color: 'var(--text-1)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            {tenant.display_name || 'My Organisation'}
          </div>
          <div style={{
            fontSize: '.62rem', color: 'var(--text-3)',
            fontFamily: 'var(--font-mono)', marginTop: 3,
          }}>
            {tenant.slug || ''}
          </div>

          {/* Status badge */}
          <StatusBadge status={tenant.status} />
        </div>
      )}

      {/* Nav links */}
      <nav style={{
        flex: 1, padding: '10px 10px',
        display: 'flex', flexDirection: 'column', gap: 2,
      }}>
        {NAV.map(item => (
          <button
            key={item.id}
            onClick={() => onSelect(item.id)}
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '9px 12px',
              borderRadius: 'var(--r-md)',
              border: 'none', cursor: 'pointer',
              background: active === item.id
                ? 'linear-gradient(90deg, var(--accent-glow), transparent)'
                : 'transparent',
              color: active === item.id ? 'var(--accent-text)' : 'var(--text-2)',
              fontFamily: 'var(--font-body)', fontSize: '.8rem',
              fontWeight: active === item.id ? 600 : 400,
              borderLeft: active === item.id
                ? '2px solid var(--accent)'
                : '2px solid transparent',
              transition: 'all .15s',
              textAlign: 'left',
              width: '100%',
            }}
          >
            <span style={{
              fontSize: '.85rem', width: 18, textAlign: 'center', flexShrink: 0,
            }}>
              {item.icon}
            </span>
            {item.label}
          </button>
        ))}
      </nav>

      {/* Plan badge */}
      {plan && (
        <div style={{
          padding: '12px 16px',
          margin: '0 10px 10px',
          background: 'var(--bg-2)', border: '1px solid var(--border)',
          borderRadius: 'var(--r-md)',
        }}>
          <div style={{
            fontSize: '.6rem', color: 'var(--text-3)',
            fontFamily: 'var(--font-mono)', letterSpacing: '.1em',
            textTransform: 'uppercase', marginBottom: 4,
          }}>
            Current plan
          </div>
          <div style={{
            fontSize: '.82rem', fontWeight: 700,
            color: 'var(--accent-text)',
            fontFamily: 'var(--font-display)',
          }}>
            {plan.name}
          </div>
        </div>
      )}

      {/* Logout */}
      <div style={{ padding: '10px 10px 20px' }}>
        <button
          onClick={onLogout}
          style={{
            width: '100%', padding: '8px 12px',
            background: 'none',
            border: '1px solid var(--border)',
            borderRadius: 'var(--r-md)',
            color: 'var(--text-3)',
            fontFamily: 'var(--font-mono)', fontSize: '.72rem',
            cursor: 'pointer',
            transition: 'all .15s',
            display: 'flex', alignItems: 'center', gap: 8,
          }}
          onMouseEnter={e => {
            e.currentTarget.style.borderColor = 'var(--danger-border)'
            e.currentTarget.style.color = 'var(--danger)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.borderColor = 'var(--border)'
            e.currentTarget.style.color = 'var(--text-3)'
          }}
        >
          <span>⏻</span> Sign out
        </button>
      </div>
    </aside>
  )
}

// ── Status badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }) {
  if (!status) return null
  const MAP = {
    trial:       { bg: 'var(--teal-dim)',   color: 'var(--teal)',    label: 'Trial'       },
    active:      { bg: 'var(--success-bg)', color: 'var(--success)', label: 'Active'      },
    over_quota:  { bg: 'var(--warn-bg)',    color: 'var(--warn)',    label: 'Over quota'  },
    suspended:   { bg: 'var(--danger-bg)',  color: 'var(--danger)',  label: 'Suspended'   },
  }
  const s = MAP[status] ?? MAP.active
  return (
    <div style={{
      display: 'inline-block',
      marginTop: 6,
      padding: '2px 8px',
      borderRadius: 12,
      background: s.bg, color: s.color,
      fontSize: '.58rem', fontFamily: 'var(--font-mono)',
      letterSpacing: '.08em', textTransform: 'uppercase',
    }}>
      {s.label}
    </div>
  )
}

// ── Section header ────────────────────────────────────────────────────────────

function SectionHeader({ title, subtitle }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <h1 style={{
        fontFamily: 'var(--font-display)', fontWeight: 800,
        fontSize: '1.3rem', color: 'var(--text-0)',
        letterSpacing: '-.02em', marginBottom: 4,
      }}>
        {title}
      </h1>
      {subtitle && (
        <p style={{ fontSize: '.78rem', color: 'var(--text-2)', lineHeight: 1.55 }}>
          {subtitle}
        </p>
      )}
    </div>
  )
}

// ── Card wrapper ──────────────────────────────────────────────────────────────

function Card({ children, style }) {
  return (
    <div style={{
      background: 'var(--bg-1)', border: '1px solid var(--border-md)',
      borderRadius: 'var(--r-xl)', padding: '24px',
      ...style,
    }}>
      {children}
    </div>
  )
}

// ── Stat row (for Usage section) ──────────────────────────────────────────────

function StatRow({ label, value, sub }) {
  return (
    <div style={{
      display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      padding: '11px 0',
      borderBottom: '1px solid var(--border)',
    }}>
      <span style={{ fontSize: '.78rem', color: 'var(--text-2)' }}>{label}</span>
      <div style={{ textAlign: 'right' }}>
        <div style={{ fontSize: '.85rem', color: 'var(--text-0)', fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
          {value}
        </div>
        {sub && (
          <div style={{ fontSize: '.62rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>
            {sub}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Section: Overview ─────────────────────────────────────────────────────────

function OverviewSection({ usage, plan, onNavigate }) {
  const vectors = usage?.vectors ?? { used: 0, limit: 0, percent: 0 }
  const users   = usage?.users   ?? { used: 0, limit: 0, percent: 0 }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <SectionHeader
        title="Overview"
        subtitle="Your organisation's current usage and plan status."
      />

      {/* Upgrade prompts */}
      {vectors.percent >= 80 && (
        <UpgradePrompt
          resource="vectors"
          pct={vectors.percent}
          planName={plan?.name ?? 'Starter'}
        />
      )}
      {users.percent >= 80 && (
        <UpgradePrompt
          resource="users"
          pct={users.percent}
          planName={plan?.name ?? 'Starter'}
        />
      )}

      {/* Meters */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        <UsageMeter
          used={vectors.used}
          limit={vectors.limit}
          label="Vectors"
          onUpgrade={() => onNavigate('settings')}
        />
        <UsageMeter
          used={users.used}
          limit={users.limit}
          label="Users"
          onUpgrade={() => onNavigate('settings')}
        />
      </div>

      {/* Plan summary card */}
      {usage && (
        <Card>
          <div style={{
            fontSize: '.62rem', color: 'var(--text-3)',
            fontFamily: 'var(--font-mono)', letterSpacing: '.12em',
            textTransform: 'uppercase', marginBottom: 12,
          }}>
            Account
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <StatRow label="Organisation"  value={usage.display_name ?? '—'} />
            <StatRow label="Plan"          value={usage.plan ?? '—'} />
            <StatRow label="Status"        value={<StatusBadge status={usage.status} />} />
            {usage.trial_ends_at && (
              <StatRow
                label="Trial ends"
                value={new Date(usage.trial_ends_at).toLocaleDateString('en-GB', {
                  day: '2-digit', month: 'short', year: 'numeric',
                })}
              />
            )}
          </div>
        </Card>
      )}

      {/* Quick action */}
      <div style={{
        display: 'flex', gap: 10, flexWrap: 'wrap',
      }}>
        <button
          onClick={() => onNavigate('documents')}
          style={quickBtn}
        >
          📄 Upload documents
        </button>
        <button
          onClick={() => onNavigate('joincode')}
          style={quickBtn}
        >
          🔑 View join code
        </button>
      </div>
    </div>
  )
}

// ── Section: Documents ────────────────────────────────────────────────────────

function DocumentsSection({ documents, docsLoading, onIngestSuccess, onDelete, plan }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <SectionHeader
        title="Documents"
        subtitle="Upload PDFs to your knowledge base. Ingested documents are available to all users in your organisation."
      />

      {/* Ingest panel */}
      <Card>
        <div style={{
          fontSize: '.68rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.1em',
          textTransform: 'uppercase', marginBottom: 14,
        }}>
          Upload new documents
        </div>
        <IngestPanel onSuccess={onIngestSuccess} />
      </Card>

      {/* Document table */}
      <div>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 10,
        }}>
          <div style={{
            fontSize: '.68rem', color: 'var(--text-3)',
            fontFamily: 'var(--font-mono)', letterSpacing: '.1em',
            textTransform: 'uppercase',
          }}>
            Ingested documents
          </div>
          {documents.length > 0 && (
            <div style={{
              fontSize: '.62rem', color: 'var(--text-3)',
              fontFamily: 'var(--font-mono)',
            }}>
              {documents.length} total
            </div>
          )}
        </div>
        <DocumentTable
          documents={documents}
          onDelete={onDelete}
          loading={docsLoading}
        />
      </div>
    </div>
  )
}

// ── Section: Usage ────────────────────────────────────────────────────────────

function UsageSection({ usage, plan }) {
  if (!usage) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
        <SectionHeader title="Usage" subtitle="Detailed usage statistics for your organisation." />
        <div style={{
          textAlign: 'center', padding: '40px 20px',
          color: 'var(--text-3)', fontSize: '.78rem',
        }}>
          Loading usage data…
        </div>
      </div>
    )
  }

  const vectors = usage.vectors ?? { used: 0, limit: 0, percent: 0 }
  const users   = usage.users   ?? { used: 0, limit: 0, percent: 0 }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <SectionHeader
        title="Usage"
        subtitle="Detailed resource usage and plan limits for your organisation."
      />

      {/* Meters */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        <UsageMeter
          used={vectors.used}
          limit={vectors.limit}
          label="Vectors (document chunks)"
        />
        <UsageMeter
          used={users.used}
          limit={users.limit}
          label="Users (team members)"
        />
      </div>

      {/* Detail table */}
      <Card>
        <div style={{
          fontSize: '.62rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.12em',
          textTransform: 'uppercase', marginBottom: 12,
        }}>
          Usage breakdown
        </div>

        <StatRow
          label="Vectors used"
          value={`${vectors.used.toLocaleString()} / ${vectors.limit > 0 ? vectors.limit.toLocaleString() : '∞'}`}
          sub={vectors.limit > 0 ? `${vectors.percent.toFixed(1)}% of plan limit` : 'Unlimited'}
        />
        <StatRow
          label="Team members"
          value={`${users.used} / ${users.limit > 0 ? users.limit : '∞'}`}
          sub={users.limit > 0 ? `${users.percent.toFixed(1)}% of plan limit` : 'Unlimited'}
        />
        <StatRow
          label="Plan"
          value={usage.plan ?? '—'}
        />
        <StatRow
          label="Account status"
          value={<StatusBadge status={usage.status} />}
        />
        {usage.last_ingestion && (
          <StatRow
            label="Last ingestion"
            value={new Date(usage.last_ingestion).toLocaleString('en-GB', {
              day: '2-digit', month: 'short', year: 'numeric',
              hour: '2-digit', minute: '2-digit',
            })}
          />
        )}
        {usage.trial_ends_at && (
          <StatRow
            label="Trial ends"
            value={new Date(usage.trial_ends_at).toLocaleDateString('en-GB', {
              day: '2-digit', month: 'short', year: 'numeric',
            })}
          />
        )}
      </Card>

      {/* Upgrade CTA when near limit */}
      {(vectors.percent >= 80 || users.percent >= 80) && (
        <UpgradePrompt
          resource={vectors.percent >= users.percent ? 'vectors' : 'users'}
          pct={Math.max(vectors.percent, users.percent)}
          planName={plan?.name ?? 'Starter'}
        />
      )}
    </div>
  )
}

// ── Section: Join Code ────────────────────────────────────────────────────────

function JoinCodeSection() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <SectionHeader
        title="Join Code"
        subtitle="Your employees enter this code during mobile app signup to join your organisation."
      />
      <JoinCodeManager />
    </div>
  )
}

// ── Section: Settings ─────────────────────────────────────────────────────────

function SettingsSection({ plan, onLogout, onNavigate }) {
  const apiBase = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <SectionHeader
        title="Settings"
        subtitle="Account and API configuration."
      />

      {/* Plan card */}
      <Card>
        <div style={{
          fontSize: '.62rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.12em',
          textTransform: 'uppercase', marginBottom: 14,
        }}>
          Subscription
        </div>

        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 16,
        }}>
          <div>
            <div style={{
              fontFamily: 'var(--font-display)', fontWeight: 800,
              fontSize: '1.1rem', color: 'var(--accent-text)',
            }}>
              {plan?.name ?? 'Starter'} Plan
            </div>
            <div style={{ fontSize: '.72rem', color: 'var(--text-2)', marginTop: 3 }}>
              {plan?.max_vectors > 0
                ? `${plan.max_vectors.toLocaleString()} vectors · ${plan.max_users} users`
                : 'Unlimited'}
            </div>
          </div>
          <a
            href="/plans"
            style={{
              background: 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
              color: '#fff', textDecoration: 'none',
              borderRadius: 'var(--r-md)', padding: '8px 16px',
              fontSize: '.72rem', fontWeight: 700,
              fontFamily: 'var(--font-mono)', letterSpacing: '.06em',
              textTransform: 'uppercase',
            }}
          >
            Upgrade
          </a>
        </div>

        <div style={{
          fontSize: '.7rem', color: 'var(--text-3)', lineHeight: 1.6,
        }}>
          To change your plan, select an option from the plan selection page.
          For Enterprise enquiries, contact us at{' '}
          <a href="mailto:support@example.com" style={{ color: 'var(--accent-text)' }}>
            support@example.com
          </a>.
        </div>
      </Card>

      {/* API config */}
      <Card>
        <div style={{
          fontSize: '.62rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.12em',
          textTransform: 'uppercase', marginBottom: 14,
        }}>
          API Configuration
        </div>

        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{
            fontSize: '.68rem', color: 'var(--text-2)',
            fontFamily: 'var(--font-mono)',
          }}>
            Backend base URL
          </span>
          <div style={{
            background: 'var(--bg-3)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--r-md)', padding: '9px 13px',
            fontFamily: 'var(--font-mono)', fontSize: '.78rem',
            color: 'var(--text-1)',
            userSelect: 'all', cursor: 'text',
          }}>
            {apiBase}
          </div>
          <span style={{ fontSize: '.62rem', color: 'var(--text-3)', fontFamily: 'var(--font-mono)' }}>
            Set via <code>VITE_API_BASE</code> environment variable.
          </span>
        </label>
      </Card>

      {/* Danger zone */}
      <Card style={{ border: '1px solid var(--danger-border)' }}>
        <div style={{
          fontSize: '.62rem', color: 'var(--danger)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.12em',
          textTransform: 'uppercase', marginBottom: 14,
        }}>
          Danger Zone
        </div>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <div>
            <div style={{ fontSize: '.8rem', color: 'var(--text-1)', marginBottom: 3 }}>
              Sign out of admin portal
            </div>
            <div style={{ fontSize: '.7rem', color: 'var(--text-3)' }}>
              You will need to sign in again.
            </div>
          </div>
          <button
            onClick={onLogout}
            style={{
              padding: '8px 16px',
              background: 'var(--danger-bg)',
              border: '1px solid var(--danger-border)',
              borderRadius: 'var(--r-md)',
              color: 'var(--danger)',
              fontFamily: 'var(--font-mono)', fontSize: '.72rem',
              cursor: 'pointer', fontWeight: 600,
            }}
          >
            Sign out
          </button>
        </div>
      </Card>
    </div>
  )
}

// ── Quick button style ────────────────────────────────────────────────────────

const quickBtn = {
  background: 'var(--bg-2)',
  border: '1px solid var(--border-md)',
  borderRadius: 'var(--r-md)',
  color: 'var(--text-1)',
  fontFamily: 'var(--font-body)',
  fontSize: '.78rem',
  padding: '9px 16px',
  cursor: 'pointer',
  display: 'flex', alignItems: 'center', gap: 8,
  transition: 'border-color .15s',
}

// ── DashboardPage ─────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const navigate = useNavigate()
  const { usage, plan, tenant, logout, refreshUsage } = useAuth()

  const [section,     setSection]     = useState('overview')
  const [documents,   setDocuments]   = useState([])
  const [docsLoading, setDocsLoading] = useState(false)
  const [docsError,   setDocsError]   = useState(null)

  // ── Fetch document list ──────────────────────────────────────────────────
  const fetchDocuments = useCallback(async () => {
    setDocsLoading(true)
    setDocsError(null)
    try {
      const data = await adminGetDocuments()
      setDocuments(data?.documents ?? [])
    } catch (err) {
      setDocsError(err.message)
    } finally {
      setDocsLoading(false)
    }
  }, [])

  // Fetch on mount and whenever the user switches to Documents tab
  useEffect(() => {
    fetchDocuments()
  }, [fetchDocuments])

  // ── Delete handler ────────────────────────────────────────────────────────
  const handleDelete = useCallback(async (id) => {
    await adminDeleteDocument(id)
    setDocuments(prev => prev.filter(d => d.id !== id))
    await refreshUsage()
  }, [refreshUsage])

  // ── Ingest success handler ────────────────────────────────────────────────
  const handleIngestSuccess = useCallback(async () => {
    await fetchDocuments()
    // refreshUsage is called by IngestPanel internally, but call again to be safe
    await refreshUsage()
  }, [fetchDocuments, refreshUsage])

  // ── Logout ────────────────────────────────────────────────────────────────
  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  // ── Render active section ─────────────────────────────────────────────────
  function renderSection() {
    switch (section) {
      case 'overview':
        return (
          <OverviewSection
            usage={usage}
            plan={plan}
            onNavigate={setSection}
          />
        )
      case 'documents':
        return (
          <DocumentsSection
            documents={documents}
            docsLoading={docsLoading}
            onIngestSuccess={handleIngestSuccess}
            onDelete={handleDelete}
            plan={plan}
          />
        )
      case 'usage':
        return <UsageSection usage={usage} plan={plan} />
      case 'joincode':
        return <JoinCodeSection />
      case 'settings':
        return (
          <SettingsSection
            plan={plan}
            onLogout={handleLogout}
            onNavigate={setSection}
          />
        )
      default:
        return null
    }
  }

  return (
    <div style={{
      display: 'flex', minHeight: '100vh',
      background: 'var(--bg-0)',
    }}>
      {/* Sidebar */}
      <Sidebar
        active={section}
        onSelect={setSection}
        tenant={tenant}
        plan={plan}
        onLogout={handleLogout}
      />

      {/* Main */}
      <main style={{
        flex: 1, overflowY: 'auto',
        padding: '40px 48px',
        maxWidth: 900,
      }}>
        {/* Docs error banner */}
        {docsError && section === 'documents' && (
          <div style={{
            marginBottom: 16,
            background: 'var(--danger-bg)', border: '1px solid var(--danger-border)',
            borderRadius: 'var(--r-md)', padding: '10px 14px',
            fontSize: '.75rem', color: 'var(--danger)',
            fontFamily: 'var(--font-mono)',
          }}>
            Failed to load documents: {docsError}
            <button
              onClick={fetchDocuments}
              style={{
                marginLeft: 12, background: 'none', border: 'none',
                color: 'var(--danger)', cursor: 'pointer',
                fontSize: '.7rem', textDecoration: 'underline',
              }}
            >Retry</button>
          </div>
        )}

        {/* Active section content */}
        <div key={section} style={{ animation: 'fadeUp .18s var(--ease)' }}>
          {renderSection()}
        </div>
      </main>
    </div>
  )
}