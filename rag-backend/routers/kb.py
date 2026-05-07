# rag-backend/routers/kb.py
#
# CHANGES vs previous version:
#
#   FIX — /kb/export now includes `parent_id` in every chunk payload.
#
#     ROOT CAUSE:
#       ChainResponse.get_citations() (online mode) deduplicates citations on
#       chunk["parent_id"].  HybridRetriever._expand_to_parents() also deduplicates
#       on parent_id.  But /kb/export was building its response from bm25._chunks
#       without ever copying the parent_id field — so every chunk exported to the
#       mobile app had no parent_id, making offline deduplication impossible.
#
#     FIX:
#       Add  "parent_id": c.get("parent_id", "")  to the chunk dict built inside
#       the for-loop in export_chunks().  The BM25 store retains the field exactly
#       as the HierarchicalChunker set it (format: "par_<md5[:12]>").
#
#     DOWNSTREAM EFFECT:
#       db.js stores parent_id in SQLite.
#       useChat.js (deep_offline path) deduplicates on parent_id before
#       displaying OfflineChunkCards — matching the online-mode behaviour.
#
# All other endpoints and logic are UNCHANGED.

import os
import time

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, JSONResponse
from schemas import (
    DocumentsResponse, HealthResponse,
    StatsResponse, WipeResponse,
)
from services        import rag_service
from routers.ingest  import _wipe_hashes
from config          import settings
from datetime        import datetime, timezone
from utils.logger    import get_logger

# Shared content-hash helper — must match get_vectors_for_export() in Qdrant
from vectorstore.qdrant_store import _content_hash

logger = get_logger(__name__)

