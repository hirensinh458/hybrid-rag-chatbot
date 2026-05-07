# chains/rag_chain.py
#
# CHANGES vs previous version (Day 2 — A4):
#   - _retrieve() restructured: rerank children FIRST, expand to parents AFTER.
#
#   OLD flow (broken):
#       retrieve()        → children + expand → parent blobs (1500 tok)
#       reranker.rerank() → scores parent blobs  ← low precision
#
#   NEW flow (A4):
#       retrieve()                       → raw child chunks (300 tok)
#       reranker.rerank()                → top-N children  ← precise signal
#       retriever.expand_to_parents()    → parent blobs (1500 tok) for LLM
#
# ── BUG 2 FIX — Online mode citations are inaccurate ─────────────────────
#   Deduplicate citations on parent_id to eliminate multiple children from
#   the same parent appearing as separate citation cards.
#
# ── BUG 3 FIX — Offline mode shows unreadable 300-char child fragments ───
#   Use parent_content instead of raw child content in offline OfflineChunks.
#
# LOGGING CHANGES:
#   - get_logger(__name__) replaces all print() calls.
#   - question text is logged at DEBUG (not INFO) to avoid leaking user PII
#     to INFO-level log aggregators.  Override with LOG_LEVEL=DEBUG locally.
#   - All timing-sensitive paths (retrieval, rerank, LLM stream) log elapsed
#     milliseconds so performance regressions are immediately visible.
#   - Fallback triggers (empty context, low score) are logged at WARNING so
#     they stand out — they indicate retrieval degradation.
# ──────────────────────────────────────────────────────────────────────────

import os
import sys
import time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retrieval.naive_retriever  import NaiveRetriever, RetrievalResult
from retrieval.hybrid_retriever import HybridRetriever
from retrieval.reranker         import Reranker
from generation.groq_llm        import BaseLLM, ChatHistory, LLMFactory
from vectorstore.qdrant_store   import QdrantVectorStore, BaseVectorStore
from embeddings.embedder        import EmbedderFactory
from schemas                    import OfflineQueryResponse, OfflineChunk
from config                     import TOP_K, MIN_RERANK_SCORE, settings
from utils.logger               import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────
# PROMPT TEMPLATES
# ─────────────────────────────────────────────────────────

RAG_SYSTEM_PROMPT = """\
You are a precise ship manual assistant. Answer questions based on the provided manual sections.

Rules:
1. Answer strictly from the provided context. Do not invent facts not in the context.
2. For follow-up questions referencing previous turns, use the conversation history.
3. Be concise and direct. No padding or filler phrases.
4. Do NOT write a 'Sources:' or 'References:' section — citations are handled separately.
5. Preserve technical terminology exactly as it appears in the source.
6. Always format tabular data as a markdown table using | col | col | syntax."""

RAG_USER_TEMPLATE = """\
Context:
{context}

Question: {question}"""

GENERAL_FALLBACK_PROMPT = """\
You are a helpful ship manual assistant. The provided manual sections do not contain
relevant information to answer this question.

Rules:
1. Start with one short sentence noting the manuals didn't cover this topic.
2. If you have general knowledge on it, answer from that.
3. If you don't, say so honestly.
4. Be concise."""


# ─────────────────────────────────────────────────────────
# CHAIN RESPONSE
# ─────────────────────────────────────────────────────────

