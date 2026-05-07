# services/rag_service.py
#
# CHANGE vs previous version:
#
#   NEW FUNCTION: delete_file_from_cloud(filename)
#
#     Deletes a file's vectors from the CLOUD store only.
#     Also removes BM25 entries immediately (local side-effect cleanup).
#     Does NOT touch _local_store — local vectors are cleaned up by the
#     next sync run via the cloud→local diff.
#
#     Called by: routers/ingest.py DELETE /ingest/{filename}
#     Preconditions enforced by the router before calling this:
#       - is_online() is True
#       - get_cloud_store() is not None
#       - filename exists in cloud_store.list_sources()
#
#   KEPT UNCHANGED: delete_file_from_stores(filename)
#     Still deletes from _local_store + BM25.
#     Used by wipe endpoint and internal tools only.
#
#   Also carries forward the BM25 rebuild fix:
#     new_bm25.build(chunks) not new_bm25.index_chunks(chunks)
#
# All other code is UNCHANGED from the original.

import threading
from pathlib import Path

from config import settings
from embeddings.embedder        import EmbedderFactory
from generation.groq_llm        import LLMFactory
from retrieval.bm25_store       import BM25Store
from retrieval.hybrid_retriever import HybridRetriever
from retrieval.reranker         import Reranker
from vectorstore.base           import BaseVectorStore
from vectorstore.factory        import get_vector_store as _factory_get_store
from chains.rag_chain           import RAGChain


# ── Singletons ────────────────────────────────────────────────────────────────
_embedder        : object = None
_reranker        : object = None
_local_store     : object = None
_cloud_store     : object = None
_bm25_store      : object = None
_chain           : object = None
_network_monitor : object = None

_lock = threading.Lock()
_tasks: dict = {}


# ── HYBRID STORE ACCESSOR ─────────────────────────────────────────────────────

def get_vector_store() -> BaseVectorStore:
    if _cloud_store is not None and is_online():
        return _cloud_store
    return _local_store


# ── FACTORIES ─────────────────────────────────────────────────────────────────

def _build_local_store(embedder) -> BaseVectorStore:
    vendor = settings.vector_store_vendor
    print(f"  [SERVICE] Local vector store : {vendor} (local mode)")
    return _factory_get_store(vendor=vendor, mode="local", embedder=embedder)


def _build_cloud_store(embedder) -> BaseVectorStore | None:
    vendor = settings.vector_store_vendor

    if vendor == "qdrant" and not settings.qdrant_cloud_url:
        print("  [SERVICE] Cloud store skipped — QDRANT_CLOUD_URL not set")
        return None
    if vendor == "lancedb" and not settings.lancedb_cloud_uri:
        print("  [SERVICE] Cloud store skipped — LANCEDB_CLOUD_URI not set")
        return None
    if vendor in ("chroma", "chromadb") and not settings.chroma_host:
        print("  [SERVICE] Cloud store skipped — CHROMA_HOST not set")
        return None

    print(f"  [SERVICE] Cloud vector store : {vendor} (cloud mode)")
    try:
        return _factory_get_store(vendor=vendor, mode="cloud", embedder=embedder)
    except Exception as e:
        print(f"  [SERVICE] ⚠  Cloud store init failed: {e} — falling back to local only")
        return None


def _build_embedder():
    print("  [SERVICE] Embedder     : huggingface (bge-small)")
    return EmbedderFactory.get("huggingface")


def _build_llm():
    provider = settings.llm_provider.lower().strip()

    if provider == "ollama":
        import generation.ollama_llm  # noqa: F401
        print(f"  [SERVICE] LLM          : Ollama local (model='{settings.ollama_model}')")
        return LLMFactory.get("ollama")

    if provider == "groq" and not settings.groq_api_key:
        raise RuntimeError("LLM_PROVIDER=groq but GROQ_API_KEY is not set in .env")

    print(f"  [SERVICE] LLM          : Groq cloud (model='{settings.groq_model}')")
    return LLMFactory.get("groq")


