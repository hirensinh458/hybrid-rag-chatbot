# retrieval/hybrid_retriever.py
#
# CHANGES vs Day 3 version:
#   Person A — Phase 4 (Day 5-6)
#
#   ONE CHANGE ONLY — retrieve() now accepts an optional `store` parameter.
#
#   WHY:
#     The hybrid manager in rag_service keeps two store singletons:
#     _local_store and _cloud_store. get_vector_store() returns whichever
#     is active (cloud if online, local otherwise).
#
#     The chain was built once at startup with _local_store passed to
#     HybridRetriever constructor. Once built, self.store was always local —
#     even when online and cloud was preferred.
#
#     Simplest fix: accept an optional `store` override in retrieve() so the
#     caller (rag_chain._retrieve) can pass rag_service.get_vector_store()
#     on each call. No constructor changes, no chain rebuild needed.
#
#   HOW:
#     retrieve(query, ..., store=None)
#     active_store = store or self.store
#
#     rag_chain._retrieve() passes:
#         import services.rag_service as _rag_svc
#         retrieval = self.retriever.retrieve(..., store=_rag_svc.get_vector_store())
#
#   Everything else (RRF, BM25, MMR, expand_to_parents) unchanged.

import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from retrieval.bm25_store      import BM25Store
from retrieval.naive_retriever import RetrievalResult
from vectorstore.base          import BaseVectorStore
from vectorstore.qdrant_store  import QdrantVectorStore
from embeddings.embedder       import BaseEmbedder, EmbedderFactory
from config                    import TOP_K, RRF_K

# ── NEW: Logger for diagnostic retrieval comparison ────────────────────────
from utils.logger import get_logger
_log = get_logger("retrieval.hybrid_retriever")


# ─────────────────────────────────────────────────────────
# COSINE SIMILARITY HELPER
# ─────────────────────────────────────────────────────────

def _cosine_sim(a: list | np.ndarray, b: list | np.ndarray) -> float:
    """
    Cosine similarity between two embedding vectors.

    Uses numpy for efficiency since embeddings are typically 384-dim floats.
    Returns 0.0 if either vector is zero-norm (degenerate case).

    Args:
        a, b: embedding vectors (list or 1-D numpy array)

    Returns:
        float in [-1.0, 1.0], typically [0.0, 1.0] for sentence embeddings
    """
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)

    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return float(np.dot(a, b) / (norm_a * norm_b))


# ─────────────────────────────────────────────────────────
# RECIPROCAL RANK FUSION
# ─────────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    dense_results  : list[dict],
    sparse_results : list[dict],
    k              : int   = RRF_K,
    dense_weight   : float = 1.0,
    sparse_weight  : float = 1.0,
) -> list[dict]:
    """
    Merge dense (vector) and sparse (BM25) results with Reciprocal Rank Fusion.

    RRF score for a chunk: sum of weight / (k + rank) across each list
    where the chunk appears. Chunks appearing in both lists are boosted.

    The deduplication key is full content string (not [:200] truncation)
    to avoid false collisions on chunks that start identically.
    """
    rrf_scores: dict[str, float] = {}
    chunk_map : dict[str, dict]  = {}

    def _key(chunk: dict) -> str:
        return chunk.get("content", "").strip()

    for rank, chunk in enumerate(dense_results, start=1):
        key             = _key(chunk)
        rrf_scores[key] = rrf_scores.get(key, 0.0) + dense_weight / (k + rank)
        chunk_map[key]  = chunk

    for rank, chunk in enumerate(sparse_results, start=1):
        key             = _key(chunk)
        rrf_scores[key] = rrf_scores.get(key, 0.0) + sparse_weight / (k + rank)
        if key not in chunk_map:
            chunk_map[key] = chunk

    sorted_keys = sorted(rrf_scores, key=lambda x: rrf_scores[x], reverse=True)

    fused: list[dict] = []
    for key in sorted_keys:
        chunk              = chunk_map[key].copy()
        chunk["rrf_score"] = round(rrf_scores[key], 6)
        chunk["score"]     = chunk["rrf_score"]
        fused.append(chunk)

    return fused


# ─────────────────────────────────────────────────────────
# HYBRID RETRIEVER
# ─────────────────────────────────────────────────────────

