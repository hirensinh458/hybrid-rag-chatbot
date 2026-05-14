// src/components/UsageMeter.jsx
//
// Labelled progress bar for a resource (vectors / users).
// Colour: accent → amber at ≥ 80% → red at ≥ 100%.
//
// Props:
//   used       number   current usage
//   limit      number   plan limit (0 = unlimited)
//   label      string   e.g. "Vectors"
//   onUpgrade  func?    optional — called when "Upgrade plan" button is clicked

export default function UsageMeter({ used = 0, limit = 0, label = 'Resource', onUpgrade }) {
  const unlimited = limit === 0
  const raw       = unlimited ? 0 : (used / limit) * 100
  const pct       = Math.min(raw, 100)

  const barColor =
    pct >= 100 ? 'var(--danger)' :
    pct >= 80  ? 'var(--warn)'   :
                 'var(--accent)'

  const bgColor =
    pct >= 100 ? 'var(--danger-bg)' :
    pct >= 80  ? 'var(--warn-bg)'   :
                 'var(--bg-3)'

  const borderColor =
    pct >= 100 ? 'var(--danger-border)' :
    pct >= 80  ? 'var(--warn-border)'   :
                 'var(--border)'

  function fmtNum(n) {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000)     return `${(n / 1_000).toFixed(1)}k`
    return String(n)
  }

  return (
    <div style={{
      background: bgColor,
      border: `1px solid ${borderColor}`,
      borderRadius: 'var(--r-md)',
      padding: '14px 16px',
      transition: 'background .2s, border-color .2s',
    }}>
      {/* Label row */}
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        alignItems: 'center', marginBottom: 10,
      }}>
        <span style={{
          fontSize: '.68rem', fontFamily: 'var(--font-mono)',
          letterSpacing: '.1em', textTransform: 'uppercase',
          color: 'var(--text-2)',
        }}>
          {label}
        </span>
        <span style={{
          fontSize: '.68rem', fontFamily: 'var(--font-mono)',
          color: pct >= 80 ? barColor : 'var(--text-2)',
        }}>
          {unlimited
            ? `${fmtNum(used)} / ∞`
            : `${fmtNum(used)} / ${fmtNum(limit)}`}
        </span>
      </div>

      {/* Bar track */}
      {!unlimited && (
        <div style={{
          height: 6, borderRadius: 3,
          background: 'var(--bg-4)', overflow: 'hidden',
        }}>
          <div style={{
            height: '100%',
            width: `${pct}%`,
            background: barColor,
            borderRadius: 3,
            transition: 'width .4s var(--ease)',
          }} />
        </div>
      )}

      {/* Pct text when over 50% */}
      {!unlimited && pct > 50 && (
        <div style={{
          marginTop: 6,
          fontSize: '.62rem', fontFamily: 'var(--font-mono)',
          color: pct >= 80 ? barColor : 'var(--text-3)',
          textAlign: 'right',
        }}>
          {pct.toFixed(0)}% used
        </div>
      )}

      {/* Upgrade CTA at 100% */}
      {pct >= 100 && onUpgrade && (
        <button
          onClick={onUpgrade}
          style={{
            marginTop: 10,
            background: 'none',
            border: `1px solid ${barColor}`,
            borderRadius: 'var(--r-sm)',
            color: barColor,
            fontFamily: 'var(--font-mono)', fontWeight: 600,
            fontSize: '.65rem', letterSpacing: '.08em',
            padding: '4px 12px', cursor: 'pointer',
            textTransform: 'uppercase',
            transition: 'background .1s',
          }}
        >
          ↑ Upgrade plan
        </button>
      )}
    </div>
  )
}