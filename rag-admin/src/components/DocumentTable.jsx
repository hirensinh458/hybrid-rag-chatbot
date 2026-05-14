// src/components/DocumentTable.jsx
//
// Table of ingested documents with per-row inline delete confirmation.
//
// Props:
//   documents  array    [{ id, filename, chunk_count, file_size, status, ingested_at }]
//   onDelete   func     async (id, filename) => void — called when delete is confirmed
//   loading    bool     show skeleton rows while fetching

import { useState } from 'react'

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusBadge({ status }) {
  const MAP = {
    success: { bg: 'var(--success-bg)', border: 'var(--success-border)', color: 'var(--success)', label: 'OK' },
    failed:  { bg: 'var(--danger-bg)',  border: 'var(--danger-border)',  color: 'var(--danger)',  label: 'Failed' },
    partial: { bg: 'var(--warn-bg)',    border: 'var(--warn-border)',    color: 'var(--warn)',    label: 'Partial' },
  }
  const s = MAP[status] ?? MAP.success
  return (
    <span style={{
      background: s.bg, border: `1px solid ${s.border}`,
      borderRadius: 20, padding: '2px 9px',
      fontSize: '.62rem', fontFamily: 'var(--font-mono)',
      color: s.color, whiteSpace: 'nowrap',
    }}>
      {s.label}
    </span>
  )
}

function SkeletonRow() {
  return (
    <div style={{
      height: 50,
      background: 'linear-gradient(90deg, var(--bg-3) 0%, var(--bg-4) 50%, var(--bg-3) 100%)',
      backgroundSize: '200% 100%',
      animation: 'shimmer 1.4s ease-in-out infinite',
      borderRadius: 'var(--r-sm)',
      marginBottom: 4,
    }} />
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtSize(bytes) {
  if (!bytes) return '—'
  if (bytes < 1024)             return `${bytes} B`
  if (bytes < 1024 * 1024)      return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString('en-GB', {
    day: '2-digit', month: 'short', year: 'numeric',
  })
}

// ── DocumentTable ─────────────────────────────────────────────────────────────

export default function DocumentTable({ documents = [], onDelete, loading = false }) {
  const [confirmId,  setConfirmId]  = useState(null)   // row id awaiting confirmation
  const [deletingId, setDeletingId] = useState(null)   // row id currently deleting

  const triggerDelete = (id) => setConfirmId(id)
  const cancelDelete  = ()   => setConfirmId(null)

  const confirmDelete = async (id, filename) => {
    setDeletingId(id)
    try {
      await onDelete(id, filename)
    } finally {
      setDeletingId(null)
      setConfirmId(null)
    }
  }

  // ── Loading skeleton ───────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={wrapStyle}>
        <div style={{ padding: '16px 16px 10px' }}>
          {[...Array(3)].map((_, i) => <SkeletonRow key={i} />)}
        </div>
      </div>
    )
  }

  // ── Empty state ────────────────────────────────────────────────────────────
  if (!documents.length) {
    return (
      <div style={{ ...wrapStyle, padding: '48px 24px', textAlign: 'center' }}>
        <div style={{ fontSize: '2rem', marginBottom: 10 }}>📂</div>
        <div style={{
          fontFamily: 'var(--font-display)', fontWeight: 700,
          fontSize: '.9rem', color: 'var(--text-1)', marginBottom: 6,
        }}>
          No documents yet
        </div>
        <div style={{ fontSize: '.75rem', color: 'var(--text-3)' }}>
          Upload PDFs using the panel above — they'll appear here after ingestion.
        </div>
      </div>
    )
  }

  // ── Table ──────────────────────────────────────────────────────────────────
  return (
    <div style={wrapStyle}>
      {/* Header */}
      <div style={headerStyle}>
        <span style={{ flex: '0 0 28px' }} />
        <span style={{ flex: 4 }}>Filename</span>
        <span style={{ flex: 1, textAlign: 'right' }}>Chunks</span>
        <span style={{ flex: 1, textAlign: 'right' }}>Size</span>
        <span style={{ flex: 2, textAlign: 'right' }}>Ingested</span>
        <span style={{ flex: 1, textAlign: 'center' }}>Status</span>
        <span style={{ flex: '0 0 110px' }} />
      </div>

      {/* Rows */}
      {documents.map((doc, i) => {
        const isDeleting  = deletingId === doc.id
        const isConfirming = confirmId === doc.id
        const isLast       = i === documents.length - 1

        return (
          <div
            key={doc.id}
            style={{
              display: 'flex', alignItems: 'center', gap: 12,
              padding: '11px 16px',
              borderBottom: isLast ? 'none' : '1px solid var(--border)',
              opacity: isDeleting ? .35 : 1,
              transition: 'opacity .25s',
              background: isConfirming ? 'var(--danger-bg)' : 'transparent',
            }}
          >
            {/* Icon */}
            <span style={{ flex: '0 0 28px', fontSize: '.9rem' }}>📄</span>

            {/* Filename */}
            <span style={{ flex: 4, ...cellPrimary, wordBreak: 'break-word' }}>
              {doc.filename}
            </span>

            {/* Chunks */}
            <span style={{ flex: 1, textAlign: 'right', ...cellMono }}>
              {doc.chunk_count ?? '—'}
            </span>

            {/* Size */}
            <span style={{ flex: 1, textAlign: 'right', ...cellMono }}>
              {fmtSize(doc.file_size)}
            </span>

            {/* Date */}
            <span style={{ flex: 2, textAlign: 'right', ...cellSub }}>
              {fmtDate(doc.ingested_at)}
            </span>

            {/* Status */}
            <span style={{ flex: 1, display: 'flex', justifyContent: 'center' }}>
              <StatusBadge status={doc.status} />
            </span>

            {/* Delete column */}
            <span style={{ flex: '0 0 110px', display: 'flex', justifyContent: 'flex-end', gap: 6 }}>
              {isConfirming ? (
                <>
                  <button
                    onClick={() => confirmDelete(doc.id, doc.filename)}
                    disabled={isDeleting}
                    style={confirmBtnStyle}
                  >
                    {isDeleting ? '…' : 'Delete'}
                  </button>
                  <button onClick={cancelDelete} style={cancelBtnStyle}>Cancel</button>
                </>
              ) : (
                <button
                  onClick={() => triggerDelete(doc.id)}
                  disabled={!!deletingId}
                  style={deleteBtnStyle}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--danger-border)'; e.currentTarget.style.color = 'var(--danger)' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--text-3)' }}
                >
                  Delete
                </button>
              )}
            </span>
          </div>
        )
      })}

      {/* Footer: document count */}
      <div style={{
        padding: '10px 16px',
        borderTop: '1px solid var(--border)',
        fontSize: '.65rem', color: 'var(--text-3)',
        fontFamily: 'var(--font-mono)',
        textAlign: 'right',
      }}>
        {documents.length} document{documents.length !== 1 ? 's' : ''}
      </div>
    </div>
  )
}