def _build_chunker():
    from ingestion.chunker import ChunkerFactory
    strategy = settings.chunker.lower().strip()
    print(f"  [SERVICE] Chunker      : {strategy}")
    return ChunkerFactory.get(strategy)


# ── STARTUP ───────────────────────────────────────────────────────────────────

async def startup() -> None:
    global _embedder, _reranker, _local_store, _cloud_store, \
           _bm25_store, _chain, _network_monitor

    data_dir = Path(settings.qdrant_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)

    print("\n  [SERVICE] Initialising singletons...")

    _embedder    = _build_embedder()
    _reranker    = Reranker(model_name=settings.reranker_model)

    _local_store = _build_local_store(_embedder)
    _cloud_store = _build_cloud_store(_embedder)

    _bm25_store  = BM25Store(path=str(data_dir / "bm25.pkl"))
    _chain       = _build_chain()

    from services.network_monitor import NetworkMonitor
    _network_monitor = NetworkMonitor(
        check_url     = settings.network_check_url,
        poll_interval = settings.network_poll_interval,
        timeout       = settings.network_check_timeout,
    )
    _network_monitor.start()

    cloud_status = "✅ connected" if _cloud_store else "⬜ not configured"
    print(f"  [SERVICE] Local store  : ✅ ready")
    print(f"  [SERVICE] Cloud store  : {cloud_status}")
    print("  [SERVICE] ✅ All singletons ready\n")


# ── CHAIN ─────────────────────────────────────────────────────────────────────

def _build_chain() -> RAGChain:
    llm = _build_llm()

    retriever = HybridRetriever(
        vector_store = _local_store,
        embedder     = _embedder,
        top_k        = settings.top_k,
    )
    if _bm25_store and _bm25_store._chunks:
        retriever.index_chunks(_bm25_store._chunks)

    return RAGChain(
        llm            = llm,
        vector_store   = _local_store,
        retriever      = retriever,
        reranker       = _reranker,
        use_reranker   = True,
        retrieve_top_k = settings.top_k,
        rerank_top_k   = settings.reranker_top_k,
        # parent_rerank_top_k is read from settings inside RAGChain.__init__
        cite_sources   = True,
    )


def get_chain() -> RAGChain:
    return _chain


def rebuild_chain() -> None:
    global _chain
    with _lock:
        _chain = _build_chain()


def clear_chain_memory() -> None:
    with _lock:
        if _chain:
            _chain.reset_memory()


# ── NETWORK STATUS ────────────────────────────────────────────────────────────

def is_online() -> bool:
    if _network_monitor is None:
        return True
    return _network_monitor.is_online


# ── FILE DELETION ─────────────────────────────────────────────────────────────

def delete_file_from_cloud(filename: str) -> dict:
    """
    Delete a file's vectors from the CLOUD store only.

    This is the correct deletion path when a cloud store is configured.

    WHY cloud only:
      Cloud is the authoritative store. Local is a cache that syncs from cloud.
      If you delete locally, the next sync re-pulls the file from cloud
      (because to_pull = cloud_ids - local_ids will include those points again).
      The only way to permanently remove a file is to remove it from cloud first.
      Local cleanup then happens automatically on the next sync run.

    BM25 is cleaned up immediately because it is a local index and stale
    entries would cause dead results in offline queries right now, not later.

    Local vectors are intentionally left as-is until the next sync.
    During that window, offline mode may still return chunks from the deleted
    file — this is an acceptable trade-off vs the risk of a diverged state.

    Preconditions (enforced by the router):
      - is_online() is True
      - get_cloud_store() is not None
      - filename exists in cloud_store.list_sources()
    """
    # AFTER
    # Step 1: Delete from cloud (authoritative)
    vectors_deleted = _cloud_store.delete_by_source(filename)
    print(
        f"  [SERVICE] ✅ Deleted {vectors_deleted} vectors from cloud "
        f"for '{filename}'"
    )

    # Step 2: Delete from Supabase Storage (if configured)
    # Run before BM25 cleanup so all remote state is cleared first.
    # Failure is logged but does not block the rest of the delete flow.
    try:
        from services.supabase_storage import delete_pdf_from_supabase
        delete_pdf_from_supabase(filename)
    except Exception as _exc:
        print(f"  [SERVICE] ⚠  Supabase delete skipped: {_exc}")

    # Step 3: Remove from BM25 immediately so queries stop returning stale results
    bm25_deleted = _bm25_store.delete_by_source(filename)
    print(f"  [SERVICE] Removed {bm25_deleted} BM25 entries for '{filename}'")

    # Step 4: Update the live retriever's BM25 reference
    with _lock:
        if _chain and hasattr(_chain.retriever, "bm25"):
            _chain.retriever.bm25 = _bm25_store

    return {
        "vectors_deleted": vectors_deleted,
        "bm25_deleted"   : bm25_deleted,
    }


