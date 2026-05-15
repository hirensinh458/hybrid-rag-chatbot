// src/pages/PlanSelectionPage.jsx
// Shown after email verification.  Displays three plan cards (Starter / Growth
// / Enterprise).  Selecting Starter transitions immediately to onboarding.
// Growth / Enterprise shows a "contact us" message — payment not yet integrated.

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SubmitButton } from './LoginPage'

const PLANS = [
  {
    id: 'starter',
    name: 'Starter',
    price: 'Free',
    badge: null,
    color: 'var(--teal)',
    features: [
      '5 users',
      '10,000 vectors',
      '3 PDFs per batch',
      'Online mode only',
    ],
    cta: 'Start for free',
    available: true,
  },
  {
    id: 'growth',
    name: 'Growth',
    price: '$99 / mo',
    badge: 'Popular',
    color: 'var(--accent)',
    features: [
      '50 users',
      '200,000 vectors',
      '20 PDFs per batch',
      'Online · Offline · Hybrid',
    ],
    cta: 'Contact sales',
    available: false,
  },
  {
    id: 'enterprise',
    name: 'Enterprise',
    price: '$499 / mo',
    badge: null,
    color: '#f59e0b',
    features: [
      'Unlimited users',
      'Unlimited vectors',
      'Unlimited batch size',
      'All modes + SLA',
    ],
    cta: 'Contact sales',
    available: false,
  },
]

export default function PlanSelectionPage() {
  const navigate = useNavigate()
  const [selecting, setSelecting] = useState(null)
  const [contacted, setContacted] = useState(null)

  const handleSelect = async (plan) => {
    if (!plan.available) {
      setContacted(plan.id)
      return
    }
    setSelecting(plan.id)
    // For Starter: backend will have set plan on tenant creation.
    // Just navigate to onboarding.
    setTimeout(() => navigate('/login', { replace: true }), 400)
  }

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center',
      padding: '40px 24px',
    }}>
      {/* Header */}
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <h1 style={{
          fontFamily: 'var(--font-display)', fontWeight: 800,
          fontSize: '1.8rem', color: 'var(--text-0)', marginBottom: 8,
        }}>
          Choose your plan
        </h1>
        <p style={{ fontSize: '.9rem', color: 'var(--text-2)', maxWidth: 440 }}>
          You can upgrade at any time. Start with Starter for free — no credit card required.
        </p>
      </div>

      {/* Plan cards */}
      <div style={{
        display: 'flex', gap: 20, flexWrap: 'wrap', justifyContent: 'center',
        maxWidth: 960, width: '100%',
      }}>
        {PLANS.map(plan => (
          <PlanCard
            key={plan.id}
            plan={plan}
            selecting={selecting === plan.id}
            contacted={contacted === plan.id}
            onSelect={() => handleSelect(plan)}
          />
        ))}
      </div>

      {/* Footer note */}
      <p style={{ marginTop: 32, fontSize: '.75rem', color: 'var(--text-3)', textAlign: 'center' }}>
        All plans include the hybrid RAG pipeline, Supabase storage, and mobile sync.
      </p>
    </div>
  )
}

function PlanCard({ plan, selecting, contacted, onSelect }) {
  const isPopular = plan.badge === 'Popular'

  return (
    <div style={{
      flex: '1 1 260px', maxWidth: 300,
      background: isPopular ? 'var(--bg-1)' : 'var(--bg-2)',
      border: isPopular ? '1px solid rgba(124,106,247,.4)' : '1px solid var(--border)',
      borderRadius: 'var(--r-xl)', padding: '28px 24px',
      position: 'relative',
      boxShadow: isPopular ? '0 0 0 1px rgba(124,106,247,.2)' : 'none',
      transition: 'transform .15s, box-shadow .15s',
    }}
      onMouseEnter={e => { e.currentTarget.style.transform = 'translateY(-2px)' }}
      onMouseLeave={e => { e.currentTarget.style.transform = 'translateY(0)' }}
    >
      {/* Popular badge */}
      {plan.badge && (
        <div style={{
          position: 'absolute', top: -12, left: '50%', transform: 'translateX(-50%)',
          background: 'var(--accent)', color: '#fff', borderRadius: 20,
          padding: '3px 14px', fontSize: '.65rem', fontWeight: 700,
          fontFamily: 'var(--font-mono)', letterSpacing: '.1em',
        }}>
          {plan.badge}
        </div>
      )}

      {/* Plan name + price */}
      <div style={{ marginBottom: 20 }}>
        <div style={{
          fontSize: '.7rem', fontFamily: 'var(--font-mono)', letterSpacing: '.12em',
          textTransform: 'uppercase', color: plan.color, marginBottom: 6,
        }}>
          {plan.name}
        </div>
        <div style={{
          fontFamily: 'var(--font-display)', fontWeight: 800,
          fontSize: '1.8rem', color: 'var(--text-0)',
        }}>
          {plan.price}
        </div>
      </div>

      {/* Features */}
      <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 24 }}>
        {plan.features.map(f => (
          <li key={f} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            fontSize: '.8rem', color: 'var(--text-1)',
          }}>
            <span style={{ color: plan.color, fontSize: '.9rem', flexShrink: 0 }}>✓</span>
            {f}
          </li>
        ))}
      </ul>

      {/* Contact notice */}
      {contacted && (
        <div style={{
          background: 'var(--accent-glow)', border: '1px solid rgba(124,106,247,.3)',
          borderRadius: 'var(--r-md)', padding: '10px 12px',
          fontSize: '.75rem', color: 'var(--accent-text)',
          fontFamily: 'var(--font-mono)', marginBottom: 12,
        }}>
          Email sales@docmind.io — we'll get back within 24h.
        </div>
      )}

      <SubmitButton
        type="button"
        loading={selecting}
        onClick={onSelect}
        style={{ width: '100%' }}
      >
        {plan.cta}
      </SubmitButton>
    </div>
  )
}