// ── Styles ────────────────────────────────────────────────────────────────────

const wrapStyle = {
  background: 'var(--bg-2)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--r-lg)',
  overflow: 'hidden',
}

const headerStyle = {
  display: 'flex', alignItems: 'center', gap: 12,
  padding: '9px 16px',
  borderBottom: '1px solid var(--border)',
  fontSize: '.62rem', fontFamily: 'var(--font-mono)',
  letterSpacing: '.1em', textTransform: 'uppercase',
  color: 'var(--text-3)',
}

const cellPrimary = {
  fontSize: '.8rem', color: 'var(--text-0)',
}

const cellMono = {
  fontSize: '.72rem', color: 'var(--accent-text)',
  fontFamily: 'var(--font-mono)',
}

const cellSub = {
  fontSize: '.7rem', color: 'var(--text-2)',
  fontFamily: 'var(--font-mono)',
}

const deleteBtnStyle = {
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: 'var(--r-sm)',
  color: 'var(--text-3)',
  fontSize: '.68rem', fontFamily: 'var(--font-mono)',
  padding: '3px 10px', cursor: 'pointer',
  transition: 'all .1s',
}

const confirmBtnStyle = {
  background: 'var(--danger)', border: 'none',
  borderRadius: 'var(--r-sm)', color: '#fff',
  fontSize: '.68rem', fontFamily: 'var(--font-mono)',
  padding: '3px 10px', cursor: 'pointer',
  fontWeight: 600,
}

const cancelBtnStyle = {
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: 'var(--r-sm)', color: 'var(--text-3)',
  fontSize: '.68rem', fontFamily: 'var(--font-mono)',
  padding: '3px 10px', cursor: 'pointer',
}