# rag-backend/routers/kb.py

import os
import time
import asyncio
import hashlib

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, JSONResponse
from schemas import (
    DocumentsResponse, HealthResponse,
    StatsResponse, WipeResponse,
)
from services        import rag_service
from config          import settings
from datetime        import datetime, timezone
from utils.logger    import get_logger

from vectorstore.qdrant_store import _content_hash

logger = get_logger(__name__)

router = APIRouter(tags=["kb"])

# Module-level caches — defined here, imported by ingest.py via local import
_vec_cache: dict = {}
_vec_cache_lock = asyncio.Lock()

_source_hash_cache: dict = {}
_source_hash_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health():
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
# CHUNK EXPORT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/kb/export")
async def export_chunks(
    request: Request,
    include_vectors: bool = Query(default=True),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=3000),
    source: str = Query(default=None),
):
    logger.info(
        "[KB/EXPORT] /kb/export — include_vectors=%s  offset=%d  limit=%d  source=%s",
        include_vectors, offset, limit, source or "(all)",
    )
    t0 = time.perf_counter()

    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    raw  = bm25._chunks

    if source:
        raw = [c for c in raw if c.get("source", "") == source]

    total_chunks = len(raw)
    page_raw     = raw[offset : offset + limit]
    has_more     = (offset + limit) < total_chunks

    etag        = await run_in_threadpool(vs.get_export_etag)
    client_etag = request.headers.get("If-None-Match", "")

    if offset == 0 and not source and client_etag and client_etag == etag:
        logger.info("[KB/EXPORT] 304 Not Modified (etag=%s...)", etag[:12])
        return Response(status_code=304, headers={"X-Export-Etag": etag})

    id_to_vec: dict = {}
    print(include_vectors, etag, client_etag)
    if include_vectors:
        print("DEBUG: include_vectors=True, fetching vectors for export...")
        async with _vec_cache_lock:
            if _vec_cache.get("etag") == etag:
                id_to_vec = _vec_cache["data"]
                logger.debug(
                    "[KB/EXPORT] Vector cache HIT (etag=%s...)  %d vectors",
                    etag[:8], len(id_to_vec),
                )
            else:
                logger.info(
                    "[KB/EXPORT] Vector cache MISS (etag=%s...) — scrolling Qdrant",
                    etag[:8],
                )
                t_v = time.perf_counter()
                id_to_vec = await run_in_threadpool(vs.get_vectors_for_export)
                logger.info(
                    "[KB/EXPORT] Qdrant scroll done in %.0f ms — %d vectors",
                    (time.perf_counter() - t_v) * 1000, len(id_to_vec),
                )
                _vec_cache["etag"] = etag
                _vec_cache["data"] = id_to_vec

    chunks  = []
    matched = 0
    for c in page_raw:
        source_val = c.get("source",  "")
        page_val   = c.get("page",    0)
        content    = c.get("content", "")

        chunk_id  = _content_hash(source_val, page_val, content)
        embedding = id_to_vec.get(chunk_id)
        if embedding is not None:
            matched += 1

        chunks.append({
            "id":             chunk_id,
            "source":         source_val,
            "content":        content,
            "parent_content": c.get("parent_content") or content,
            "parent_id":      c.get("parent_id", ""),
            "page":           page_val,
            "chunk_type":     c.get("type", "text"),
            "section_path":   c.get("section_path", ""),
            "heading":        c.get("heading",       ""),
            "bbox":           c.get("bbox"),
            "page_width":     c.get("page_width"),
            "page_height":    c.get("page_height"),
            "embedding":      embedding,
        })


    # Deduplicate chunks by id to prevent sending duplicate vector entries
    seen_ids = set()
    deduped = []
    for chunk in chunks:
        cid = chunk["id"]
        if cid not in seen_ids:
            seen_ids.add(cid)
            deduped.append(chunk)
    chunks = deduped

    # Update matched count to reflect the deduplicated page
    matched = sum(1 for c in chunks if c.get("embedding") is not None)

    logger.info(
        "[KB/EXPORT] page ready offset=%d  size=%d  total=%d  "
        "has_more=%s  vectors=%d  %.0f ms",
        offset, len(chunks), total_chunks, has_more,
        matched, (time.perf_counter() - t0) * 1000,
    )

    response = JSONResponse(content={
        "chunks":   chunks,
        "total":    total_chunks,
        "offset":   offset,
        "limit":    limit,
        "has_more": has_more,
        "etag":     etag,
    })
    response.headers["X-Export-Etag"] = etag
    return response


