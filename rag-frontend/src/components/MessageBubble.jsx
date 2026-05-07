// src/components/MessageBubble.jsx
//
// CHANGE — Online citations are now clickable PDF buttons (same as offline mode):
//
//   BEFORE:
//     Citations rendered as plain <span> chips — decorative only, not interactive.
//     Clicking a source like "engine.pdf · p12" did nothing.
//
//   AFTER:
//     Each citation chip is a <button> that opens PdfViewerModal at the correct
//     page, with the section path shown in the modal header — identical behaviour
//     to the offline OfflineChunkCard "Open" button.
//
//   HOW:
//     1. Citations component receives an onOpenCitation(citation) callback.
//     2. Clicking a chip calls onOpenCitation with the citation object.
//     3. MessageBubble manages pdfModal state (same pattern as OfflineChunkCards).
//     4. PdfViewerModal is opened with { filename, page, bbox, sectionPath }.
//
//   NOTE ON bbox:
//     Online citations currently carry { source, page, heading, section_path, type }.
//     bbox is NOT included in the citation object sent by the backend (it is only
//     stored per-chunk, not per-citation). The modal opens at the correct page
//     but without a highlight box — exactly the same as offline chunks that have
//     no bbox. No backend change is needed; bbox simply defaults to null.
//
// All other code (OfflineChunkCard, RetrievedImages, etc.) is UNCHANGED.

import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm    from 'remark-gfm'
import PdfViewerModal from './PdfViewerModal'

const IMAGE_BASE = ''

function Cursor() {
  return (
    <span style={{
      display: 'inline-block', width: 2, height: '1em',
      background: 'var(--accent)', marginLeft: 2,
      verticalAlign: 'text-bottom',
      animation: 'blink 1s step-end infinite',
    }} />
  )
}

// ── CHANGED: Citations now receives onOpenCitation callback ──────────────────
// Each chip is a clickable button that fires onOpenCitation(citation).
// Hover state shows the cursor change and a subtle border highlight so users
// know the chip is interactive, matching the offline "Open" button affordance.

