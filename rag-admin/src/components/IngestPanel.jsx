// src/components/IngestPanel.jsx
//
// Drag-and-drop PDF upload panel.
// Reads max_batch_pdfs from useAuth().plan and enforces it client-side.
// Calls adminIngest(files) then refreshUsage() on success.
//
// Props:
//   onSuccess   func?   (data) => void — called after a successful ingest

import { useState, useRef, useCallback } from 'react'
import { useAuth } from '../context/AuthContext'
import { adminIngest } from '../api'

export default function IngestPanel({ onSuccess }) {
  const { plan, refreshUsage } = useAuth()
  const maxBatch = plan?.max_batch_pdfs ?? 3

  const [files,     setFiles]     = useState([])   // File[] queued for upload
  const [dragging,  setDragging]  = useState(false)
  const [uploading, setUploading] = useState(false)
  const [result,    setResult]    = useState(null) // { ok: bool, message: string }
  const fileInputRef = useRef(null)

  // ── File helpers ────────────────────────────────────────────────────────────
  function fmtSize(bytes) {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  }

  const addFiles = useCallback((incoming) => {
    const pdfs = [...incoming].filter(
      f => f.type === 'application/pdf' || f.name.toLowerCase().endsWith('.pdf')
    )
    setFiles(prev => {
      const combined = [...prev, ...pdfs]
      // Deduplicate by name
      const seen = new Set()
      const deduped = combined.filter(f => {
        if (seen.has(f.name)) return false
        seen.add(f.name)
        return true
      })
      if (deduped.length > maxBatch) {
        setResult({
          ok: false,
          message: `Your plan allows ${maxBatch} file${maxBatch !== 1 ? 's' : ''} per batch. Extra files were removed.`,
        })
        return deduped.slice(0, maxBatch)
      }
      setResult(null)
      return deduped
    })
  }, [maxBatch])

  const removeFile = (idx) => setFiles(prev => prev.filter((_, i) => i !== idx))

  // ── Drag events ─────────────────────────────────────────────────────────────
  const onDragOver  = (e) => { e.preventDefault(); setDragging(true) }
  const onDragLeave = ()  => setDragging(false)
  const onDrop      = (e) => {
    e.preventDefault()
    setDragging(false)
    addFiles(e.dataTransfer.files)
  }

  // ── Upload ───────────────────────────────────────────────────────────────────
  const handleUpload = async () => {
    if (!files.length || uploading) return
    setUploading(true)
    setResult(null)
    try {
      const data = await adminIngest(files)
      await refreshUsage()
      const n = files.length
      setFiles([])
      setResult({
        ok: true,
        message: `✓ ${data?.total_chunks ?? '?'} chunks ingested from ${n} file${n !== 1 ? 's' : ''}.`,
      })
      if (onSuccess) onSuccess(data)
    } catch (err) {
      setResult({ ok: false, message: err.message ?? 'Ingestion failed.' })
    } finally {
      setUploading(false)
    }
  }

  const usedSlots = files.length
  const slotsLeft = maxBatch - usedSlots

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Drop zone */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => fileInputRef.current?.click()}
        onKeyDown={e => e.key === 'Enter' && fileInputRef.current?.click()}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        style={{
          border: `2px dashed ${dragging ? 'var(--accent)' : 'var(--border-md)'}`,
          borderRadius: 'var(--r-lg)',
          padding: '30px 24px',
          display: 'flex', flexDirection: 'column',
          alignItems: 'center', gap: 8,
          cursor: 'pointer', outline: 'none',
          background: dragging ? 'var(--accent-glow)' : 'var(--bg-2)',
          transition: 'all .15s',
        }}
      >
        <span style={{ fontSize: '1.8rem' }}>📥</span>
        <span style={{ fontSize: '.85rem', color: 'var(--text-1)', fontWeight: 500 }}>
          Drop PDFs here or{' '}
          <span style={{ color: 'var(--accent-text)', textDecoration: 'underline' }}>browse</span>
        </span>
        <span style={{
          fontSize: '.65rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', letterSpacing: '.06em',
        }}>
          {usedSlots > 0
            ? `${usedSlots} / ${maxBatch} file${maxBatch !== 1 ? 's' : ''} selected`
            : `Max ${maxBatch} file${maxBatch !== 1 ? 's' : ''} per batch · PDF only`}
        </span>

        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,application/pdf"
          multiple
          style={{ display: 'none' }}
          onChange={e => { addFiles(e.target.files); e.target.value = '' }}
        />
      </div>

      {/* File queue */}
      {files.length > 0 && (
        <div style={{
          background: 'var(--bg-2)', border: '1px solid var(--border)',
          borderRadius: 'var(--r-md)', overflow: 'hidden',
        }}>
          {files.map((f, i) => (
            <div key={`${f.name}-${i}`} style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '9px 14px',
              borderBottom: i < files.length - 1 ? '1px solid var(--border)' : 'none',
            }}>
              <span style={{ fontSize: '.85rem', flexShrink: 0 }}>📄</span>
              <span style={{
                flex: 1, fontSize: '.78rem', color: 'var(--text-1)',
                wordBreak: 'break-all',
              }}>
                {f.name}
              </span>
              <span style={{
                fontSize: '.65rem', color: 'var(--text-3)',
                fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap', flexShrink: 0,
              }}>
                {fmtSize(f.size)}
              </span>
              <button
                onClick={() => removeFile(i)}
                style={{
                  background: 'none', border: 'none',
                  cursor: 'pointer', color: 'var(--text-3)',
                  fontSize: '1.1rem', lineHeight: 1, padding: 0,
                  flexShrink: 0,
                }}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Upload button */}
      <button
        onClick={handleUpload}
        disabled={!files.length || uploading}
        style={{
          padding: '11px',
          background: !files.length || uploading
            ? 'var(--bg-3)'
            : 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
          border: 'none', borderRadius: 'var(--r-md)',
          color: !files.length || uploading ? 'var(--text-3)' : '#fff',
          fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: '.82rem',
          cursor: !files.length || uploading ? 'not-allowed' : 'pointer',
          transition: 'all .15s',
          display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
        }}
      >
        {uploading && (
          <span style={{
            width: 14, height: 14, borderRadius: '50%',
            border: '2px solid rgba(255,255,255,.3)', borderTopColor: '#fff',
            animation: 'spin .7s linear infinite', display: 'inline-block',
          }} />
        )}
        {uploading
          ? 'Ingesting…'
          : files.length > 0
            ? `Ingest ${files.length} file${files.length !== 1 ? 's' : ''}`
            : 'Select files to ingest'}
      </button>

      {/* Batch limit hint */}
      {slotsLeft < maxBatch && slotsLeft > 0 && (
        <div style={{
          fontSize: '.65rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)', textAlign: 'center',
        }}>
          {slotsLeft} slot{slotsLeft !== 1 ? 's' : ''} remaining in this batch
        </div>
      )}

      {/* Result message */}
      {result && (
        <div style={{
          background: result.ok ? 'var(--success-bg)' : 'var(--danger-bg)',
          border: `1px solid ${result.ok ? 'var(--success-border)' : 'var(--danger-border)'}`,
          borderRadius: 'var(--r-md)', padding: '10px 14px',
          fontSize: '.75rem',
          color: result.ok ? 'var(--success)' : 'var(--danger)',
          fontFamily: 'var(--font-mono)',
          animation: 'fadeUp .2s var(--ease)',
        }}>
          {result.message}
        </div>
      )}
    </div>
  )
}