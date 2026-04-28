# rag-backend/routers/kb.py
#
# CHANGES vs previous version:
#
#   FIX — /kb/export returned embedding: null for every chunk (0 vectors on mobile)
#     PROBLEM:
#       chunk_id was built as c.get("id") or f"{source}_{page}_{i}" from BM25 chunks.
#       get_vectors_for_export() previously keyed by Qdrant point UUID.
#       BM25 chunks have no knowledge of Qdrant UUIDs — lookup always missed.
#
#     FIX:
#       Import _content_hash from qdrant_store. Both the export endpoint and
#       get_vectors_for_export() now use the same hash of (source, page, content[:80]).
#       Since both BM25 and Qdrant store the same source/page/content values,
#       the lookup now always hits.
#
#   FIX — "Sync failed: HTTP 304" in mobile app
#     PROBLEM:
#       apiFetch() throws on any non-2xx status including 304.
#       syncFromServer() checked res.status === 304 AFTER the throw,
#       so it was always caught as an error, not as a skip signal.
#
#     FIX:
#       /kb/export is now fetched with raw fetch() in the mobile app (see
#       useOfflineSearch.js change). No change needed in kb.py for this —
#       the server-side 304 response is already correct.
#
#   KEPT: FORCE_OFFLINE_MODE override on /health (from previous version)
#   KEPT: All other endpoints unchanged

import os

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

# FIX: import the shared content-hash helper so chunk_id keys in this endpoint
# are IDENTICAL to the keys produced by get_vectors_for_export() in Qdrant.
from vectorstore.qdrant_store import _content_hash

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
        return {
            "status":         "ok",
            "is_online":      False,
            "groq_available": bool(settings.groq_api_key),
            "timestamp":      ts,
        }

    return HealthResponse(
        status          = "ok",
        groq_available  = bool(settings.groq_api_key),
        is_online       = rag_service.is_online(),
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
        "chunks": [ { "id", "source", "content", "parent_content", "page",
                      "chunk_type", "section_path", "heading",
                      "bbox", "page_width", "page_height",
                      "embedding": [float x 384] | null }, ... ],
        "total":  <int>,
        "etag":   "<md5>"
      }
    """
    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    raw  = bm25._chunks  # list of dicts from the BM25 store

    # ── Etag delta check ──────────────────────────────────────────────────────
    etag        = await run_in_threadpool(vs.get_export_etag)
    client_etag = request.headers.get("If-None-Match", "")
    if client_etag and client_etag == etag:
        # Nothing changed — tell the app to skip the DB write
        return Response(
            status_code = 304,
            headers     = {"X-Export-Etag": etag},
        )

    # ── Fetch vectors from Qdrant ─────────────────────────────────────────────
    # id_to_vec is keyed by _content_hash(source, page, content) —
    # the same hash we build below for each BM25 chunk.
    id_to_vec: dict = {}
    if include_vectors:
        id_to_vec = await run_in_threadpool(vs.get_vectors_for_export)

    # ── Build response payload ────────────────────────────────────────────────
    chunks          = []
    matched_vectors = 0

    _bm25_sample_logged = False

    for i, c in enumerate(raw):
        source  = c.get("source",  "")
        page    = c.get("page",    0)
        content = c.get("content", "")

        # ── DEEP DEBUG ────────────────────────────────────────────────────────
        if not _bm25_sample_logged:
            _bm25_sample_logged = True
            print(f"\n  [DEBUG/bm25] === FIRST BM25 CHUNK SAMPLE ===")
            print(f"  [DEBUG/bm25] raw keys in chunk dict: {list(c.keys())}")
            print(f"  [DEBUG/bm25] source type={type(source).__name__!r}  repr={repr(source)}")
            print(f"  [DEBUG/bm25] page   type={type(page).__name__!r}  value={page!r}")
            print(f"  [DEBUG/bm25] content type={type(content).__name__!r}  first 120 chars repr:")
            print(f"  [DEBUG/bm25]   {repr(content[:120])}")
            print(f"  [DEBUG/bm25] content[:80] repr for hash: {repr(content[:80])}")
            _key = f"{source}|{page}|{content[:80]}"
            print(f"  [DEBUG/bm25] hash input repr: {repr(_key)}")
            print(f"  [DEBUG/bm25] resulting hash : {_content_hash(source, page, content)}")
            print(f"  [DEBUG/bm25] id_to_vec has {len(id_to_vec)} keys total")
            # Print 5 sample keys from id_to_vec so we can visually compare
            sample_keys = list(id_to_vec.keys())[:5]
            print(f"  [DEBUG/bm25] sample Qdrant-side hash keys: {sample_keys}")
            print(f"  [DEBUG/bm25] ==========================================\n")
        # ── END DEEP DEBUG ────────────────────────────────────────────────────

        chunk_id  = _content_hash(source, page, content)
        embedding = id_to_vec.get(chunk_id)
        if embedding is not None:
            matched_vectors += 1

        chunks.append({
            "id":             chunk_id,
            "source":         source,
            "content":        content,
            "parent_content": c.get("parent_content") or content,
            "page":           page,
            "chunk_type":     c.get("type", "text"),
            "section_path":   c.get("section_path", ""),
            "heading":        c.get("heading",       ""),
            "bbox":           c.get("bbox"),
            "page_width":     c.get("page_width"),
            "page_height":    c.get("page_height"),
            "embedding":      embedding,
        })

    print(f"  [EXPORT] {len(chunks)} chunks exported, {matched_vectors} with vectors, etag={etag[:8]}...")

    response = JSONResponse(content={
        "chunks": chunks,
        "total":  len(chunks),
        "etag":   etag,
    })
    response.headers["X-Export-Etag"] = etag
    return response


# ─────────────────────────────────────────────────────────────────────────────
# EXISTING ENDPOINTS (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse)
async def stats():
    """Public — frontend polls this to show KB status."""
    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    return StatsResponse(
        total_vectors   = vs.count(),
        bm25_docs       = len(bm25),
        parent_count    = 0,
        indexed_files   = vs.list_sources(),
        embedding_model = settings.embedding_model,
        llm_model       = settings.groq_model,
        collection      = settings.qdrant_collection,
    )


@router.get("/documents", response_model=DocumentsResponse)
async def documents():
    files = rag_service.get_vector_store().list_sources()
    return DocumentsResponse(files=files, total_files=len(files))


@router.delete("/collection", response_model=WipeResponse)
async def wipe():
    """Wipe the entire knowledge base."""
    rag_service.get_vector_store().reset_collection()
    rag_service.get_bm25_store().reset()
    _wipe_hashes()
    return WipeResponse(status="ok", message="Knowledge base wiped.")

@router.get("/kb/debug-hash")
async def debug_hash():
    """
    Diagnostic — compare content-hash keys between BM25 and Qdrant.
    REMOVE before production.
    """
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
            "hash":         key,
            "in_qdrant":    key in qdrant_map,
        })

    qdrant_samples = []
    for k in list(qdrant_map.keys())[:5]:
        qdrant_samples.append({
            "hash":       k,
            "in_bm25":    k in bm25_keys,
        })

    overlap = len(bm25_keys & set(qdrant_map.keys()))

    return {
        "bm25_total":      len(raw),
        "qdrant_total":    len(qdrant_map),
        "overlap_of_first_5_bm25": overlap,
        "bm25_samples":    bm25_samples,
        "qdrant_samples":  qdrant_samples,
    }