# ─────────────────────────────────────────────────────────────────────────────
# DIFF
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/kb/diff")
async def kb_diff(request: Request):
    body = await request.json()
    client_hashes: dict = body.get("sources", {})

    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    raw  = bm25._chunks

    etag = await run_in_threadpool(vs.get_export_etag)

    async with _source_hash_lock:
        if _source_hash_cache.get("etag") == etag:
            server_hashes = _source_hash_cache["data"]
            logger.debug(
                "[KB/DIFF] Source hash cache HIT (etag=%s...)  %d sources",
                etag[:8], len(server_hashes),
            )
        else:
            logger.info(
                "[KB/DIFF] Source hash cache MISS (etag=%s...) — computing",
                etag[:8],
            )
            t0 = time.perf_counter()

            chunks_by_source: dict = {}
            for c in raw:
                src = c.get("source", "")
                chunks_by_source.setdefault(src, []).append(c.get("content", ""))

            server_hashes = {}
            for src, contents in chunks_by_source.items():
                per_chunk = sorted(
                    hashlib.md5(t.encode()).hexdigest() for t in contents
                )
                combined = "|".join(per_chunk)
                server_hashes[src] = hashlib.md5(combined.encode()).hexdigest()

            _source_hash_cache["etag"] = etag
            _source_hash_cache["data"] = server_hashes

            logger.info(
                "[KB/DIFF] Source hashes computed in %.0f ms — %d sources",
                (time.perf_counter() - t0) * 1000, len(server_hashes),
            )

    server_sources = set(server_hashes.keys())
    client_sources = set(client_hashes.keys())

    to_delete = sorted(client_sources - server_sources)
    to_add    = sorted(
        src for src in server_sources
        if server_hashes[src] != client_hashes.get(src)
    )
    unchanged = sorted(
        src for src in server_sources
        if server_hashes[src] == client_hashes.get(src)
    )

    logger.info(
        "[KB/DIFF] diff result — to_add=%d  to_delete=%d  unchanged=%d",
        len(to_add), len(to_delete), len(unchanged),
    )

    return JSONResponse({
        "to_add":         to_add,
        "to_delete":      to_delete,
        "unchanged":      unchanged,
        "server_sources": server_hashes,
    })


# ─────────────────────────────────────────────────────────────────────────────
# EXISTING ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse)
async def stats():
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
    from routers.ingest import _wipe_hashes   # local import — avoids circular dep
    rag_service.get_vector_store().reset_collection()
    rag_service.get_bm25_store().reset()
    _wipe_hashes()
    _vec_cache.clear()
    _source_hash_cache.clear()
    logger.info("[KB/COLLECTION] ✅ Knowledge base wiped")
    return WipeResponse(status="ok", message="Knowledge base wiped.")


@router.get("/kb/debug-hash")
async def debug_hash():
    logger.warning(
        "[KB/DEBUG] /kb/debug-hash called — diagnostic endpoint, remove before production"
    )
    vs   = rag_service.get_vector_store()
    bm25 = rag_service.get_bm25_store()
    raw  = bm25._chunks

    qdrant_map = await run_in_threadpool(vs.get_vectors_for_export)

    bm25_keys    = set()
    bm25_samples = []
    for c in raw[:5]:
        src = c.get("source", "")
        pg  = c.get("page",   0)
        ct  = c.get("content","")
        key = _content_hash(src, pg, ct)
        bm25_keys.add(key)
        bm25_samples.append({
            "source":    repr(src),
            "page":      repr(pg),
            "content_80": repr(ct[:80]),
            "parent_id": c.get("parent_id", ""),
            "hash":      key,
            "in_qdrant": key in qdrant_map,
        })

    qdrant_samples = []
    for k in list(qdrant_map.keys())[:5]:
        qdrant_samples.append({"hash": k, "in_bm25": k in bm25_keys})

    overlap = len(bm25_keys & set(qdrant_map.keys()))

    logger.info(
        "[KB/DEBUG] debug-hash: bm25=%d  qdrant=%d  overlap(first5)=%d",
        len(raw), len(qdrant_map), overlap,
    )

    return {
        "bm25_total":              len(raw),
        "qdrant_total":            len(qdrant_map),
        "overlap_of_first_5_bm25": overlap,
        "bm25_samples":            bm25_samples,
        "qdrant_samples":          qdrant_samples,
    }