function Citations({ citations, onOpenCitation }) {
  if (!citations?.length) return null

  // Deduplicate on (source, page) — same logic as before, unchanged.
  const unique = citations.filter(
    (c, i, a) => a.findIndex(x => x.source === c.source && x.page === c.page) === i
  )

  return (
    <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 5 }}>
      {unique.map((c, i) => {
        // BUG 2 FIX (unchanged): backend sends `type`, not `chunk_type`.
        const chunkType = c.type || c.chunk_type || 'text'
        const icon      = chunkType === 'image' ? '🖼' : chunkType === 'table' ? '⊞' : '◈'
        const section   = c.section_path || c.heading || ''
        const hasPdf    = !!c.source

        return (
          // CHANGED: was <span cursor:'default'>, now <button> that opens PDF viewer
          <button
            key={i}
            onClick={() => hasPdf && onOpenCitation(c)}
            disabled={!hasPdf}
            title={
              hasPdf
                ? `Open ${c.source}${c.page ? ` at page ${c.page}` : ''} in PDF viewer`
                : section || undefined
            }
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              background: 'var(--teal-dim)',
              border: '1px solid rgba(45,212,191,.18)',
              borderRadius: 20, padding: '3px 10px',
              fontSize: '.69rem', color: 'var(--teal)',
              fontFamily: 'var(--font-mono)',
              // Interactive styles
              cursor: hasPdf ? 'pointer' : 'default',
              transition: 'border-color .15s, background .15s',
              // Reset browser button defaults
              outline: 'none',
              textDecoration: 'none',
            }}
            onMouseEnter={e => {
              if (hasPdf) {
                e.currentTarget.style.borderColor  = 'rgba(45,212,191,.55)'
                e.currentTarget.style.background   = 'rgba(45,212,191,.18)'
              }
            }}
            onMouseLeave={e => {
              e.currentTarget.style.borderColor = 'rgba(45,212,191,.18)'
              e.currentTarget.style.background  = 'var(--teal-dim)'
            }}
          >
            <span style={{ fontSize: 10 }}>{icon}</span>

            {/* Source filename + page number — same text as before */}
            {c.source}{c.page ? ` · p${c.page}` : ''}

            {/* Section label — unchanged */}
            {section && (
              <span style={{ color: 'rgba(45,212,191,.5)', fontSize: '.65rem' }}>
                {section.length > 22 ? section.slice(0, 22) + '…' : section}
              </span>
            )}

            {/* NEW: small "open" arrow so users know it's clickable */}
            {hasPdf && (
              <span style={{
                fontSize: '.6rem', color: 'rgba(45,212,191,.6)',
                marginLeft: 1,
              }}>
                ↗
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

function RetrievedImages({ imageUrls }) {
  if (!imageUrls?.length) return null
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{
        fontSize: '.62rem', fontFamily: 'var(--font-mono)',
        color: 'var(--text-3)', letterSpacing: '.1em',
        textTransform: 'uppercase', marginBottom: 8,
      }}>Referenced images</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {imageUrls.map((url, i) => (
          <div key={i} style={{
            border: '1px solid var(--border)', borderRadius: 'var(--r-md)',
            overflow: 'hidden', background: 'var(--bg-2)',
          }}>
            <img
              src={`${IMAGE_BASE}${url}`}
              alt={`Figure ${i + 1}`}
              style={{ maxWidth: '100%', display: 'block', maxHeight: 320, objectFit: 'contain' }}
              onError={e => { e.target.style.display = 'none' }}
            />
            <div style={{
              padding: '4px 10px', fontSize: '.65rem',
              color: 'var(--text-3)', fontFamily: 'var(--font-mono)',
              borderTop: '1px solid var(--border)',
            }}>{url.split('/').pop()}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function TypingDots() {
  return (
    <div style={{ display: 'flex', gap: 5, alignItems: 'center', padding: '4px 0' }}>
      {[0, 1, 2].map(i => (
        <div key={i} style={{
          width: 7, height: 7, borderRadius: '50%',
          background: 'var(--accent)',
          animation: `pulse 1.4s ease-in-out ${i * 0.2}s infinite`,
          opacity: .6,
        }} />
      ))}
    </div>
  )
}

// ── B4: Highlight query keywords in text (UNCHANGED) ─────────────────────────
const STOPWORDS = new Set(['a','an','the','is','are','was','were','be','been','being',
  'have','has','had','do','does','did','will','would','could','should','may','might',
  'shall','can','need','dare','used','ought','in','on','at','to','for','of','and',
  'or','but','not','with','this','that','it','its','as','by','from','what','how'])

function highlightKeywords(text, query) {
  if (!query) return text
  const words = query.toLowerCase().split(/\s+/).filter(w => w.length > 2 && !STOPWORDS.has(w))
  if (!words.length) return text

  const pattern = new RegExp(`(${words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi')
  const parts = text.split(pattern)

  return parts.map((part, i) =>
    pattern.test(part)
      ? <mark key={i} style={{ background: 'rgba(251,191,36,0.25)', color: 'var(--text-0)', borderRadius: 2, padding: '0 1px' }}>{part}</mark>
      : part
  )
}

// ── B3: Section breadcrumb (UNCHANGED) ───────────────────────────────────────
function SectionBreadcrumb({ section }) {
  if (!section) return null
  const parts = section.split(/\s*[>\/]\s*/)
  return (
    <div style={{
      fontFamily: 'var(--font-mono)', fontSize: '.62rem',
      color: 'var(--text-2)', marginTop: 4,
      display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 3,
    }}>
      {parts.map((p, i) => (
        <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
          {i > 0 && <span style={{ color: 'var(--text-3)' }}>›</span>}
          <span style={{ color: i === parts.length - 1 ? 'var(--accent-text)' : 'var(--text-2)' }}>{p}</span>
        </span>
      ))}
    </div>
  )
}

// ── B3: Relevance bar (UNCHANGED) ────────────────────────────────────────────
function RelevanceBar({ score }) {
  const pct = Math.min(1, Math.max(0, score)) * 100
  const color = pct > 65 ? '#34d399' : pct > 35 ? '#fbbf24' : '#94a3b8'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 8 }}>
      <div style={{
        flex: 1, height: 3, background: 'var(--bg-4)',
        borderRadius: 2, overflow: 'hidden',
      }}>
        <div style={{ width: `${pct}%`, height: '100%', background: color, borderRadius: 2, transition: 'width .3s' }} />
      </div>
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: '.6rem', color: 'var(--text-3)', flexShrink: 0 }}>
        {score.toFixed(2)}
      </span>
    </div>
  )
}

// ── Offline chunk card (B2 + B3 + B4 + B5) — UNCHANGED ──────────────────────
function OfflineChunkCard({ chunk, index, query, onOpenPdf }) {
  const [expanded, setExpanded] = useState(false)
  const section  = chunk.section_path || chunk.heading || ''
  const lines    = chunk.content.split('\n')
  const isLong   = lines.length > 3 || chunk.content.length > 300
  const displayed = (!isLong || expanded) ? chunk.content : lines.slice(0, 3).join('\n')
  const hasPdf   = !!chunk.source

  return (
    <div style={{
      background: 'var(--bg-3)',
      border: '1px solid var(--border-md)',
      borderRadius: 'var(--r-md)',
      marginBottom: 8,
      overflow: 'hidden',
      transition: 'border-color .15s',
    }}
      onMouseEnter={e => e.currentTarget.style.borderColor = 'rgba(124,106,247,.35)'}
      onMouseLeave={e => e.currentTarget.style.borderColor = 'var(--border-md)'}
    >
      {/* ── Header row ── */}
      <div style={{ padding: '10px 14px 0' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{
            fontFamily: 'var(--font-mono)', fontSize: '.6rem',
            color: 'var(--text-3)', background: 'var(--bg-4)',
            padding: '2px 6px', borderRadius: 8, flexShrink: 0,
          }}>#{index + 1}</span>

          {/* B2: Clickable source+page → opens PDF viewer */}
          <button
            onClick={() => hasPdf && onOpenPdf(chunk)}
            disabled={!hasPdf}
            title={hasPdf ? 'Open in manual' : ''}
            style={{
              background: 'none', border: 'none', padding: 0,
              fontFamily: 'var(--font-mono)', fontSize: '.68rem',
              color: hasPdf ? 'var(--teal)' : 'var(--text-3)',
              cursor: hasPdf ? 'pointer' : 'default',
              flex: 1, textAlign: 'left',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              textDecoration: hasPdf ? 'underline' : 'none',
              textDecorationColor: 'rgba(45,212,191,.3)',
              textUnderlineOffset: 3,
            }}
          >
            📄 {chunk.source}{chunk.page ? ` · Page ${chunk.page}` : ''}
          </button>

          {hasPdf && (
            <button
              onClick={() => onOpenPdf(chunk)}
              style={{
                background: 'var(--bg-4)', border: '1px solid var(--border-md)',
                color: 'var(--teal)', borderRadius: 6,
                padding: '2px 8px', fontSize: '.6rem', cursor: 'pointer',
                fontFamily: 'var(--font-mono)', flexShrink: 0,
                display: 'flex', alignItems: 'center', gap: 4,
              }}
              title="Open in PDF viewer"
            >
              📖 Open
            </button>
          )}
        </div>

        {/* B3: Section breadcrumb on its own row */}
        <SectionBreadcrumb section={section} />
      </div>

      {/* ── Chunk content (B4 highlight + B5 expand) ── */}
      <div style={{ padding: '10px 14px 0' }}>
        <div style={{
          fontSize: '.83rem', color: 'var(--text-1)',
          lineHeight: 1.65, whiteSpace: 'pre-wrap',
          fontFamily: 'var(--font-body)',
        }}>
          {highlightKeywords(displayed, query)}
          {isLong && !expanded && (
            <span style={{ color: 'var(--text-3)', fontStyle: 'italic' }}>…</span>
          )}
        </div>

        {/* B5: Show more/less toggle */}
        {isLong && (
          <button
            onClick={() => setExpanded(e => !e)}
            style={{
              background: 'none', border: 'none', padding: '6px 0 0',
              color: 'var(--accent-text)', fontSize: '.72rem',
              fontFamily: 'var(--font-mono)', cursor: 'pointer',
              display: 'block',
            }}
          >
            {expanded ? '▲ Show less' : '▼ Show more'}
          </button>
        )}
      </div>

      {/* B3: Relevance bar */}
      <div style={{ padding: '4px 14px 10px' }}>
        <RelevanceBar score={chunk.score} />
      </div>
    </div>
  )
}

// Renders all offline chunks with a header (UNCHANGED)
function OfflineChunkCards({ chunks, query }) {
  const [pdfModal, setPdfModal] = useState(null)

  if (!chunks?.length) {
    return (
      <div style={{ fontSize: '.85rem', color: 'var(--text-2)', padding: '8px 0' }}>
        No relevant manual sections found for this query.
      </div>
    )
  }

  const handleOpenPdf = (chunk) => {
    setPdfModal({
      filename   : chunk.source,
      page       : chunk.page || 1,
      bbox       : chunk.bbox || null,
      sectionPath: chunk.section_path || chunk.heading || '',
    })
  }

  return (
    <div>
      <div style={{
        fontSize: '.62rem', fontFamily: 'var(--font-mono)',
        letterSpacing: '.1em', textTransform: 'uppercase',
        color: 'var(--text-3)', marginBottom: 10,
      }}>
        {chunks.length} relevant section{chunks.length > 1 ? 's' : ''} found
      </div>
      {chunks.map((chunk, i) => (
        <OfflineChunkCard
          key={i}
          chunk={chunk}
          index={i}
          query={query}
          onOpenPdf={handleOpenPdf}
        />
      ))}

      {/* B1: PDF viewer modal */}
      {pdfModal && (
        <PdfViewerModal
          filename={pdfModal.filename}
          page={pdfModal.page}
          bbox={pdfModal.bbox}
          sectionPath={pdfModal.sectionPath}
          onClose={() => setPdfModal(null)}
        />
      )}
    </div>
  )
}


// ── Main export ───────────────────────────────────────────────────────────────

export default function MessageBubble({ message, query }) {
  const isUser = message.role === 'user'

  // NEW: pdfModal state for online mode citation clicks
  // Sits here (not inside Citations) so PdfViewerModal is rendered outside
  // the flex chip row, avoiding layout issues from a modal inside inline-flex.
  const [onlinePdfModal, setOnlinePdfModal] = useState(null)

  // Handler passed down to Citations — converts a citation object into modal props
  const handleOpenCitation = (citation) => {
    setOnlinePdfModal({
      filename   : citation.source,
      page       : citation.page || 1,
      // bbox is not in citation objects — opens at correct page without highlight
      bbox       : citation.bbox || null,
      sectionPath: citation.section_path || citation.heading || '',
    })
  }

  if (isUser) {
    return (
      <div style={{
        display: 'flex', justifyContent: 'flex-end',
        animation: 'fadeUp .22s var(--ease)', marginBottom: 12,
      }}>
        <div style={{
          maxWidth: '68%',
          background: 'linear-gradient(135deg, #1e1b3a, #17142e)',
          border: '1px solid rgba(124,106,247,.2)',
          borderRadius: '18px 18px 4px 18px',
          padding: '11px 16px',
        }}>
          <div style={{
            fontSize: '.62rem', fontFamily: 'var(--font-mono)',
            letterSpacing: '.1em', textTransform: 'uppercase',
            color: 'var(--accent-dim)', marginBottom: 5,
          }}>you</div>
          <div style={{ fontSize: '.88rem', color: 'var(--text-0)', lineHeight: 1.55 }}>
            {message.content}
          </div>
        </div>
      </div>
    )
  }

  // Assistant
  const tokens = message.usage?.total_tokens

  return (
    <div style={{
      display: 'flex', justifyContent: 'flex-start',
      animation: 'fadeUp .22s var(--ease)', marginBottom: 12,
    }}>
      {/* Avatar dot */}
      <div style={{
        width: 28, height: 28, borderRadius: 8, flexShrink: 0,
        background: message.is_offline
          ? 'linear-gradient(135deg, #4b5563, #374151)'
          : 'linear-gradient(135deg, var(--accent), var(--accent-dim))',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: '.75rem', color: '#fff', marginRight: 10, marginTop: 2,
      }}>
        {message.is_offline ? '📋' : '✦'}
      </div>

      <div style={{
        maxWidth: 'min(78%, 700px)',
        background: 'var(--bg-2)',
        border: `1px solid ${message.is_offline ? 'rgba(255,255,255,.08)' : 'var(--border-md)'}`,
        borderRadius: '4px 18px 18px 18px',
        padding: '12px 16px',
        minWidth: 60,
      }}>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 7,
        }}>
          <div style={{
            fontSize: '.62rem', fontFamily: 'var(--font-mono)',
            letterSpacing: '.1em', textTransform: 'uppercase',
            color: message.is_offline ? 'var(--text-3)' : 'var(--accent-text)',
          }}>
            {message.is_offline ? 'manual sections' : 'docmind'}
          </div>
          {message.is_offline && (
            <span style={{
              fontSize: '.6rem', fontFamily: 'var(--font-mono)',
              color: 'var(--text-3)', background: 'var(--bg-3)',
              padding: '2px 7px', borderRadius: 10,
            }}>offline · retrieval only</span>
          )}
        </div>

        {/* ── OFFLINE: chunk cards (UNCHANGED) ─────────── */}
        {message.is_offline && (
          <OfflineChunkCards chunks={message.offline_chunks} query={query} />
        )}

        {/* ── ONLINE: markdown + CHANGED citations ──────── */}
        {!message.is_offline && (
          <>
            {message.content ? (
              <div className="md" style={{ fontSize: '.87rem', color: 'var(--text-1)', lineHeight: 1.7 }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {message.content}
                </ReactMarkdown>
                {message.streaming && <Cursor />}
              </div>
            ) : (
              <TypingDots />
            )}

            {!message.streaming && (
              <>
                <RetrievedImages imageUrls={message.image_urls} />

                {/* CHANGED: pass onOpenCitation so chips open the PDF viewer */}
                <Citations
                  citations={message.citations}
                  onOpenCitation={handleOpenCitation}
                />

                {tokens && (
                  <div style={{
                    marginTop: 8, fontSize: '.64rem', fontFamily: 'var(--font-mono)',
                    color: 'var(--text-3)', textAlign: 'right',
                  }}>
                    {tokens.toLocaleString()} tokens
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>

      {/* NEW: PDF viewer modal for online citation clicks — rendered outside the bubble */}
      {onlinePdfModal && (
        <PdfViewerModal
          filename={onlinePdfModal.filename}
          page={onlinePdfModal.page}
          bbox={onlinePdfModal.bbox}
          sectionPath={onlinePdfModal.sectionPath}
          onClose={() => setOnlinePdfModal(null)}
        />
      )}
    </div>
  )
}