class ChainResponse:
    """
    Wraps the full output of an online RAG chain call.
    query_type is always "document" now (no router).
    """

    def __init__(
        self,
        answer    : str,
        retrieval : RetrievalResult,
        question  : str,
        model     : str,
        usage     : dict = None,
        query_type: str  = "document",
    ):
        self.answer     = answer
        self.retrieval  = retrieval
        self.question   = question
        self.model      = model
        self.usage      = usage or {}
        self.query_type = query_type

    def get_answer(self) -> str:
        return self.answer

    def get_citations(self) -> list[dict]:
        """
        Return one citation per unique parent passage (deduplicated on parent_id).

        BUG 2 FIX:
        Previously every chunk in the retrieval result produced its own
        citation, so a single parent passage retrieved via 3 different child
        chunks would appear three times.  Now we track seen parent_ids and
        only emit the first citation for each unique parent.

        Fallback key: if parent_id is absent (e.g. naive / atomic chunks),
        we fall back to (source, page) to avoid duplication there too.
        """
        citations: list[dict] = []
        seen: set[str]        = set()

        for chunk in self.retrieval.get_chunks():
            # Build a deduplication key — prefer parent_id for hierarchical
            # chunks; fall back to source+page for other chunker strategies.
            dedup_key = chunk.get("parent_id") or (
                f"{chunk.get('source', '')}|{chunk.get('page', '')}"
            )

            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            citations.append({
                "source"      : chunk.get("source",       "unknown"),
                "page"        : chunk.get("page",          "?"),
                "heading"     : chunk.get("heading",       ""),
                "section_path": chunk.get("section_path",  ""),
                "type"        : chunk.get("type",          "text"),
            })

        logger.debug(
            "get_citations: %d unique citations from %d chunks",
            len(citations),
            len(self.retrieval.get_chunks()),
        )
        return citations

    def get_images(self) -> list[str]:
        return self.retrieval.get_images()

    def has_images(self) -> bool:
        return len(self.get_images()) > 0

    def get_chunks(self) -> list[dict]:
        return self.retrieval.get_chunks()

    def get_context(self) -> str:
        return self.retrieval.to_context_string()

    def __repr__(self) -> str:
        return (
            f"ChainResponse("
            f"model={self.model}, "
            f"query_type={self.query_type}, "
            f"chunks={len(self.retrieval)}, "
            f"tokens={self.usage.get('total_tokens', '?')})"
        )


# ─────────────────────────────────────────────────────────
# RAG CHAIN
# ─────────────────────────────────────────────────────────