def delete_file_from_stores(filename: str) -> dict:
    """
    Delete from LOCAL store + BM25.

    Kept for internal use: wipe endpoint, admin tools, pure-local deployments.
    NOT used by the user-facing delete endpoint when cloud is configured.
    """
    vectors_deleted = _local_store.delete_by_source(filename)
    bm25_deleted    = _bm25_store.delete_by_source(filename)

    with _lock:
        if _chain and hasattr(_chain.retriever, "bm25"):
            _chain.retriever.bm25 = _bm25_store

    return {
        "vectors_deleted": vectors_deleted,
        "bm25_deleted"   : bm25_deleted,
    }


# ── BM25 REBUILD ─────────────────────────────────────────────────────────────

def rebuild_bm25_async() -> None:
    """
    Rebuild BM25 from local store contents after a vector sync.
    FIX: uses new_bm25.build() — index_chunks() is on HybridRetriever, not BM25Store.
    """
    def _rebuild():
        global _bm25_store
        try:
            print("  [BM25] Starting async BM25 rebuild from local store...")
            all_ids = _local_store.get_all_ids(with_payload_fields=["source"])
            if not all_ids:
                print("  [BM25] Local store empty — skipping BM25 rebuild")
                return

            all_points = _local_store.get_points_by_ids([p["id"] for p in all_ids])
            chunks = [
                {
                    "content": pt["payload"].get("content", ""),
                    "source" : pt["payload"].get("source", ""),
                }
                for pt in all_points
                if pt["payload"].get("content")
            ]

            if not chunks:
                print("  [BM25] No content found — skipping BM25 rebuild")
                return

            data_dir = Path(settings.qdrant_path).parent
            new_bm25 = BM25Store(path=str(data_dir / "bm25.pkl"))
            new_bm25.build(chunks)

            with _lock:
                _bm25_store = new_bm25
                if _chain and hasattr(_chain.retriever, "bm25"):
                    _chain.retriever.bm25 = new_bm25

            print(f"  [BM25] ✅ Rebuilt BM25 index with {len(chunks)} chunks")

        except Exception as e:
            print(f"  [BM25] ⚠  BM25 rebuild failed: {e}")

    threading.Thread(target=_rebuild, daemon=True).start()


# ── CHUNKER ACCESSOR ──────────────────────────────────────────────────────────

def get_chunker():
    return _build_chunker()


# ── SINGLETON ACCESSORS ───────────────────────────────────────────────────────

def get_local_store() -> BaseVectorStore:
    return _local_store

def get_cloud_store() -> BaseVectorStore | None:
    return _cloud_store

def get_bm25_store() -> BM25Store:
    return _bm25_store

def get_reranker() -> Reranker:
    return _reranker

def get_parent_store():
    return None

def get_embedder():
    return _embedder


# ── TASK REGISTRY ─────────────────────────────────────────────────────────────

def set_task(task_id, status, progress=0, message="", result=None):
    _tasks[task_id] = {
        "status"  : status,
        "progress": progress,
        "message" : message,
        "result"  : result or {},
    }

def get_task(task_id: str) -> dict | None:
    return _tasks.get(task_id)