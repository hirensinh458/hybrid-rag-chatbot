// src/components/IngestPanel.jsx
//
// Drag-and-drop PDF upload panel.
// Calls adminIngest(files) from api.js, shows per-file progress rows,
// then refreshes usage in AuthContext on success.
//
// Props:
//   onSuccess   func?   called with the ingest response after a successful upload
//   compact     bool?   if true, renders a slimmer version (used on OnboardingPage)

import { useState, useRef, useCallback } from 'react'
import { useAuth } from '../context/AuthContext'
import { adminIngest } from '../api'

// ── File row ──────────────────────────────────────────────────────────────────

function FileRow({ file, status, result, error }) {
  const icon =
    status === 'done'    ? '✓' :
    status === 'error'   ? '✗' :
    status === 'loading' ? '…' : '📄'

  const color =
    status === 'done'  ? 'var(--success)'  :
    status === 'error' ? 'var(--danger)'   :
    status === 'loading' ? 'var(--accent-text)' :
    'var(--text-2)'

  function fmtSize(b) {
    if (!b) return ''
    if (b < 1024)        return `${b} B`
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
    return `${(b / 1024 / 1024).toFixed(1)} MB`
  }

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '8px 12px',
      borderBottom: '1px solid var(--border)',
      animation: 'fadeUp .15s var(--ease)',
    }}>
      {/* Icon / spinner */}
      <span style={{
        width: 18, flexShrink: 0, textAlign: 'center',
        color,
        fontFamily: 'var(--font-mono)',
        fontSize: status === 'loading' ? '.8rem' : '.9rem',
        ...(status === 'loading' ? {
          display: 'inline-block',
          animation: 'spin .8s linear infinite',
        } : {}),
      }}>
        {icon}
      </span>

      {/* Filename */}
      <span style={{
        flex: 1, fontSize: '.78rem', color: 'var(--text-0)',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}>
        {file.name}
      </span>

      {/* Size */}
      <span style={{
        fontSize: '.65rem', color: 'var(--text-3)',
        fontFamily: 'var(--font-mono)', flexShrink: 0,
      }}>
        {fmtSize(file.size)}
      </span>

      {/* Result detail */}
      {status === 'done' && result && (
        <span style={{
          fontSize: '.65rem', color: 'var(--success)',
          fontFamily: 'var(--font-mono)', flexShrink: 0,
        }}>
          {result.chunks_added ?? result.chunk_count ?? ''} chunks
        </span>
      )}
      {status === 'error' && error && (
        <span style={{
          fontSize: '.65rem', color: 'var(--danger)',
          fontFamily: 'var(--font-mono)', flexShrink: 0,
          maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis',
        }}>
          {error}
        </span>
      )}
    </div>
  )
}

// ── Main ──────────────────────────────────────────────────────────────────────

