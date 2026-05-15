// pages/DashboardPage.jsx
import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listTenants, getAlerts, getActivity, listPlans } from '../api/superAdmin'
import { StatusBadge, PlanBadge, UsageMeter, Spinner, RelTime, SectionHeader } from '../components/Shared'
import { useAuth } from '../context/AuthContext'

function StatCard({ label, value, sub, accent }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={accent ? {color: 'var(--accent)'} : {}}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

export default function DashboardPage() {
  const navigate = useNavigate()
  const { session } = useAuth()
  const [tenants,  setTenants]  = useState([])
  const [alerts,   setAlerts]   = useState([])
  const [activity, setActivity] = useState([])
  const [loading,  setLoading]  = useState(true)

  useEffect(() => {
    if (!session) return

    async function load() {
      try {
        const [t, a, act] = await Promise.all([
          listTenants({ pageSize: 100 }),
          getAlerts({ pageSize: 10 }),
          getActivity({ pageSize: 8 }),
        ])
        setTenants(t.items || [])
        setAlerts(a.items || [])
        setActivity(act.items || [])
      } catch {}
      setLoading(false)
    }
    load()
  }, [session])

  if (loading) return <Spinner text="Loading dashboard…" />

  // Compute stats
  const total       = tenants.length
  const active      = tenants.filter(t => t.status === 'active').length
  const trial       = tenants.filter(t => t.status === 'trial').length
  const overQuota   = tenants.filter(t => t.status === 'over_quota').length
  const suspended   = tenants.filter(t => t.status === 'suspended').length
  const totalVectors = tenants.reduce((s, t) => s + (t.tenant_usage?.vector_count || 0), 0)

  // Recently active tenants (by created_at desc, last 5)
  const recent = [...tenants].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 6)

  const ACTION_COLOR = {
    tenant_updated:    'info',
    tenant_reconciled: 'success',
    bulk_suspend:      'danger',
    bulk_plan_change:  'warning',
    plan_created:      'success',
    plan_updated:      'warning',
    member_removed:    'danger',
  }

  return (
    <div className="page-enter">
      <SectionHeader
        title="Dashboard"
        sub="Platform overview across all tenants"
      />

      {/* Stats grid */}
      <div className="stats-grid">
        <StatCard label="Total Tenants"   value={total}        sub={`${active} active`} />
        <StatCard label="Trial Tenants"   value={trial}        sub="14-day trial"       />
        <StatCard label="Over Quota"      value={overQuota}    sub="Need attention"     accent={overQuota > 0} />
        <StatCard label="Total Vectors"   value={totalVectors >= 1e6 ? `${(totalVectors/1e6).toFixed(1)}M` : totalVectors >= 1000 ? `${(totalVectors/1000).toFixed(0)}k` : totalVectors}
                                          sub="across all tenants" />
      </div>

      <div className="two-col" style={{gap: 20}}>

        {/* Status breakdown */}
        <div className="card mb-20">
          <div className="card-header">
            <span className="card-title">Tenant Status Breakdown</span>
          </div>
          <div className="card-body">
            {[
              { label: 'Active',      count: active,    cls: 'active'    },
              { label: 'Trial',       count: trial,     cls: 'trial'     },
              { label: 'Over Quota',  count: overQuota, cls: 'over_quota'},
              { label: 'Suspended',   count: suspended, cls: 'suspended' },
            ].map(row => (
              <div key={row.label} style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding:'8px 0', borderBottom:'1px solid var(--border)' }}>
                <StatusBadge status={row.cls} />
                <span className="mono-sm" style={{color:'var(--text-primary)'}}>{row.count}</span>
              </div>
            ))}

            {suspended > 0 && (
              <div style={{ marginTop: 16 }}>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={() => navigate('/tenants?status=suspended')}
                  style={{ width: '100%' }}
                >View Suspended Tenants →</button>
              </div>
            )}
          </div>
        </div>

        {/* Unread alerts */}
        <div className="card mb-20">
          <div className="card-header" style={{ justifyContent: 'space-between' }}>
            <span className="card-title">Recent Alerts</span>
            {alerts.length > 0 && (
              <button className="btn btn-ghost btn-sm" onClick={() => navigate('/alerts')}>
                View all
              </button>
            )}
          </div>
          <div className="card-body-flush">
            {alerts.length === 0 ? (
              <div className="empty-state" style={{padding:32}}>
                <div style={{fontSize:24, marginBottom:8}}>✓</div>
                <div style={{color:'var(--green)', fontSize:13}}>No unread alerts</div>
              </div>
            ) : alerts.slice(0,5).map(a => (
              <div key={a.id} className="alert-item unread">
                <div style={{
                  width: 6, height: 6, borderRadius: '50%',
                  background: a.type?.includes('quota') ? 'var(--amber)' : 'var(--red)',
                  marginTop: 5, flexShrink: 0
                }}/>
                <div style={{flex:1, minWidth:0}}>
                  <div style={{fontSize:12.5, color:'var(--text-primary)', marginBottom:2}}>{a.message}</div>
                  <RelTime iso={a.created_at} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Recent tenants */}
      <div className="card mb-20">
        <div className="card-header" style={{justifyContent:'space-between'}}>
          <span className="card-title">Recently Joined Tenants</span>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/tenants')}>View all</button>
        </div>
        <div className="card-body-flush table-wrap">
          <table>
            <thead>
              <tr>
                <th>Tenant</th>
                <th>Plan</th>
                <th>Status</th>
                <th>Vectors</th>
                <th>Users</th>
                <th>Joined</th>
              </tr>
            </thead>
            <tbody>
              {recent.map(t => {
                const plan  = t.plans || {}
                const usage = t.tenant_usage || {}
                return (
                  <tr key={t.id} onClick={() => navigate(`/tenants/${t.id}`)}>
                    <td className="td-primary">
                      {t.display_name}
                      <div className="mono-xs text-muted">{t.slug}</div>
                    </td>
                    <td><PlanBadge name={plan.name} /></td>
                    <td><StatusBadge status={t.status} /></td>
                    <td className="mono-sm">{(usage.vector_count || 0).toLocaleString()}</td>
                    <td className="mono-sm">{usage.user_count || 0}</td>
                    <td><RelTime iso={t.created_at} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Activity feed preview */}
      <div className="card">
        <div className="card-header" style={{justifyContent:'space-between'}}>
          <span className="card-title">Recent Activity</span>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate('/activity')}>Full feed</button>
        </div>
        <div className="card-body">
          {activity.length === 0
            ? <div className="text-muted text-sm">No recent activity.</div>
            : activity.map(a => (
              <div key={a.id} className="activity-item">
                <div className={`activity-dot ${ACTION_COLOR[a.action] || ''}`} />
                <div className="activity-content">
                  <div className="activity-action">
                    <span className="mono-xs" style={{color:'var(--accent)', marginRight:8}}>{a.action}</span>
                    <span style={{fontSize:12, color:'var(--text-secondary)'}}>{a.actor_email}</span>
                  </div>
                  <div className="activity-meta">
                    <RelTime iso={a.created_at} />
                  </div>
                </div>
              </div>
            ))
          }
        </div>
      </div>
    </div>
  )
}