class HybridRetriever:
    """
    Hybrid retriever: Dense (cosine) + Sparse (BM25) fused with RRF.

    Online pipeline (is_offline=False):
        embed query
        → BM25 + vector search
        → RRF fusion
        → MMR deduplication
        → return child chunks
        [caller: rerank children → expand_to_parents → send to LLM]

    Offline pipeline (is_offline=True):
        embed query
        → BM25 + vector search
        → RRF fusion
        → MMR deduplication  ← ensures 5 diverse manual sections
        → return child chunks directly to worker
        (no reranker, no parent expansion)

    A2 CHANGE (Day 3):
        _deduplicate() replaced by _mmr_deduplicate().

    A4 CHANGE (Day 2):
        _expand_to_parents() not called inside retrieve().
        Use expand_to_parents(retrieval) AFTER reranking in online mode.

    PHASE 4 CHANGE (Person A):
        retrieve() accepts optional `store` parameter.
        Enables rag_chain to pass rag_service.get_vector_store() per call
        so online/offline store switching is transparent.
    """

    def __init__(
        self,
        vector_store   : BaseVectorStore = None,
        embedder       : BaseEmbedder    = None,
        top_k          : int             = TOP_K,
        rrf_k          : int             = RRF_K,
        dense_weight   : float           = 1.0,
        sparse_weight  : float           = 1.0,
        deduplicate    : bool            = True,
        score_threshold: float           = 0.0,
        bm25_path      : str             = None,
        parent_store                     = None,   # ignored — kept for compat
        use_mmr        : bool            = True,
        mmr_threshold  : float           = 0.70,
    ):
        self.embedder        = embedder or EmbedderFactory.get("huggingface")
        self.store           = vector_store or QdrantVectorStore(embedder=self.embedder)
        self.top_k           = top_k
        self.rrf_k           = rrf_k
        self.dense_weight    = dense_weight
        self.sparse_weight   = sparse_weight
        self.deduplicate     = deduplicate
        self.score_threshold = score_threshold
        self.use_mmr         = use_mmr
        self.mmr_threshold   = mmr_threshold

        from pathlib import Path
        from config import settings
        default_bm25_path = str(Path(settings.qdrant_path).parent / "bm25.pkl")
        self.bm25 = BM25Store(path=bm25_path or default_bm25_path)

        print(
            f"  [HYBRID] Ready. "
            f"top_k={top_k} | rrf_k={rrf_k} | "
            f"dense={dense_weight} | sparse={sparse_weight} | "
            f"mmr={'✅' if use_mmr else '❌'} (threshold={mmr_threshold}) | "
            f"parent_expansion=post-rerank (A4) | "
            f"dynamic_store=✅ (Phase 4)"
        )

    # ── INDEX ─────────────────────────────────────────────

    def index_chunks(self, chunks: list[dict]) -> None:
        self.bm25.build(chunks)

    def add_chunks(self, chunks: list[dict]) -> None:
        self.bm25.add(chunks)

    # ── CORE RETRIEVAL ────────────────────────────────────

    def retrieve(
        self,
        query        : str,
        top_k        : int             = None,
        filter_field : str             = None,
        filter_value : str             = None,
        is_offline   : bool            = False,
        store        : BaseVectorStore = None,    # ← NEW (Phase 4)
    ) -> RetrievalResult:
        """
        Run hybrid retrieval and return RAW CHILD CHUNKS.

        A2 CHANGE: dedup step is now _mmr_deduplicate().
        A4 CHANGE: _expand_to_parents() not called here.
        PHASE 4 CHANGE: optional `store` parameter overrides self.store.

        Args:
            query        : search string
            top_k        : override instance default
            filter_field : optional metadata field to filter by
            filter_value : value to match for filter_field
            is_offline   : affects log message
            store        : optional BaseVectorStore override. When provided,
                           this store is used for the dense search instead of
                           self.store. rag_chain._retrieve() passes
                           rag_service.get_vector_store() here so online/offline
                           store switching works without rebuilding the chain.

        Returns:
            RetrievalResult — child chunks, MMR-diverse, sorted by RRF score
        """
        k       = top_k or self.top_k
        fetch_k = max(k * 3, 20)

        # ── PHASE 4: use the provided store override if given ──────────────
        # This is the only line that changed from the original retrieve().
        # self.store is the local store (set at construction).
        # `store` will be the cloud store when online — passed by rag_chain.
        active_store = store or self.store

        # 1. Embed query
        q_vec = self.embedder.embed_text(query)

        # 2. Dense search (uses active_store — may be cloud or local)
        if filter_field and filter_value:
            dense_results = active_store.search_with_filter(
                query_vector = q_vec,
                filter_by    = filter_field,
                filter_val   = filter_value,
                top_k        = fetch_k,
            )
        else:
            dense_results = active_store.search(
                query_vector = q_vec,
                top_k        = fetch_k,
            )

        # 3. BM25 search (always local — BM25 index is local)
        sparse_results = self.bm25.search(query=query, top_k=fetch_k)

        # ── NEW: Log raw candidate counts and a few samples ───────────────
        mode_str = "OFFLINE" if is_offline else "ONLINE"
        store_tag = "cloud" if (store and store is not self.store) else "local"
        _log.debug(
            "[HYBRID/RETRIEVE] %s | store=%s | dense=%d  sparse=%d  fetch_k=%d",
            mode_str, store_tag, len(dense_results), len(sparse_results), fetch_k,
        )
        # Log first 3 dense + sparse results for traceability
        for i, d in enumerate(dense_results[:3]):
            _log.debug(
                "[HYBRID/RETRIEVE] DENSE[%d] src=%s p=%s score=.4f content_preview=%r",
                i, d.get("source", "?"), d.get("page", "?"),
                d.get("score", 0.0),
                (d.get("content", "")[:60]).replace("\n", " "),
            )
        for i, s in enumerate(sparse_results[:3]):
            _log.debug(
                "[HYBRID/RETRIEVE] SPARSE[%d] src=%s p=%s score=%.4f content_preview=%r",
                i, s.get("source", "?"), s.get("page", "?"),
                s.get("score", 0.0),
                (s.get("content", "")[:60]).replace("\n", " "),
            )

        # 4. RRF fusion
        fused = reciprocal_rank_fusion(
            dense_results  = dense_results,
            sparse_results = sparse_results,
            k              = self.rrf_k,
            dense_weight   = self.dense_weight,
            sparse_weight  = self.sparse_weight,
        )

        # ── NEW: Log fused list (before MMR) ──────────────────────────────
        _log.debug(
            "[HYBRID/RETRIEVE] Fused %d chunks (before MMR)", len(fused)
        )
        for i, f in enumerate(fused[:5]):
            _log.debug(
                "[HYBRID/RETRIEVE] FUSED[%d] src=%s p=%s rrf=%.6f content_preview=%r",
                i, f.get("source", "?"), f.get("page", "?"),
                f.get("rrf_score", 0.0),
                (f.get("content", "")[:60]).replace("\n", " "),
            )

        # 5. Score threshold filter
        if self.score_threshold > 0:
            fused = [r for r in fused if r["score"] >= self.score_threshold]

        # 6. Dedup + diversity (MMR)
        if self.deduplicate:
            fused = self._mmr_deduplicate(
                chunks    = fused,
                query_vec = q_vec,
                top_k     = k,
            )
        else:
            fused = fused[:k]

        # ── NEW: Log final MMR‑filtered list ──────────────────────────────
        _log.debug(
            "[HYBRID/RETRIEVE] After MMR: %d chunks (mode=%s store=%s)",
            len(fused), mode_str, store_tag,
        )
        for i, f in enumerate(fused):
            _log.debug(
                "[HYBRID/RETRIEVE] FINAL[%d] src=%s p=%s score=%.4f content_preview=%r",
                i, f.get("source", "?"), f.get("page", "?"),
                f.get("score", 0.0),
                (f.get("content", "")[:60]).replace("\n", " "),
            )

        mode = "offline" if is_offline else "online (rerank + expand in chain)"
        store_tag = "cloud" if (store and store is not self.store) else "local"
        print(f"  [HYBRID] {len(fused)} child chunks ({mode}, store={store_tag})")
        return RetrievalResult(fused)

    # ── MMR DEDUPLICATION ─────────────────────────────────

    def _mmr_deduplicate(
        self,
        chunks    : list[dict],
        query_vec : list | np.ndarray,
        top_k     : int,
    ) -> list[dict]:
        """
        Two-stage deduplication: exact match first, then MMR semantic diversity.

        Stage 1 — Exact dedup (O(n) hash set):
            Removes chunks with identical content strings.

        Stage 2 — MMR greedy selection:
            Batch-embeds all remaining candidates in one forward pass.
            Greedily accepts chunks that are semantically diverse from
            everything already in the accepted set.

            Selection rule:
                max_sim = max cosine_sim(candidate, s) for s in accepted
                Accept candidate if max_sim < mmr_threshold (default 0.70)

        Backfill:
            If fewer than top_k chunks survive MMR, backfill from the
            remainder (diversity is moot when options are scarce).

        Fallback:
            If embedding candidates fails, fall back to score cutoff.
        """
        if not chunks:
            return []

        # Stage 1: exact dedup
        seen_content: set        = set()
        unique      : list[dict] = []
        for c in chunks:
            content = c.get("content", "").strip()
            if content not in seen_content:
                seen_content.add(content)
                unique.append(c)

        if len(unique) <= top_k:
            return unique

        if not self.use_mmr:
            return unique[:top_k]

        # Stage 2: MMR semantic dedup
        try:
            texts          = [c["content"] for c in unique]
            candidate_vecs = self.embedder.embed_documents(texts)
            candidate_vecs = [np.asarray(v, dtype=np.float32) for v in candidate_vecs]
        except Exception as e:
            print(f"  [HYBRID/MMR] Embedding failed ({e}) — falling back to score cutoff")
            return unique[:top_k]

        accepted_indices: list[int]        = []
        accepted_vecs   : list[np.ndarray] = []

        for i, (chunk, vec) in enumerate(zip(unique, candidate_vecs)):
            if len(accepted_indices) >= top_k:
                break

            if not accepted_indices:
                accepted_indices.append(i)
                accepted_vecs.append(vec)
                continue

            max_sim = max(_cosine_sim(vec, av) for av in accepted_vecs)

            if max_sim < self.mmr_threshold:
                accepted_indices.append(i)
                accepted_vecs.append(vec)

        # Backfill
        if len(accepted_indices) < top_k:
            accepted_set    = set(accepted_indices)
            remaining_count = top_k - len(accepted_indices)
            backfill        = [
                i for i in range(len(unique))
                if i not in accepted_set
            ][:remaining_count]
            accepted_indices.extend(backfill)
            if backfill:
                print(
                    f"  [HYBRID/MMR] Backfilled {len(backfill)} chunk(s) — "
                    f"all candidates exceeded similarity threshold"
                )

        result = [unique[i] for i in accepted_indices]
        print(
            f"  [HYBRID/MMR] {len(unique)} unique → "
            f"{len(result)} MMR-diverse (threshold={self.mmr_threshold})"
        )
        return result

    # ── PARENT EXPANSION ──────────────────────────────────

    def expand_to_parents(self, retrieval: RetrievalResult) -> RetrievalResult:
        """
        PUBLIC wrapper — expands child chunks to parent context.

        Called by rag_chain._retrieve() in ONLINE mode, AFTER reranking.
        """
        expanded = self._expand_to_parents(retrieval.get_chunks())
        print(f"  [HYBRID] Parent expansion: {len(retrieval)} → {len(expanded)} passages")
        return RetrievalResult(expanded)

    def _expand_to_parents(self, chunks: list[dict]) -> list[dict]:
        """
        Replace each child's content with its parent_content if available.
        Deduplicates on parent_id.
        """
        expanded     : list[dict] = []
        seen_parents : set        = set()

        for child in chunks:
            parent_id      = child.get("parent_id", "")
            parent_content = child.get("parent_content", "")

            if parent_content and parent_id:
                if parent_id in seen_parents:
                    continue
                seen_parents.add(parent_id)
                merged            = {k: v for k, v in child.items()}
                merged["content"] = parent_content
                expanded.append(merged)
            else:
                expanded.append(child)

        return expanded

    # ── HELPERS ───────────────────────────────────────────

    def get_context(self, query: str, **kwargs) -> str:
        return self.retrieve(query, **kwargs).to_context_string()

    def get_info(self) -> dict:
        return {
            "type"          : "HybridRetriever",
            "top_k"         : self.top_k,
            "rrf_k"         : self.rrf_k,
            "dense_weight"  : self.dense_weight,
            "sparse_weight" : self.sparse_weight,
            "deduplicate"   : self.deduplicate,
            "mmr_enabled"   : self.use_mmr,
            "mmr_threshold" : self.mmr_threshold,
            "bm25_docs"     : len(self.bm25),
            "parent_mode"   : "post-rerank-expansion (A4)",
            "dynamic_store" : "✅ (Phase 4)",
        }


__all__ = ["reciprocal_rank_fusion", "HybridRetriever"]