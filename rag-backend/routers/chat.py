# routers/chat.py
#
# CHANGES vs previous version:
#   - JWT auth dependency REMOVED from all routes — no auth needed.
#   - get_or_create_session() replaced with get_chain() (single shared chain).
#   - Online path  → SSE stream (same as before).
#   - Offline path → run retrieval only, return OfflineQueryResponse as normal
#     JSON response. No SSE needed since there is no LLM streaming.
#   - /session/pin and /session/clear still work, operate on shared chain.
#
# B-Phase 3: Offline Reranker
#   - When ENABLE_OFFLINE_RERANKER=true in .env, the offline path now applies
#     the cross-encoder reranker between retrieval and response building.
#   - Retrieval always fetches top_k=settings.top_k candidates (default 20).
#   - With reranker ON : cross-encoder rescores + keeps reranker_top_k (default 5)
#   - With reranker OFF: simple slice to offline_top_k (default 5)
#   - get_reranker() accessor used (already added to rag_service.py).
#
# CHANGE — Add /chat/offline endpoint (Mode 2 fix):
#   - Mobile app calls POST /chat/offline which previously returned 404.
#   - This endpoint accepts the same ChatRequest body and forces offline mode.
#   - The offline retrieval + reranker logic is identical to the offline branch
#     in /chat/stream — both paths use the same shared helpers.
#
# LOGGING CHANGES:
#   - Every request logs mode (ONLINE/OFFLINE/MODE2), question length, and
#     KB availability at INFO.
#   - Token streaming is logged at DEBUG — first token arrival marks when
#     the LLM started generating (useful for measuring LLM latency).
#   - Each offline chunk's source/page/score is logged at DEBUG for
#     easy comparison when tuning retrieval quality.
#   - SSE generator exceptions are logged at ERROR with full traceback.
#   - Session/pin operations are logged at INFO.

import json
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from schemas import ChatRequest, ClearRequest, OfflineQueryResponse, OfflineChunk
from services import rag_service
from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])


class PinRequest(BaseModel):
    filename: str


# ── Shared offline retrieval helper ──────────────────────────────────────────
#
# Both /chat/stream (offline branch) and /chat/offline use the exact same
# retrieval + reranker logic.  Extracted here as a plain function to avoid
# duplication.  The caller passes in the already-resolved chain and store so
# this function stays pure (no rag_service calls inside).

