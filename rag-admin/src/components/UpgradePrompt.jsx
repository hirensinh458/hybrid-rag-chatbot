// src/components/UpgradePrompt.jsx
//
// Dismissible banner shown when a resource is approaching or at its plan limit.
// Renders nothing when pct < 80.
//
// Props:
//   resource   string   "vectors" | "users"
//   pct        number   0-100 (can exceed 100 if over limit)
//   planName   string   e.g. "Starter"
//   onDismiss  func?    optional dismiss handler

export default function UpgradePrompt({ resource, pct = 0, planName = 'Starter', onDismiss }) {
  if (pct < 80) return null

  const isOver = pct >= 100

  return (
    <div style={{
      background: isOver ? 'var(--danger-bg)' : 'var(--warn-bg)',
      border: `1px solid ${isOver ? 'var(--danger-border)' : 'var(--warn-border)'}`,
      borderRadius: 'var(--r-md)',
      padding: '12px 16px',
      display: 'flex', alignItems: 'flex-start', gap: 12,
      animation: 'fadeUp .2s var(--ease)',
    }}>
      {/* Icon */}
      <span style={{ fontSize: '1rem', flexShrink: 0, marginTop: 1 }}>
        {isOver ? '🚨' : '⚠️'}
      </span>

      {/* Text */}
      <div style={{ flex: 1 }}>
        <div style={{
          fontSize: '.78rem', fontWeight: 600,
          color: isOver ? 'var(--danger)' : 'var(--warn)',
          marginBottom: 3,
        }}>
          {isOver
            ? `${capitalise(resource)} limit reached on ${planName}`
            : `Approaching ${resource} limit on ${planName}`}
        </div>
        <div style={{ fontSize: '.72rem', color: 'var(--text-2)', lineHeight: 1.55 }}>
          {isOver
            ? `New documents cannot be added. Upgrade your plan to continue ingesting content.`
            : `You've used ${pct.toFixed(0)}% of your ${resource} quota. Consider upgrading soon.`}
        </div>
      </div>

      {/* Upgrade link */}
      <a
        href="/plans"
        style={{
          flexShrink: 0,
          background: isOver ? 'var(--danger)' : 'var(--warn)',
          color: '#fff', textDecoration: 'none',
          borderRadius: 'var(--r-sm)', padding: '5px 12px',
          fontSize: '.68rem', fontWeight: 700,
          fontFamily: 'var(--font-mono)', letterSpacing: '.06em',
          textTransform: 'uppercase', whiteSpace: 'nowrap',
          alignSelf: 'center',
        }}
      >
        Upgrade
      </a>

      {/* Dismiss */}
      {onDismiss && (
        <button
          onClick={onDismiss}
          aria-label="Dismiss"
          style={{
            flexShrink: 0, background: 'none', border: 'none',
            cursor: 'pointer', color: 'var(--text-3)',
            fontSize: '1.1rem', lineHeight: 1, padding: 0,
            alignSelf: 'flex-start',
          }}
        >
          ×
        </button>
      )}
    </div>
  )
}

function capitalise(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s
}