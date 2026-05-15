// pages/TenantDetailPage.jsx
import React, { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  getTenant, patchTenant, reconcileTenant, impersonateTenant,
  deleteTenantDocument, listMembers, removeMember, promoteMember,
  listPlans,
} from '../api/superAdmin'
import {
  StatusBadge, PlanBadge, UsageMeter, Spinner, RelTime,
  EmptyState, ConfirmModal, JsonEditor, SectionHeader,
} from '../components/Shared'
import { useToast } from '../context/ToastContext'

const TABS = ['Overview', 'Usage', 'Config', 'Documents', 'Members', 'Audit']

export default function TenantDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { addToast } = useToast()

  const [tenant, setTenant] = useState(null)
  const [members, setMembers] = useState([])
  const [plans, setPlans] = useState([])
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState(0)

  // Impersonation
  const [impersonating, setImpersonating] = useState(false)
  const [snapshot, setSnapshot] = useState(null)

  // Edit state
  const [editName, setEditName] = useState('')
  const [editStatus, setEditStatus] = useState('')
  const [editPlan, setEditPlan] = useState('')
  const [editTrial, setEditTrial] = useState('')
  const [saving, setSaving] = useState(false)

  // Config tab
  const [configJson, setConfigJson] = useState('')
  const [configDirty, setConfigDirty] = useState(false)
  const [configSaving, setConfigSaving] = useState(false)

  // Confirm modal
  const [confirm, setConfirm] = useState(null)

  useEffect(() => {
    loadAll()
    listPlans().then(p => setPlans(p || [])).catch(() => { })
  }, [id])

  async function loadAll() {
    setLoading(true)
    try {
      const t = await getTenant(id)
      setTenant(t)
      setEditName(t.display_name || '')
      setEditStatus(t.status || '')
      setEditPlan(t.plan_id || '')
      setEditTrial(t.trial_ends_at ? t.trial_ends_at.slice(0, 10) : '')
      setConfigJson(JSON.stringify(t.config_overrides || {}, null, 2))
      setMembers(t.members || [])
    } catch (e) {
      addToast(e.message, 'error')
    }
    setLoading(false)
  }

  async function saveOverview() {
    setSaving(true)
    try {
      await patchTenant(id, {
        display_name: editName,
        status: editStatus,
        plan_id: editPlan,
        trial_ends_at: editTrial || null,
      })
      addToast('Tenant updated.', 'success')
      loadAll()
    } catch (e) {
      addToast(e.message, 'error')
    }
    setSaving(false)
  }

  async function saveConfig() {
    let parsed
    try { parsed = JSON.parse(configJson) }
    catch { addToast('Invalid JSON — fix before saving.', 'error'); return }
    setConfigSaving(true)
    try {
      await patchTenant(id, { config_overrides: parsed })
      addToast('Config saved.', 'success')
      setConfigDirty(false)
    } catch (e) {
      addToast(e.message, 'error')
    }
    setConfigSaving(false)
  }

  async function doReconcile() {
    try {
      const r = await reconcileTenant(id)
      addToast(
        r.corrected
          ? `Reconciled — corrected from ${r.stored_count} → ${r.real_count} (${r.drift_pct}% drift).`
          : `No drift detected. Real count: ${r.real_count}.`,
        r.corrected ? 'warning' : 'success'
      )
    } catch (e) {
      addToast(e.message, 'error')
    }
  }

  async function doImpersonate() {
    setImpersonating(true)
    try {
      const snap = await impersonateTenant(id)
      setSnapshot(snap)
    } catch (e) {
      addToast(e.message, 'error')
    } finally {
      setImpersonating(false)
    }
  }

  async function doDeleteDoc(docId, filename) {
    try {
      const r = await deleteTenantDocument(id, docId)
      addToast(`Deleted "${filename}" — freed ${r.vectors_freed} vectors.`, 'success')
      loadAll()
    } catch (e) {
      addToast(e.message, 'error')
    }
  }

  async function doRemoveMember(userId, email) {
    try {
      await removeMember(id, userId)
      addToast(`Removed ${email} from tenant.`, 'success')
      loadAll()
    } catch (e) {
      addToast(e.message, 'error')
    }
  }

  async function doPromoteMember(userId, role) {
    try {
      await promoteMember(id, userId, role)
      addToast(`Role updated to ${role}.`, 'success')
      loadAll()
    } catch (e) {
      addToast(e.message, 'error')
    }
  }

  if (loading) return <Spinner text="Loading tenant…" />
  if (!tenant) return <EmptyState icon="◧" title="Tenant not found" body="This tenant may have been deleted." />

  const plan = tenant.plans || {}
  const usage = tenant.tenant_usage || {}
  const docs = tenant.documents || []

  const vectorPct = plan.max_vectors > 0 ? (usage.vector_count / plan.max_vectors) * 100 : 0
  const userPct = plan.max_users > 0 ? (usage.user_count / plan.max_users) * 100 : 0

  return (
    <div className="page-enter">
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, marginBottom: 24 }}>
        <button className="btn btn-ghost btn-sm" onClick={() => navigate('/tenants')}>← Back</button>
        <div style={{ flex: 1 }}>
          <SectionHeader
            title={tenant.display_name}
            sub={<span className="mono-xs" style={{ color: 'var(--accent)' }}>{tenant.slug}</span>}
          >
            <StatusBadge status={tenant.status} />
            <PlanBadge name={plan.name} />
          </SectionHeader>
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          <button className="btn btn-secondary btn-sm" onClick={doReconcile}>⟳ Reconcile</button>
          <button className="btn btn-secondary btn-sm" onClick={doImpersonate} disabled={impersonating}>
            {impersonating ? '⊙ Loading…' : '⊙ Impersonate'}
          </button>
        </div>
      </div>

      {/* Impersonation panel */}
      {snapshot && (
        <div style={{ marginBottom: 20, background: 'var(--bg-surface)', border: '1px solid rgba(251,191,36,0.3)', borderRadius: 'var(--r-lg)', overflow: 'hidden' }}>
          <div className="impersonation-banner" style={{ borderRadius: 0, margin: 0 }}>
            <span>⚠</span>
            <strong>IMPERSONATION VIEW — READ ONLY</strong>
            <span style={{ marginLeft: 'auto', color: 'var(--text-secondary)', fontSize: 12 }}>Every load is audit-logged</span>
            <button className="btn btn-ghost btn-xs" onClick={() => { setSnapshot(null); setImpersonating(false) }}>Dismiss</button>
          </div>
          <div style={{ padding: 20, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 16 }}>
            <div>
              <div className="label">Status</div>
              <StatusBadge status={snapshot.status} />
            </div>
            <div>
              <div className="label">Join Code</div>
              <span className="mono-sm" style={{ color: 'var(--accent)', letterSpacing: '0.08em' }}>{snapshot.join_code}</span>
            </div>
            <div>
              <div className="label">Documents</div>
              <span className="mono-sm">{snapshot.documents?.length || 0}</span>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="tabs">
        {TABS.map((t, i) => (
          <button
            key={t}
            className={`tab ${tab === i ? 'active' : ''}`}
            onClick={() => setTab(i)}
          >
            {t}
            {t === 'Documents' && docs.length > 0 && (
              <span className="chip" style={{ marginLeft: 6 }}>{docs.length}</span>
            )}
            {t === 'Members' && members.length > 0 && (
              <span className="chip" style={{ marginLeft: 6 }}>{members.length}</span>
            )}
          </button>
        ))}
      </div>

      {/* ── Tab 0: Overview ─────────────────────────────────── */}
      {tab === 0 && (
        <div className="two-col">
          <div>
            <div className="card mb-20">
              <div className="card-header"><span className="card-title">Identity &amp; Config</span></div>
              <div className="card-body">
                <div className="form-field">
                  <label className="label">Display Name</label>
                  <input className="input" value={editName} onChange={e => setEditName(e.target.value)} />
                </div>
                <div className="form-field">
                  <label className="label">Slug (immutable)</label>
                  <input className="input input-mono" value={tenant.slug} readOnly style={{ opacity: 0.5 }} />
                </div>
                <div className="form-field">
                  <label className="label">Join Code</label>
                  <input className="input input-mono" value={tenant.join_code || ''} readOnly style={{ letterSpacing: '0.1em', color: 'var(--accent)' }} />
                </div>
                <div className="two-col" style={{ gap: 12 }}>
                  <div className="form-field">
                    <label className="label">Status</label>
                    <select className="input" value={editStatus} onChange={e => setEditStatus(e.target.value)}>
                      <option value="trial">Trial</option>
                      <option value="active">Active</option>
                      <option value="over_quota">Over Quota</option>
                      <option value="suspended">Suspended</option>
                    </select>
                  </div>
                  <div className="form-field">
                    <label className="label">Plan</label>
                    <select className="input" value={editPlan} onChange={e => setEditPlan(e.target.value)}>
                      {plans.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                    </select>
                  </div>
                </div>
                <div className="form-field">
                  <label className="label">Trial Ends At</label>
                  <input type="date" className="input input-mono" value={editTrial} onChange={e => setEditTrial(e.target.value)} />
                </div>
                <button
                  className="btn btn-primary"
                  onClick={saveOverview}
                  disabled={saving}
                  style={{ width: '100%', marginTop: 4 }}
                >
                  {saving ? 'Saving…' : 'Save Changes'}
                </button>
              </div>
            </div>

            {/* Quick actions */}
            <div className="card">
              <div className="card-header"><span className="card-title">Quick Actions</span></div>
              <div className="card-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <button
                  className="btn btn-secondary"
                  onClick={() => setConfirm({ type: 'suspend', label: 'Suspend Tenant', msg: `Suspend ${tenant.display_name}? Users will lose access immediately.`, danger: true, action: () => patchTenant(id, { status: 'suspended' }).then(() => { addToast('Tenant suspended.', 'success'); loadAll() }) })}
                  disabled={tenant.status === 'suspended'}
                >Suspend Tenant</button>
                <button
                  className="btn btn-secondary"
                  onClick={() => setConfirm({ type: 'reactivate', label: 'Reactivate Tenant', msg: `Reactivate ${tenant.display_name}?`, action: () => patchTenant(id, { status: 'active' }).then(() => { addToast('Tenant reactivated.', 'success'); loadAll() }) })}
                  disabled={tenant.status === 'active'}
                >Reactivate Tenant</button>
                <button className="btn btn-secondary" onClick={doReconcile}>Force Reconciliation</button>
              </div>
            </div>
          </div>

          {/* Right col — info */}
          <div>
            <div className="card mb-20">
              <div className="card-header"><span className="card-title">Account Info</span></div>
              <div className="card-body">
                {[
                  ['Created', <RelTime iso={tenant.created_at} />],
                  ['Plan', <PlanBadge name={plan.name} />],
                  ['Status', <StatusBadge status={tenant.status} />],
                  ['Vectors', <span className="mono-sm">{(usage.vector_count || 0).toLocaleString()}</span>],
                  ['Users', <span className="mono-sm">{usage.user_count || 0}</span>],
                  ['Last Ingest', <RelTime iso={usage.last_ingestion} />],
                ].map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                    <span className="text-muted">{k}</span>
                    {v}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Tab 1: Usage ─────────────────────────────────────── */}
      {tab === 1 && (
        <div className="two-col">
          <div className="card">
            <div className="card-header"><span className="card-title">Resource Usage</span></div>
            <div className="card-body">
              <UsageMeter label="Vectors" used={usage.vector_count || 0} limit={plan.max_vectors || 1} />
              <UsageMeter label="Users" used={usage.user_count || 0} limit={plan.max_users || 1} />
              <div className="divider" />
              <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                <div>
                  <div className="label">Vector Usage</div>
                  <span className="mono-sm" style={{ color: vectorPct > 100 ? 'var(--red)' : vectorPct > 80 ? 'var(--amber)' : 'var(--green)' }}>
                    {vectorPct.toFixed(1)}%
                  </span>
                </div>
                <div>
                  <div className="label">User Usage</div>
                  <span className="mono-sm" style={{ color: userPct > 80 ? 'var(--amber)' : 'var(--text-secondary)' }}>
                    {userPct.toFixed(1)}%
                  </span>
                </div>
                <div>
                  <div className="label">Max Batch PDFs</div>
                  <span className="mono-sm">{plan.max_batch_pdfs || '—'}</span>
                </div>
              </div>
              <div className="divider" />
              <button className="btn btn-secondary" onClick={doReconcile} style={{ width: '100%' }}>
                ⟳ Reconcile Now
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-header"><span className="card-title">Plan Limits</span></div>
            <div className="card-body">
              {[
                ['Max Vectors', (plan.max_vectors || 0).toLocaleString()],
                ['Max Users', plan.max_users || '—'],
                ['Max Batch PDFs', plan.max_batch_pdfs || '—'],
                ['Price/month', `$${plan.price_monthly || 0}`],
                ['Allowed Modes', (plan.allowed_modes || []).join(', ') || '—'],
              ].map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
                  <span className="text-muted">{k}</span>
                  <span className="mono-sm">{v}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Tab 2: Config ────────────────────────────────────── */}
      {tab === 2 && (
        <div>
          <div className="card mb-16">
            <div className="card-header" style={{ justifyContent: 'space-between' }}>
              <span className="card-title">Config Overrides</span>
              <span className="mono-xs text-muted">Merged over plan defaults at runtime</span>
            </div>
            <div className="card-body">
              <JsonEditor
                label="config_overrides (JSON)"
                value={configJson}
                onChange={v => { setConfigJson(v); setConfigDirty(true) }}
                rows={12}
              />
              <button
                className="btn btn-primary"
                onClick={saveConfig}
                disabled={configSaving || !configDirty}
                style={{ marginTop: 8 }}
              >
                {configSaving ? 'Saving…' : 'Save Config'}
              </button>
            </div>
          </div>

          <div className="card">
            <div className="card-header"><span className="card-title">Effective Config</span></div>
            <div className="card-body">
              <p className="text-secondary text-sm mb-12">
                Effective value = plan defaults merged with the overrides above.
                Tenant-specific keys always win.
              </p>
              <pre className="json-editor" style={{ background: 'var(--bg-base)', resize: 'none', cursor: 'default', minHeight: 80 }}>
                {JSON.stringify(tenant.config_overrides || {}, null, 2)}
              </pre>
            </div>
          </div>
        </div>
      )}

      {/* ── Tab 3: Documents ─────────────────────────────────── */}
      {tab === 3 && (
        <div className="card">
          <div className="card-header" style={{ justifyContent: 'space-between' }}>
            <span className="card-title">Documents ({docs.length})</span>
            <span className="mono-xs text-muted">Vectors freed on delete</span>
          </div>
          {docs.length === 0
            ? <EmptyState icon="📄" title="No documents" body="This tenant has not ingested any documents." />
            : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Filename</th>
                      <th>Chunks</th>
                      <th>Size</th>
                      <th>Status</th>
                      <th>Ingested</th>
                      <th style={{ width: 80 }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {docs.map(d => (
                      <tr key={d.id} onClick={() => { }}>
                        <td className="td-primary truncate" style={{ maxWidth: 240 }}>{d.filename}</td>
                        <td className="mono-sm">{d.chunk_count || 0}</td>
                        <td className="mono-sm">{d.file_size ? `${(d.file_size / 1024).toFixed(1)} KB` : '—'}</td>
                        <td>
                          <span className={`badge ${d.status === 'success' ? 'success' : d.status === 'failed' ? 'danger' : 'warning'}`}>
                            {d.status}
                          </span>
                        </td>
                        <td><RelTime iso={d.ingested_at} /></td>
                        <td onClick={e => e.stopPropagation()}>
                          <button
                            className="btn btn-danger btn-xs"
                            onClick={() => setConfirm({
                              type: 'doc',
                              label: 'Delete Document',
                              msg: `Delete "${d.filename}"? This will free ${d.chunk_count} vectors and cannot be undone.`,
                              danger: true,
                              action: () => doDeleteDoc(d.id, d.filename),
                            })}
                          >Delete</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </div>
      )}

      {/* ── Tab 4: Members ───────────────────────────────────── */}
      {tab === 4 && (
        <div className="card">
          <div className="card-header">
            <span className="card-title">Members ({members.length})</span>
          </div>
          {members.length === 0
            ? <EmptyState icon="👤" title="No members" body="No users have joined this tenant." />
            : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Email</th>
                      <th>Role</th>
                      <th>Joined</th>
                      <th>Last Login</th>
                      <th style={{ width: 200 }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.map(m => (
                      <tr key={m.id}>
                        <td className="td-primary">{m.email || <span className="text-muted mono-xs">{m.user_id?.slice(0, 8)}…</span>}</td>
                        <td>
                          <span className={`badge ${m.role === 'admin' ? 'info' : m.role === 'super_admin' ? 'danger' : 'neutral'}`}>
                            {m.role}
                          </span>
                        </td>
                        <td><RelTime iso={m.created_at} /></td>
                        <td><RelTime iso={m.last_sign_in_at} /></td>
                        <td onClick={e => e.stopPropagation()}>
                          <div style={{ display: 'flex', gap: 6 }}>
                            <select
                              className="input"
                              value={m.role}
                              style={{ width: 'auto', fontSize: 11, padding: '3px 6px' }}
                              onChange={e => {
                                const newRole = e.target.value
                                setConfirm({
                                  type: 'role',
                                  label: 'Change Role',
                                  msg: `Change ${m.email || m.user_id}'s role from "${m.role}" to "${newRole}"?`,
                                  danger: false,
                                  action: () => doPromoteMember(m.user_id, newRole),
                                })
                              }}
                            >
                              <option value="user">user</option>
                              <option value="admin">admin</option>
                            </select>
                            <button
                              className="btn btn-danger btn-xs"
                              onClick={() => setConfirm({
                                type: 'member',
                                label: 'Remove Member',
                                msg: `Remove ${m.email || m.user_id} from this tenant?`,
                                danger: true,
                                action: () => doRemoveMember(m.user_id, m.email),
                              })}
                            >Remove</button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </div>
      )}

      {/* ── Tab 5: Audit ─────────────────────────────────────── */}
      {tab === 5 && (
        <TenantAuditTab tenantId={id} />
      )}

      {/* Confirm modal */}
      {confirm && (
        <ConfirmModal
          title={confirm.label}
          message={confirm.msg}
          confirmLabel={confirm.label}
          danger={confirm.danger}
          onConfirm={async () => { await confirm.action(); setConfirm(null) }}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  )
}

// Lazy audit tab (avoids API call until needed)
function TenantAuditTab({ tenantId }) {
  const [entries, setEntries] = useState([])
  const [loading, setLoading] = useState(true)
  const { addToast } = useToast()

  useEffect(() => {
    import('../api/superAdmin').then(({ getActivity }) => {
      getActivity({ pageSize: 50, tenantId })
        .then(d => setEntries(d.items || []))
        .catch(e => addToast(e.message, 'error'))
        .finally(() => setLoading(false))
    })
  }, [tenantId])

  if (loading) return <Spinner />

  return (
    <div className="card">
      <div className="card-header"><span className="card-title">Audit Log (this tenant)</span></div>
      <div className="card-body">
        {entries.length === 0
          ? <EmptyState icon="📋" title="No audit entries" />
          : entries.map(a => (
            <div key={a.id} className="activity-item">
              <div className="activity-dot info" />
              <div className="activity-content">
                <div className="activity-action">
                  <span className="mono-xs" style={{ color: 'var(--accent)', marginRight: 8 }}>{a.action}</span>
                  <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{a.actor_email}</span>
                </div>
                {a.payload && Object.keys(a.payload).length > 0 && (
                  <pre className="mono-xs text-muted" style={{ marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxWidth: 600 }}>
                    {JSON.stringify(a.payload, null, 2).slice(0, 200)}{JSON.stringify(a.payload).length > 200 ? '…' : ''}
                  </pre>
                )}
                <RelTime iso={a.created_at} />
              </div>
            </div>
          ))
        }
      </div>
    </div>
  )
}