def _run_offline_retrieval(question: str, chain, active_store) -> list:
    """
    Run hybrid retrieval (+ optional reranker) and return a list of chunk dicts.

    Parameters
    ----------
    question     : user query string
    chain        : the active RAGChain instance (for retriever + source filter)
    active_store : the local vector store instance

    Returns
    -------
    list[dict]   : final ranked chunks ready to be serialised as OfflineChunk
    """
    logger.info(
        "[CHAT/OFFLINE] Starting offline retrieval — top_k=%d  "
        "reranker=%s  source_filter=%s",
        settings.top_k,
        "enabled" if settings.enable_offline_reranker else "disabled",
        chain.get_source_filter() or "none",
    )
    t0 = time.perf_counter()

    retriever = chain.retriever

    retrieval = retriever.retrieve(
        query        = question,
        top_k        = settings.top_k,          # always fetch full candidate pool (default 20)
        filter_field = "source" if chain.get_source_filter() else None,
        filter_value = chain.get_source_filter(),
        is_offline   = True,
        store        = active_store,
    )
    elapsed_retrieve = (time.perf_counter() - t0) * 1000
    logger.info(
        "[CHAT/OFFLINE] Initial retrieval: %d chunks in %.0f ms",
        len(retrieval), elapsed_retrieve,
    )

    if settings.enable_offline_reranker:
        reranker = rag_service.get_reranker()

        # RERANK #1 — score child chunks (300-tok, precise fragments)
        t_r1 = time.perf_counter()
        reranked = reranker.rerank(
            query     = question,
            retrieval = retrieval,
            top_k     = settings.reranker_top_k,          # e.g. 20 → 10
        )
        elapsed_r1 = (time.perf_counter() - t_r1) * 1000
        logger.info(
            "[CHAT/OFFLINE] Rerank #1 done — %d children kept (%.0f ms)",
            len(reranked), elapsed_r1,
        )

        # EXPAND — replace child content with full parent passage (1500-tok)
        t_expand = time.perf_counter()
        expanded = retriever.expand_to_parents(reranked)
        elapsed_expand = (time.perf_counter() - t_expand) * 1000
        logger.info(
            "[CHAT/OFFLINE] Parent expansion: %d → %d passages (%.0f ms)",
            len(reranked), len(expanded), elapsed_expand,
        )

        # RERANK #2 — score parent passages (1500-tok, full context)
        # The cross-encoder now sees the complete passage, not a fragment.
        t_r2 = time.perf_counter()
        reranked2    = reranker.rerank(
            query     = question,
            retrieval = expanded,
            top_k     = settings.parent_rerank_top_k,     # e.g. 10 → 5
        )
        elapsed_r2 = (time.perf_counter() - t_r2) * 1000
        final_chunks = reranked2.get_chunks()
        logger.info(
            "[CHAT/OFFLINE] Rerank #2 done — %d parent chunks kept (%.0f ms)",
            len(final_chunks), elapsed_r2,
        )
    else:
        final_chunks = retrieval.get_chunks()[:settings.offline_top_k]
        logger.info(
            "[CHAT/OFFLINE] Reranker OFF — returning top %d MMR child chunks "
            "(no parent expansion)",
            len(final_chunks),
        )

    # Log chunk ordering at DEBUG so retrieval quality can be inspected
    for i, c in enumerate(final_chunks):
        if settings.enable_offline_reranker:
            score_label = f"rerank={c.get('rerank_score', '?'):.4f}"
        else:
            score_label = f"rrf={c.get('rrf_score', c.get('score', '?'))}"
        logger.debug(
            "[CHAT/OFFLINE] chunk[%d] %s  src=%s  p=%s  "
            "content_preview=%r",
            i, score_label,
            c.get("source", "?"),
            c.get("page",   "?"),
            c.get("content", "")[:60].replace("\n", " "),
        )

    total_elapsed = (time.perf_counter() - t0) * 1000
    logger.info(
        "[CHAT/OFFLINE] _run_offline_retrieval complete — "
        "%d chunks  total=%.0f ms",
        len(final_chunks), total_elapsed,
    )
    return final_chunks


def _build_offline_response(question: str, final_chunks: list) -> OfflineQueryResponse:
    """
    Convert a list of raw chunk dicts into a fully populated OfflineQueryResponse.
    """
    logger.debug(
        "[CHAT/OFFLINE] Building OfflineQueryResponse from %d chunks",
        len(final_chunks),
    )
    offline_chunks = [
        OfflineChunk(
            source       = c.get("source", "unknown"),
            page         = c.get("page"),
            heading      = c.get("heading", ""),
            section_path = c.get("section_path", ""),
            content      = c.get("parent_content") or c.get("content", ""),
            score        = round(float(c.get("score", 0.0)), 4),
            chunk_type   = c.get("type", "text"),
            bbox         = c.get("bbox"),
            page_width   = c.get("page_width"),
            page_height  = c.get("page_height"),
        )
        for c in final_chunks
    ]

    return OfflineQueryResponse(
        query      = question,
        chunks     = offline_chunks,
        total      = len(offline_chunks),
        is_offline = True,
    )


# ── Chat stream (online) / chunk response (offline) ──────────