class RAGChain:
    """
    Simplified RAG pipeline for offline-capable ship manual lookup.

    Online  (is_online=True):
        Retrieve children → Rerank children → Expand to parents → LLM stream
    Offline (is_online=False):
        Retrieve children (no rerank, no expand) → OfflineQueryResponse with chunk cards
        No LLM call, no SSE — just JSON with precise manual excerpts.

    A4 KEY CHANGE:
        _retrieve() now separates reranking from parent expansion.
        Reranker runs on 300-token children (precise), then expansion
        gives the LLM 1500-token parent passages (context). Both win.
    """

    def __init__(
        self,
        llm           : BaseLLM        = None,
        vector_store  : BaseVectorStore = None,
        retriever                       = None,
        reranker      : Reranker        = None,
        use_reranker  : bool            = True,
        retrieve_top_k: int             = TOP_K,
        rerank_top_k  : int             = 5,
        cite_sources  : bool            = True,
        llm_provider  : str             = "groq",
    ):
        logger.info("[RAG CHAIN] Initialising RAGChain...")

        # ── LLM ───────────────────────────────────────────
        self.llm = llm or LLMFactory.get(llm_provider)
        self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
        logger.info("[RAG CHAIN] LLM: %s", self.llm.model_name)

        # ── Vector store ──────────────────────────────────
        embedder   = EmbedderFactory.get("huggingface")
        self.store = vector_store or QdrantVectorStore(embedder=embedder)
        logger.info(
            "[RAG CHAIN] Vector store: %s", type(self.store).__name__
        )

        # ── Retriever ─────────────────────────────────────
        if retriever is not None:
            self.retriever = retriever
        else:
            self.retriever = HybridRetriever(
                vector_store = self.store,
                embedder     = embedder,
                top_k        = retrieve_top_k,
            )
        logger.info(
            "[RAG CHAIN] Retriever: %s  top_k=%d",
            type(self.retriever).__name__,
            retrieve_top_k,
        )

        # ── Reranker ──────────────────────────────────────
        self.use_reranker = use_reranker
        self.reranker     = reranker or (Reranker() if use_reranker else None)
        self.rerank_top_k = rerank_top_k
        self.parent_rerank_top_k = settings.parent_rerank_top_k
        logger.info(
            "[RAG CHAIN] Reranker: %s  (children first — A4 flow)  "
            "rerank_top_k=%d → parent_rerank_top_k=%d",
            "enabled" if use_reranker else "disabled",
            rerank_top_k,
            self.parent_rerank_top_k,
        )

        # ── Settings ──────────────────────────────────────
        self.retrieve_top_k  = retrieve_top_k
        self.cite_sources    = cite_sources
        self._source_filter  : str | None = None

        # ── Memory ────────────────────────────────────────
        self.history = self.llm.history

        logger.info("[RAG CHAIN] ✅ Ready! Online/offline branching enabled.")

    # ── INDEXING ──────────────────────────────────────────

    def index_documents(self, chunks: list[dict]) -> None:
        """Index document chunks into the vector store and retriever."""
        logger.info("[RAG CHAIN] Indexing %d chunks into vector store...", len(chunks))
        t0 = time.perf_counter()
        self.store.add_documents(chunks)
        if hasattr(self.retriever, "index_chunks"):
            self.retriever.index_chunks(chunks)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "[RAG CHAIN] ✅ Indexed %d chunks in %.0f ms",
            len(chunks), elapsed,
        )

    # ── RETRIEVAL ─────────────────────────────────────────

    def _retrieve(self, question: str, is_offline: bool = False) -> RetrievalResult:
        """
        Run retrieval with the correct child-first / parent-expand order.

        OFFLINE mode:
            retrieve() → child chunks (300 tok) → return directly
            No reranking (CPU-intensive), no parent expansion.

        ONLINE mode (A4 fix):
            retrieve() → child chunks (300 tok)
            [optional] rerank children → precise cross-encoder signal
            expand_to_parents()        → 1500-tok passages for LLM context

        PHASE 4 CHANGE:
            store=rag_service.get_vector_store() passed to retriever.retrieve()
            so online calls use the cloud store and offline calls use local,
            without rebuilding the chain on network state changes.
        """
        mode_label = "OFFLINE" if is_offline else "ONLINE"
        logger.debug(
            "[RAG CHAIN] _retrieve() — mode=%s  filter=%s  question_len=%d chars",
            mode_label,
            self._source_filter or "none",
            len(question),
        )

        # ── Phase 4: resolve active store on every call ────────────────────
        # Imported inside the method to avoid circular import
        # (rag_service imports RAGChain at module level).
        import services.rag_service as _rag_svc
        active_store = _rag_svc.get_vector_store()
        store_tag = (
            "cloud"
            if (active_store and active_store is not self.store)
            else "local"
        )
        logger.debug(
            "[RAG CHAIN] Active vector store for this query: %s (%s)",
            type(active_store).__name__,
            store_tag,
        )

        t_retrieve = time.perf_counter()
        retrieval = self.retriever.retrieve(
            question,
            filter_field = "source" if self._source_filter else None,
            filter_value = self._source_filter,
            is_offline   = is_offline,
            store        = active_store,
        )

        # ── Log raw retrieval chunks (before rerank) for online/offline comparison ──
        mode_tag = "OFFLINE" if is_offline else "ONLINE"
        raw_chunks = retrieval.get_chunks()
        logger.info(
            "[RAG CHAIN] Raw retrieval (before rerank): %d chunks | mode=%s",
            len(raw_chunks), mode_tag,
        )
        for i, c in enumerate(raw_chunks):
            logger.info(
                "[RAG CHAIN] RAW[%d] src=%s p=%s score=%.4f parent_id=%s content_preview=%r",
                i,
                c.get("source", "?"),
                c.get("page", "?"),
                c.get("score", 0.0),
                c.get("parent_id", "")[:12] if c.get("parent_id") else "(none)",
                c.get("content", "")[:60].replace("\n", " "),
            )
        # ────────────────────────────────────────────────────────────────────────── 


        elapsed_retrieve = (time.perf_counter() - t_retrieve) * 1000
        logger.info(
            "[RAG CHAIN] Retrieval: %d chunks in %.0f ms (store=%s)",
            len(retrieval), elapsed_retrieve, store_tag,
        )

        # ── OFFLINE: return child chunks directly ──────────────────────────
        if is_offline:
            logger.info(
                "[RAG CHAIN] Offline mode — returning %d child chunks "
                "(no rerank, no parent expansion)",
                len(retrieval),
            )
            return retrieval

        # ── ONLINE RERANK #1: rerank children (precise, 300-tok fragments) ─
        if self.use_reranker and self.reranker and len(retrieval) > 0:
            logger.info(
                "[RAG CHAIN] Rerank #1 — scoring %d children (300-tok fragments)...",
                len(retrieval),
            )
            t_rerank1 = time.perf_counter()
            retrieval = self.reranker.rerank(
                query     = question,
                retrieval = retrieval,
                top_k     = self.rerank_top_k,        # e.g. 20 → 10
            )
            elapsed_rerank1 = (time.perf_counter() - t_rerank1) * 1000
            logger.info(
                "[RAG CHAIN] Rerank #1 done — kept %d children  (%.0f ms)",
                len(retrieval), elapsed_rerank1,
            )

        # ── Expand top-N reranked children to parent passages ─────────────
        if hasattr(self.retriever, "expand_to_parents"):
            t_expand = time.perf_counter()
            pre_expand = len(retrieval)
            retrieval = self.retriever.expand_to_parents(retrieval)
            elapsed_expand = (time.perf_counter() - t_expand) * 1000
            logger.info(
                "[RAG CHAIN] Parent expansion: %d → %d passages  (%.0f ms)",
                pre_expand, len(retrieval), elapsed_expand,
            )
        else:
            logger.warning(
                "[RAG CHAIN] expand_to_parents not available on %s — skipping",
                type(self.retriever).__name__,
            )

        # ── ONLINE RERANK #2: rerank parents (full context, 1500-tok) ─────
        # Cross-encoder now scores the FULL parent passage against the query,
        # not the small child fragment. This is the definitive relevance signal.
        if self.use_reranker and self.reranker and len(retrieval) > 0:
            logger.info(
                "[RAG CHAIN] Rerank #2 — scoring %d parent passages (1500-tok)...",
                len(retrieval),
            )
            t_rerank2 = time.perf_counter()
            retrieval = self.reranker.rerank(
                query     = question,
                retrieval = retrieval,
                top_k     = self.parent_rerank_top_k, # e.g. 10 → 5
            )
            elapsed_rerank2 = (time.perf_counter() - t_rerank2) * 1000
            logger.info(
                "[RAG CHAIN] Rerank #2 done — kept %d parent passages for LLM  (%.0f ms)",
                len(retrieval), elapsed_rerank2,
            )

        return retrieval

    # ── PROMPT BUILDING ───────────────────────────────────

    def _build_prompt(self, question: str, context: str) -> str:
        return RAG_USER_TEMPLATE.format(context=context, question=question)

    # ── ASK (blocking, online only) ───────────────────────

    def ask(self, question: str, has_kb: bool = True) -> ChainResponse:
        """
        Blocking online RAG pipeline.
        Always assumes online — use stream() for online/offline branching.
        """
        logger.info("[RAG CHAIN] ask() — has_kb=%s", has_kb)
        t0 = time.perf_counter()

        # No KB available — use general fallback prompt
        if not has_kb:
            logger.warning(
                "[RAG CHAIN] No KB documents — falling back to general LLM response"
            )
            self.llm.set_system_prompt(GENERAL_FALLBACK_PROMPT)
            result = self.llm.generate(
                prompt   = question,
                history  = self.history,
                store_as = question,
            )
            self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "[RAG CHAIN] General fallback response generated in %.0f ms "
                "(tokens: prompt=%d  completion=%d  total=%d)",
                elapsed,
                result.get("usage", {}).get("prompt_tokens",     0),
                result.get("usage", {}).get("completion_tokens", 0),
                result.get("usage", {}).get("total_tokens",      0),
            )
            return ChainResponse(
                answer     = result["content"],
                retrieval  = RetrievalResult([]),
                question   = question,
                model      = result["model"],
                usage      = result["usage"],
                query_type = "general",
            )

        # ── Retrieve + optional rerank ────────────────────
        retrieval = self._retrieve(question, is_offline=False)
        context   = retrieval.to_context_string()
        best_score = retrieval.best_score() if len(retrieval) > 0 else 0.0
        logger.debug(
            "[RAG CHAIN] Context length: %d chars  best_rerank_score=%.4f",
            len(context), best_score,
        )

        # ── Weak context / low score fallback ────────────
        if not context.strip() or (
            self.use_reranker
            and best_score < MIN_RERANK_SCORE
        ):
            logger.warning(
                "[RAG CHAIN] ⚠ Weak/empty context (best_score=%.4f < threshold=%.4f) — "
                "falling back to general LLM response",
                best_score, MIN_RERANK_SCORE,
            )
            self.llm.set_system_prompt(GENERAL_FALLBACK_PROMPT)
            result = self.llm.generate(
                prompt   = question,
                history  = self.history,
                store_as = question,
            )
            self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "[RAG CHAIN] Fallback response generated in %.0f ms (no KB context used)",
                elapsed,
            )
            return ChainResponse(
                answer     = result["content"],
                retrieval  = RetrievalResult([]),
                question   = question,
                model      = result["model"],
                usage      = result["usage"],
                query_type = "general",
            )

        # ── Document-grounded RAG response ───────────────
        logger.info(
            "[RAG CHAIN] Building RAG prompt from %d passage(s), context=%d chars",
            len(retrieval), len(context),
        )
        self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
        prompt = self._build_prompt(question, context)
        t_llm = time.perf_counter()
        result = self.llm.generate(
            prompt   = prompt,
            history  = self.history,
            store_as = question,
        )
        elapsed_llm = (time.perf_counter() - t_llm) * 1000
        elapsed_total = (time.perf_counter() - t0) * 1000
        logger.info(
            "[RAG CHAIN] ✅ ask() complete — LLM=%.0f ms  total=%.0f ms  "
            "tokens(prompt=%d  completion=%d  total=%d)",
            elapsed_llm, elapsed_total,
            result.get("usage", {}).get("prompt_tokens",     0),
            result.get("usage", {}).get("completion_tokens", 0),
            result.get("usage", {}).get("total_tokens",      0),
        )
        return ChainResponse(
            answer     = result["content"],
            retrieval  = retrieval,
            question   = question,
            model      = result["model"],
            usage      = result["usage"],
            query_type = "document",
        )

    # ── STREAM (generator, online OR offline) ─────────────

    def stream(self, question: str, has_kb: bool = True, is_online: bool = True):
        """
        Main entry point — handles both online and offline modes.

        Online  → yields str tokens then final ChainResponse (SSE streaming)
        Offline → yields a single OfflineQueryResponse (normal JSON, no SSE)

        Args:
            question  : user's question
            has_kb    : whether any documents are indexed
            is_online : network status from NetworkMonitor / rag_service

        Yields:
            str              — text tokens (online only)
            ChainResponse    — final metadata object (online only)
            OfflineQueryResponse — all chunks at once (offline only)
        """
        mode = "ONLINE" if is_online else "OFFLINE"
        logger.info(
            "[RAG CHAIN] stream() — mode=%s  has_kb=%s  question_len=%d",
            mode, has_kb, len(question),
        )
        t0 = time.perf_counter()

        # ── OFFLINE BRANCH ────────────────────────────────
        if not is_online:
            if not has_kb:
                logger.warning(
                    "[RAG CHAIN] OFFLINE + no KB — returning empty OfflineQueryResponse"
                )
                yield OfflineQueryResponse(
                    query      = question,
                    chunks     = [],
                    total      = 0,
                    is_offline = True,
                )
                return

            offline_top_k = settings.offline_top_k
            # _retrieve with is_offline=True returns child chunks directly
            logger.info(
                "[RAG CHAIN] OFFLINE retrieval — fetching top %d chunks",
                offline_top_k,
            )
            retrieval = self._retrieve(question, is_offline=True)
            chunks    = retrieval.get_chunks()[:offline_top_k]
            logger.info(
                "[RAG CHAIN] OFFLINE retrieval complete — %d chunks selected",
                len(chunks),
            )

            offline_chunks = [
                OfflineChunk(
                    source       = c.get("source", "unknown"),
                    page         = c.get("page"),
                    heading      = c.get("heading", ""),
                    section_path = c.get("section_path", ""),
                    # BUG 3 FIX: use parent_content (full 1500-char passage)
                    # instead of raw child content (300-char fragment).
                    content      = c.get("parent_content") or c.get("content", ""),
                    score        = round(float(c.get("score", 0.0)), 4),
                    chunk_type   = c.get("type", "text"),
                    bbox         = c.get("bbox"),
                    page_width   = c.get("page_width"),
                    page_height  = c.get("page_height"),
                )
                for c in chunks
            ]

            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "[RAG CHAIN] ✅ OFFLINE stream() complete — %d chunks, %.0f ms",
                len(offline_chunks), elapsed,
            )
            yield OfflineQueryResponse(
                query      = question,
                chunks     = offline_chunks,
                total      = len(offline_chunks),
                is_offline = True,
            )
            return

        # ── ONLINE BRANCH ─────────────────────────────────

        # No KB — general fallback
        if not has_kb:
            logger.warning(
                "[RAG CHAIN] ONLINE + no KB — streaming general fallback response"
            )
            self.llm.set_system_prompt(GENERAL_FALLBACK_PROMPT)
            full_reply: list[str] = []
            usage: dict = {}
            for chunk in self.llm.stream(
                prompt   = question,
                history  = self.history,
                store_as = question,
            ):
                if isinstance(chunk, str):
                    full_reply.append(chunk)
                    yield chunk
                else:
                    usage = chunk.get("usage", {})
            self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "[RAG CHAIN] General fallback stream done — %.0f ms  tokens=%d",
                elapsed, usage.get("total_tokens", 0),
            )
            yield ChainResponse(
                answer     = "".join(full_reply),
                retrieval  = RetrievalResult([]),
                question   = question,
                model      = self.llm.model_name,
                usage      = usage,
                query_type = "general",
            )
            return

        # ── Retrieve + rerank children + expand to parents (online — A4) ─
        retrieval = self._retrieve(question, is_offline=False)
        context   = retrieval.to_context_string()
        best_score = retrieval.best_score() if len(retrieval) > 0 else 0.0
        logger.debug(
            "[RAG CHAIN] Context: %d chars  best_score=%.4f",
            len(context), best_score,
        )

        # Weak context fallback
        if not context.strip() or (
            self.use_reranker
            and best_score < MIN_RERANK_SCORE
        ):
            logger.warning(
                "[RAG CHAIN] ⚠ Weak context (best_score=%.4f) — "
                "streaming general fallback (no document grounding)",
                best_score,
            )
            self.llm.set_system_prompt(GENERAL_FALLBACK_PROMPT)
            full_reply = []
            usage = {}
            for chunk in self.llm.stream(
                prompt   = question,
                history  = self.history,
                store_as = question,
            ):
                if isinstance(chunk, str):
                    full_reply.append(chunk)
                    yield chunk
                else:
                    usage = chunk.get("usage", {})
            self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(
                "[RAG CHAIN] Fallback stream done — %.0f ms  tokens=%d",
                elapsed, usage.get("total_tokens", 0),
            )
            yield ChainResponse(
                answer     = "".join(full_reply),
                retrieval  = RetrievalResult([]),
                question   = question,
                model      = self.llm.model_name,
                usage      = usage,
                query_type = "general",
            )
            return

        # ── RAG-grounded stream ───────────────────────────
        logger.info(
            "[RAG CHAIN] Building RAG prompt — %d passage(s)  context=%d chars",
            len(retrieval), len(context),
        )
        self.llm.set_system_prompt(RAG_SYSTEM_PROMPT)
        prompt     = self._build_prompt(question, context)
        full_reply = []
        usage: dict = {}
        token_count = 0

        t_stream = time.perf_counter()
        for chunk in self.llm.stream(
            prompt   = prompt,
            history  = self.history,
            store_as = question,
        ):
            if isinstance(chunk, str):
                full_reply.append(chunk)
                token_count += 1
                yield chunk
            else:
                # Final metadata dict from the LLM
                usage = chunk.get("usage", {})

        elapsed_stream = (time.perf_counter() - t_stream) * 1000
        elapsed_total  = (time.perf_counter() - t0) * 1000
        logger.info(
            "[RAG CHAIN] ✅ ONLINE stream() complete — "
            "stream=%.0f ms  total=%.0f ms  "
            "tokens(prompt=%d  completion=%d  total=%d)",
            elapsed_stream, elapsed_total,
            usage.get("prompt_tokens",     0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens",      0),
        )

        yield ChainResponse(
            answer     = "".join(full_reply),
            retrieval  = retrieval,
            question   = question,
            model      = self.llm.model_name,
            usage      = usage,
            query_type = "document",
        )

    # ── SOURCE FILTER (pin) ───────────────────────────────

    def set_source_filter(self, filename: str) -> None:
        """Pin retrieval to a single source file."""
        logger.info("[RAG CHAIN] Source filter set → '%s'", filename)
        self._source_filter = filename

    def clear_source_filter(self) -> None:
        """Remove the source pin."""
        logger.info("[RAG CHAIN] Source filter cleared (searching all documents)")
        self._source_filter = None

    def get_source_filter(self) -> str | None:
        return self._source_filter

    # ── MEMORY ────────────────────────────────────────────

    def reset_memory(self) -> None:
        """Clear conversation history and rolling summary."""
        logger.info("[RAG CHAIN] Conversation memory reset")
        self.llm.reset_history()

    def get_history(self) -> list[dict]:
        return self.history.to_messages()

    # ── INFO ──────────────────────────────────────────────

    def get_info(self) -> dict:
        return {
            "llm"           : self.llm.get_info(),
            "retriever"     : type(self.retriever).__name__,
            "reranker"      : self.reranker.get_info() if self.reranker else None,
            "retrieve_top_k": self.retrieve_top_k,
            "rerank_top_k"  : self.rerank_top_k,
            "cite_sources"  : self.cite_sources,
            "history_turns" : len(self.history),
            "vector_store"  : self.store.get_stats(),
            "last_query_type": "document",
        }


__all__ = ["ChainResponse", "RAGChain"]