export default function IngestPanel({ onSuccess, compact = false }) {
  const { plan, refreshUsage } = useAuth()

  const [dragOver,   setDragOver]   = useState(false)
  const [files,      setFiles]      = useState([])      // File[] staged
  const [statuses,   setStatuses]   = useState({})      // { name: 'pending'|'loading'|'done'|'error' }
  const [results,    setResults]    = useState({})      // { name: responseObj }
  const [errors,     setErrors]     = useState({})      // { name: errorMsg }
  const [uploading,  setUploading]  = useState(false)
  const [globalErr,  setGlobalErr]  = useState(null)

  const inputRef = useRef(null)

  const maxBatch = plan?.max_batch_pdfs ?? 3

  // ── File validation ────────────────────────────────────────────────────────
  const addFiles = useCallback((incoming) => {
    const pdfs = [...incoming].filter(f => f.type === 'application/pdf' || f.name.endsWith('.pdf'))
    if (!pdfs.length) return

    setGlobalErr(null)
    setFiles(prev => {
      const names = new Set(prev.map(f => f.name))
      const fresh = pdfs.filter(f => !names.has(f.name))
      const next  = [...prev, ...fresh]
      if (next.length > maxBatch) {
        setGlobalErr(`Your plan allows up to ${maxBatch} file${maxBatch !== 1 ? 's' : ''} per batch.`)
        return next.slice(0, maxBatch)
      }
      return next
    })
  }, [maxBatch])

  const removeFile = (name) => {
    setFiles(prev => prev.filter(f => f.name !== name))
    setStatuses(prev => { const n = { ...prev }; delete n[name]; return n })
    setErrors(prev => { const n = { ...prev }; delete n[name]; return n })
    setResults(prev => { const n = { ...prev }; delete n[name]; return n })
  }

  const clearAll = () => {
    setFiles([])
    setStatuses({})
    setResults({})
    setErrors({})
    setGlobalErr(null)
  }

  // ── Drag handlers ──────────────────────────────────────────────────────────
  const onDragOver  = (e) => { e.preventDefault(); setDragOver(true)  }
  const onDragLeave = ()  => setDragOver(false)
  const onDrop = (e) => {
    e.preventDefault()
    setDragOver(false)
    addFiles(e.dataTransfer.files)
  }
  const onInputChange = (e) => addFiles(e.target.files)

  // ── Upload ─────────────────────────────────────────────────────────────────
  const handleUpload = async () => {
    if (!files.length || uploading) return
    setUploading(true)
    setGlobalErr(null)

    // Mark all pending
    const initStatus = {}
    files.forEach(f => { initStatus[f.name] = 'loading' })
    setStatuses(initStatus)

    try {
      const response = await adminIngest(files)

      // Backend returns { results: [{ filename, status, chunks_added, error }] }
      // or a simpler object — handle both shapes
      const rows = response?.results ?? []

      const newStatuses = {}
      const newResults  = {}
      const newErrors   = {}

      files.forEach(f => {
        const row = rows.find(r => r.filename === f.name || r.filename?.endsWith(f.name))
        if (row) {
          const failed = row.status === 'failed' || row.error
          newStatuses[f.name] = failed ? 'error' : 'done'
          if (failed) newErrors[f.name]  = row.error ?? 'Ingestion failed'
          else        newResults[f.name] = row
        } else {
          // If backend doesn't itemise, mark all done
          newStatuses[f.name] = 'done'
          newResults[f.name]  = response
        }
      })

      setStatuses(newStatuses)
      setResults(newResults)
      setErrors(newErrors)

      // Refresh usage badge in sidebar
      await refreshUsage()

      // Notify parent (DashboardPage refreshes document list)
      if (onSuccess) onSuccess(response)

    } catch (err) {
      // Mark all as error
      const errStatuses = {}
      files.forEach(f => { errStatuses[f.name] = 'error' })
      setStatuses(errStatuses)
      setGlobalErr(err.message ?? 'Upload failed. Please try again.')
    } finally {
      setUploading(false)
    }
  }

  // ── Derived ────────────────────────────────────────────────────────────────
  const allDone    = files.length > 0 && files.every(f => statuses[f.name] === 'done')
  const hasErrors  = files.some(f => statuses[f.name] === 'error')
  const canUpload  = files.length > 0 && !uploading && !allDone
  const overLimit  = files.length >= maxBatch

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: compact ? 10 : 14 }}>

      {/* Batch limit hint */}
      {!compact && (
        <div style={{
          fontSize: '.68rem', color: 'var(--text-3)',
          fontFamily: 'var(--font-mono)',
          display: 'flex', justifyContent: 'space-between',
        }}>
          <span>PDF files only</span>
          <span style={{ color: overLimit ? 'var(--warn)' : 'var(--text-3)' }}>
            {files.length} / {maxBatch} files (plan limit)
          </span>
        </div>
      )}

      {/* Drop zone */}
      <div
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        style={{
          border: `2px dashed ${
            dragOver   ? 'var(--accent)'      :
            overLimit  ? 'var(--warn-border)' :
            'var(--border-md)'
          }`,
          borderRadius: 'var(--r-lg)',
          padding: compact ? '20px 16px' : '28px 20px',
          display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 8,
          cursor: overLimit ? 'not-allowed' : 'pointer',
          background: dragOver ? 'var(--accent-glow)' : 'var(--bg-2)',
          transition: 'border-color .15s, background .15s',
          textAlign: 'center',
        }}
      >
        <span style={{ fontSize: compact ? '1.4rem' : '1.8rem' }}>📥</span>
        <div style={{
          fontSize: compact ? '.75rem' : '.82rem',
          color: dragOver ? 'var(--accent-text)' : 'var(--text-2)',
          fontFamily: 'var(--font-body)',
          transition: 'color .15s',
        }}>
          {dragOver
            ? 'Drop to add files'
            : overLimit
              ? `Batch limit reached (${maxBatch} files)`
              : 'Drag & drop PDFs here, or click to browse'}
        </div>
        {!compact && (
          <div style={{
            fontSize: '.65rem', color: 'var(--text-3)',
            fontFamily: 'var(--font-mono)',
          }}>
            Up to {maxBatch} file{maxBatch !== 1 ? 's' : ''} per batch · PDF only
          </div>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept=".pdf,application/pdf"
        multiple
        onChange={onInputChange}
        style={{ display: 'none' }}
      />

      {/* File list */}
      {files.length > 0 && (
        <div style={{
          background: 'var(--bg-1)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--r-md)',
          overflow: 'hidden',
        }}>
          {files.map(f => (
            <div key={f.name} style={{ position: 'relative' }}>
              <FileRow
                file={f}
                status={statuses[f.name] ?? 'pending'}
                result={results[f.name]}
                error={errors[f.name]}
              />
              {/* Remove button — only when not uploading */}
              {!uploading && statuses[f.name] !== 'done' && (
                <button
                  onClick={() => removeFile(f.name)}
                  title="Remove"
                  style={{
                    position: 'absolute', right: 10, top: '50%',
                    transform: 'translateY(-50%)',
                    background: 'none', border: 'none',
                    cursor: 'pointer', color: 'var(--text-3)',
                    fontSize: '1rem', lineHeight: 1, padding: 2,
                  }}
                >×</button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Global error */}
      {globalErr && (
        <div style={{
          background: 'var(--danger-bg)', border: '1px solid var(--danger-border)',
          borderRadius: 'var(--r-md)', padding: '9px 13px',
          fontSize: '.73rem', color: 'var(--danger)',
          fontFamily: 'var(--font-mono)', animation: 'fadeUp .2s var(--ease)',
        }}>
          {globalErr}
        </div>
      )}

      {/* Success banner */}
      {allDone && !hasErrors && (
        <div style={{
          background: 'var(--success-bg)', border: '1px solid var(--success-border)',
          borderRadius: 'var(--r-md)', padding: '9px 13px',
          fontSize: '.73rem', color: 'var(--success)',
          fontFamily: 'var(--font-mono)', animation: 'fadeUp .2s var(--ease)',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <span>✓ Ingestion complete!</span>
          <button
            onClick={clearAll}
            style={{
              background: 'none', border: 'none',
              color: 'var(--success)', cursor: 'pointer',
              fontSize: '.7rem', fontFamily: 'var(--font-mono)',
              textDecoration: 'underline',
            }}
          >Upload more</button>
        </div>
      )}

      {/* Action buttons */}
      {!allDone && (
        <div style={{ display: 'flex', gap: 8 }}>
          {files.length > 0 && (
            <button
              onClick={clearAll}
              disabled={uploading}
              style={{
                background: 'none',
                border: '1px solid var(--border-md)',
                borderRadius: 'var(--r-md)',
                color: 'var(--text-2)',
                fontFamily: 'var(--font-mono)', fontSize: '.75rem',
                padding: '9px 14px', cursor: uploading ? 'not-allowed' : 'pointer',
                opacity: uploading ? .5 : 1,
              }}
            >
              Clear
            </button>
          )}
          <button
            onClick={handleUpload}
            disabled={!canUpload}
            style={{
              flex: 1,
              padding: '10px 20px',
              background: canUpload
                ? 'linear-gradient(135deg, var(--accent), var(--accent-dim))'
                : 'var(--bg-3)',
              border: 'none', borderRadius: 'var(--r-md)',
              color: canUpload ? '#fff' : 'var(--text-3)',
              fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: '.85rem',
              cursor: canUpload ? 'pointer' : 'not-allowed',
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
              ? 'Uploading…'
              : files.length === 0
                ? 'Select files to upload'
                : `Upload ${files.length} file${files.length !== 1 ? 's' : ''}`}
          </button>
        </div>
      )}
    </div>
  )
}