@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Online  → SSE stream of tokens then a done event with citations.
    Offline → Normal JSON response (OfflineQueryResponse) with manual sections.
              No SSE needed — there is no LLM streaming in offline mode.
    """
    vector_store = rag_service.get_vector_store()
    has_kb       = vector_store.count() > 0
    chain        = rag_service.get_chain()
    online       = rag_service.is_online()

    logger.info(
        "[CHAT] /chat/stream — mode=%s  has_kb=%s  question_len=%d",
        "ONLINE" if online else "OFFLINE",
        has_kb,
        len(req.question),
    )

    # ── OFFLINE ───────────────────────────────────────────
    if not online:
        if not has_kb:
            logger.warning("[CHAT] OFFLINE + no KB — returning empty response")
            result = OfflineQueryResponse(
                query      = req.question,
                chunks     = [],
                total      = 0,
                is_offline = True,
            )
            return JSONResponse(content=result.model_dump())

        # Import active store so offline path uses local store (same as chain)
        active_store = rag_service.get_local_store()
        logger.info(
            "[CHAT] OFFLINE — using local store (%s)  count=%d",
            type(active_store).__name__,
            active_store.count(),
        )

        t0 = time.perf_counter()
        final_chunks = _run_offline_retrieval(req.question, chain, active_store)
        result       = _build_offline_response(req.question, final_chunks)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "[CHAT] ✅ OFFLINE response ready — %d chunks  %.0f ms",
            result.total, elapsed,
        )
        return JSONResponse(content=result.model_dump())

    # ── ONLINE (SSE stream) ────────────────────────────────
    logger.info("[CHAT] ONLINE — starting SSE stream...")

    async def event_generator():
        t_start = time.perf_counter()
        token_count = 0
        first_token_logged = False

        try:
            for chunk in chain.stream(req.question, has_kb=has_kb, is_online=True):
                if isinstance(chunk, str):
                    # ── Token received ────────────────────
                    if not first_token_logged:
                        first_token_logged = True
                        logger.debug(
                            "[CHAT] First token received at %.0f ms",
                            (time.perf_counter() - t_start) * 1000,
                        )
                    token_count += 1
                    yield f"data: {json.dumps({'token': chunk})}\n\n"

                else:
                    # ── Final ChainResponse ───────────────
                    is_document = chunk.query_type == "document"
                    citations   = []
                    image_urls  = []

                    if is_document:
                        citations = [
                            {
                                "source"      : c.get("source", ""),
                                "page"        : c.get("page"),
                                "heading"     : c.get("heading", ""),
                                "section_path": c.get("section_path", ""),
                                "chunk_type"  : c.get("type", "text"),
                                "bbox"        : c.get("bbox"),
                                "page_width"  : c.get("page_width"),
                                "page_height" : c.get("page_height"),
                                "source_url"  : c.get("source_url", ""),
                            }
                            for c in chunk.get_citations()
                        ]
                        image_urls = [
                            f"/images/{Path(p).name}"
                            for p in chunk.get_images()
                        ]

                    # ── ONLINE RETRIEVAL CHUNKS LOG (INFO level for comparison) ──
                    retrieval_chunks = chunk.retrieval.get_chunks()
                    logger.info(
                        "[CHAT/ONLINE] Final retrieval chunks passed to LLM: %d chunks",
                        len(retrieval_chunks)
                    )

                    # ── SEMANTIC SEARCH ONLY LOG ──────────────────
                    # Access raw retrieval before rerank from chain
                    import services.rag_service as _rs
                    chain2 = _rs.get_chain()
                    q_vec = chain2.retriever.embedder.embed_text(req.question)
                    logger.info(
                        "[CHAT/ONLINE/SEMANTIC] Query embedding first 10: %s",
                        q_vec[:10]
                    )
                    logger.info(
                        "[CHAT/ONLINE/SEMANTIC] Query embedding norm: %.4f",
                        sum(v*v for v in q_vec) ** 0.5
                    )
                    # Log the raw dense results from rag_chain._retrieve
                    # These are already logged at INFO as [RAG CHAIN] RAW[...]
                    # ──────────────────────────────────────────────────

                    for i, rc in enumerate(retrieval_chunks):
                        logger.info(
                            "[CHAT/ONLINE] chunk[%d] src=%s p=%s score=%.4f parent_id=%s content_preview=%r",
                            i,
                            rc.get("source", "?"),
                            rc.get("page", "?"),
                            rc.get("rerank_score", rc.get("score", 0.0)),
                            rc.get("parent_id", "")[:12],
                            rc.get("content", "")[:80].replace("\n", " "),
                        )
                    # ────────────────────────────────────────────────────────────

                    elapsed = (time.perf_counter() - t_start) * 1000
                    logger.info(
                        "[CHAT] ✅ ONLINE stream complete — "
                        "query_type=%s  citations=%d  images=%d  "
                        "tokens_streamed=%d  total=%.0f ms  "
                        "usage(prompt=%d  completion=%d  total=%d)",
                        chunk.query_type,
                        len(citations),
                        len(image_urls),
                        token_count,
                        elapsed,
                        chunk.usage.get("prompt_tokens",     0),
                        chunk.usage.get("completion_tokens", 0),
                        chunk.usage.get("total_tokens",      0),
                    )

                    yield f"data: {json.dumps({'done': True, 'citations': citations, 'image_urls': image_urls, 'query_type': chunk.query_type, 'usage': chunk.usage})}\n\n"

        except Exception as e:
            elapsed = (time.perf_counter() - t_start) * 1000
            logger.error(
                "[CHAT] ❌ SSE stream error after %.0f ms: %s",
                elapsed, e, exc_info=True,
            )
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── Intranet-only / forced offline endpoint ───────────────────────────────────

@router.post("/chat/offline")
async def chat_offline(req: ChatRequest):
    """
    Intranet-only mode (Mode 2):
    Server is reachable via LAN but there is no internet for Groq.
    Runs retrieval-only pipeline and returns OfflineQueryResponse as plain JSON.
    No SSE, no LLM call.

    This endpoint exists as an explicit alternative to /chat/stream so the
    mobile app can force offline retrieval regardless of what the server's
    NetworkMonitor reports.  The mobile app always calls /chat/offline when
    it detects Mode 2 (server reachable, no internet).

    The retrieval + reranker logic is identical to the offline branch inside
    /chat/stream — both share the same _run_offline_retrieval() helper above.
    """
    chain        = rag_service.get_chain()
    active_store = rag_service.get_local_store()
    has_kb       = active_store.count() > 0

    logger.info(
        "[CHAT/OFFLINE] /chat/offline (Mode 2) — has_kb=%s  store=%s(%d)  question_len=%d",
        has_kb,
        type(active_store).__name__,
        active_store.count(),
        len(req.question),
    )

    if not has_kb:
        logger.warning("[CHAT/OFFLINE] Mode 2 — no KB documents, returning empty response")
        result = OfflineQueryResponse(
            query      = req.question,
            chunks     = [],
            total      = 0,
            is_offline = True,
        )
        return JSONResponse(content=result.model_dump())

    t0 = time.perf_counter()
    final_chunks = _run_offline_retrieval(req.question, chain, active_store)
    result       = _build_offline_response(req.question, final_chunks)
    elapsed = (time.perf_counter() - t0) * 1000
    logger.info(
        "[CHAT/OFFLINE] ✅ Mode 2 response ready — %d chunks  %.0f ms",
        result.total, elapsed,
    )
    return JSONResponse(content=result.model_dump())


# ── Session management ────────────────────────────────────────

@router.post("/session/clear")
async def clear_session(req: ClearRequest):
    logger.info("[CHAT] Session clear requested")
    rag_service.clear_chain_memory()
    logger.info("[CHAT] ✅ Conversation history cleared")
    return {"status": "ok"}


# ── Pin / unpin ───────────────────────────────────────────────

@router.post("/session/pin")
async def pin_source(req: PinRequest):
    """Pin the chain to a single source file."""
    chain   = rag_service.get_chain()
    sources = rag_service.get_vector_store().list_sources()

    logger.info(
        "[CHAT] Pin request — filename='%s'  available_sources=%d",
        req.filename, len(sources),
    )

    if req.filename not in sources:
        logger.warning(
            "[CHAT] Pin failed — '%s' not found in knowledge base (available: %s)",
            req.filename, sources,
        )
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"File '{req.filename}' not found in the knowledge base.",
        )

    chain.set_source_filter(req.filename)
    logger.info("[CHAT] ✅ Pinned to source: '%s'", req.filename)
    return {"status": "ok", "pinned": req.filename}


@router.delete("/session/pin")
async def unpin_source():
    """Remove the source pin."""
    logger.info("[CHAT] Unpin requested — removing source filter")
    rag_service.get_chain().clear_source_filter()
    logger.info("[CHAT] ✅ Source pin cleared")
    return {"status": "ok", "pinned": None}


@router.get("/session/pin")
async def get_pin():
    """Return the currently pinned filename, or null."""
    pinned = rag_service.get_chain().get_source_filter()
    logger.debug("[CHAT] Current pin: %s", pinned or "none")
    return {"pinned": pinned}