router = APIRouter(tags=["kb"])


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health():
    """
    Public — used by the mobile app to determine its network mode.
    FORCE_OFFLINE_MODE=true in environment → always returns is_online=false.
    """
    force_offline = os.getenv("FORCE_OFFLINE_MODE", "false").strip().lower() == "true"
    ts = datetime.now(timezone.utc).isoformat()

    if force_offline:
        logger.warning(
            "[KB/HEALTH] FORCE_OFFLINE_MODE=true — reporting is_online=false "
            "(dev override active)"
        )
        return {
            "status":         "ok",
            "is_online":      False,
            "groq_available": bool(settings.groq_api_key),
            "timestamp":      ts,
        }

    is_online      = rag_service.is_online()
    groq_available = bool(settings.groq_api_key)

    logger.info(
        "[KB/HEALTH] Health check — is_online=%s  groq_available=%s",
        is_online, groq_available,
    )

    return HealthResponse(
        status          = "ok",
        groq_available  = groq_available,
        is_online       = is_online,
        timestamp       = ts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CHUNK EXPORT — for mobile offline sync
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/kb/export")
async def export_chunks(
    request: Request,
    include_vectors: bool = Query(default=True),
):
    """
    Export all knowledge base chunks for mobile offline sync.

    Query params:
      include_vectors=true  — attach 384-dim embedding to each chunk (default)
      include_vectors=false — text-only export (smaller payload)

    Headers:
      If-None-Match: <etag>  — client sends its stored etag; 304 returned if unchanged

    Response headers:
      X-Export-Etag: <md5>   — etag of current KB state

    Response body:
      {
        "chunks": [ { "id", "source", "content", "parent_content", "parent_id",
                      "page", "chunk_type", "section_path", "heading",
                      "bbox", "page_width", "page_height",
                      "embedding": [float x 384] | null }, ... ],
        "total":  <int>,
        "etag":   "<md5>"
      }

    parent_id: stable hash set by HierarchicalChunker (format "par_<md5[:12]>").
      Multiple child chunks sharing the same parent_id belong to the same
      parent passage.  The mobile app uses this for deduplication — matching
      the behaviour of ChainResponse.get_citations() in online mode.
    """
    logger.info(
        "[KB/EXPORT] /kb/export requested — include_vectors=%s",
        include_vectors,
    )
    t0 = time.perf_counter()

    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    raw  = bm25._chunks  # list of dicts from the BM25 store

    logger.debug(
        "[KB/EXPORT] BM25 store: %d chunks  Vector store: %s (%d vectors)",
        len(raw),
        type(vs).__name__,
        vs.count(),
    )

    # ── Etag delta check ─────────────────────────────────────────────────────
    etag        = await run_in_threadpool(vs.get_export_etag)
    client_etag = request.headers.get("If-None-Match", "")

    if client_etag and client_etag == etag:
        logger.info(
            "[KB/EXPORT] 304 Not Modified — client etag matches (etag=%s...)",
            etag[:12],
        )
        return Response(
            status_code = 304,
            headers     = {"X-Export-Etag": etag},
        )

    logger.info(
        "[KB/EXPORT] etag mismatch (client=%s...  server=%s...) — building export",
        client_etag[:12] if client_etag else "none",
        etag[:12],
    )

    # ── Fetch vectors from Qdrant ────────────────────────────────────────────
    # id_to_vec is keyed by _content_hash(source, page, content) —
    # the same hash we build below for each BM25 chunk.
    id_to_vec: dict = {}
    if include_vectors:
        logger.debug("[KB/EXPORT] Fetching vectors from Qdrant for export...")
        t_vecs = time.perf_counter()
        id_to_vec = await run_in_threadpool(vs.get_vectors_for_export)
        elapsed_vecs = (time.perf_counter() - t_vecs) * 1000
        logger.info(
            "[KB/EXPORT] Fetched %d vectors in %.0f ms",
            len(id_to_vec), elapsed_vecs,
        )

    # ── Build response payload ───────────────────────────────────────────────
    chunks          = []
    matched_vectors = 0

    _bm25_sample_logged = False

    for i, c in enumerate(raw):
        source  = c.get("source",  "")
        page    = c.get("page",    0)
        content = c.get("content", "")

        # ── DEEP DEBUG — log the very first chunk so field types can be verified
        if not _bm25_sample_logged:
            _bm25_sample_logged = True
            logger.debug(
                "[KB/EXPORT] First BM25 chunk sample — "
                "source=%r  page=%r  content_first80=%r  parent_id=%r",
                source, page, content[:80],
                c.get("parent_id", ""),
            )
            sample_keys = list(id_to_vec.keys())[:5]
            logger.debug(
                "[KB/EXPORT] Sample Qdrant hash keys: %s",
                sample_keys,
            )

        chunk_id  = _content_hash(source, page, content)
        embedding = id_to_vec.get(chunk_id)
        if embedding is not None:
            matched_vectors += 1

        chunks.append({
            "id":             chunk_id,
            "source":         source,
            "content":        content,
            "parent_content": c.get("parent_content") or content,
            # FIX: parent_id now exported so the mobile app can deduplicate
            # offline results on parent_id — mirroring ChainResponse.get_citations()
            # which is the reference implementation for online-mode deduplication.
            "parent_id":      c.get("parent_id", ""),
            "page":           page,
            "chunk_type":     c.get("type", "text"),
            "section_path":   c.get("section_path", ""),
            "heading":        c.get("heading",       ""),
            "bbox":           c.get("bbox"),
            "page_width":     c.get("page_width"),
            "page_height":    c.get("page_height"),
            "embedding":      embedding,
        })

    elapsed_total = (time.perf_counter() - t0) * 1000
    logger.info(
        "[KB/EXPORT] ✅ Export ready — chunks=%d  vectors_matched=%d  "
        "vector_match_rate=%.1f%%  etag=%s...  total=%.0f ms",
        len(chunks),
        matched_vectors,
        (matched_vectors / max(len(chunks), 1)) * 100,
        etag[:8],
        elapsed_total,
    )

    response = JSONResponse(content={
        "chunks": chunks,
        "total":  len(chunks),
        "etag":   etag,
    })
    response.headers["X-Export-Etag"] = etag
    return response


# ─────────────────────────────────────────────────────────────────────────────
# EXISTING ENDPOINTS (unchanged logic, logging added)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse)
async def stats():
    """Public — frontend polls this to show KB status."""
    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()

    vector_count = vs.count()
    bm25_count   = len(bm25)
    sources      = vs.list_sources()

    logger.debug(
        "[KB/STATS] Stats polled — vectors=%d  bm25=%d  sources=%d  "
        "embedding_model=%s  llm=%s",
        vector_count, bm25_count, len(sources),
        settings.embedding_model, settings.groq_model,
    )

    return StatsResponse(
        total_vectors   = vector_count,
        bm25_docs       = bm25_count,
        parent_count    = 0,
        indexed_files   = sources,
        embedding_model = settings.embedding_model,
        llm_model       = settings.groq_model,
        collection      = settings.qdrant_collection,
    )


@router.get("/documents", response_model=DocumentsResponse)
async def documents():
    files = rag_service.get_vector_store().list_sources()
    logger.debug("[KB/DOCUMENTS] Documents list — %d files indexed", len(files))
    return DocumentsResponse(files=files, total_files=len(files))


@router.delete("/collection", response_model=WipeResponse)
async def wipe():
    """Wipe the entire knowledge base."""
    logger.warning(
        "[KB/COLLECTION] ⚠ WIPE requested — deleting ALL vectors, BM25 index, "
        "and hash registry. This is irreversible!"
    )
    rag_service.get_vector_store().reset_collection()
    rag_service.get_bm25_store().reset()
    _wipe_hashes()
    logger.info("[KB/COLLECTION] ✅ Knowledge base wiped")
    return WipeResponse(status="ok", message="Knowledge base wiped.")


@router.get("/kb/debug-hash")
async def debug_hash():
    """
    Diagnostic — compare content-hash keys between BM25 and Qdrant.
    REMOVE before production.
    """
    logger.warning(
        "[KB/DEBUG] /kb/debug-hash called — this is a diagnostic endpoint "
        "and should be removed in production"
    )
    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    raw  = bm25._chunks

    qdrant_map = await run_in_threadpool(vs.get_vectors_for_export)

    bm25_keys  = set()
    bm25_samples = []
    for c in raw[:5]:  # first 5 only
        src = c.get("source", "")
        pg  = c.get("page",   0)
        ct  = c.get("content","")
        key = _content_hash(src, pg, ct)
        bm25_keys.add(key)
        bm25_samples.append({
            "source":       repr(src),
            "page":         repr(pg),
            "content_80":   repr(ct[:80]),
            "parent_id":    c.get("parent_id", ""),
            "hash":         key,
            "in_qdrant":    key in qdrant_map,
        })

    qdrant_samples = []
    for k in list(qdrant_map.keys())[:5]:
        qdrant_samples.append({
            "hash":    k,
            "in_bm25": k in bm25_keys,
        })

    overlap = len(bm25_keys & set(qdrant_map.keys()))

    logger.info(
        "[KB/DEBUG] debug-hash: bm25=%d  qdrant=%d  overlap(first5)=%d",
        len(raw), len(qdrant_map), overlap,
    )

    return {
        "bm25_total":               len(raw),
        "qdrant_total":             len(qdrant_map),
        "overlap_of_first_5_bm25":  overlap,
        "bm25_samples":             bm25_samples,
        "qdrant_samples":           qdrant_samples,
    }