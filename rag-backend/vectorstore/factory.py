# vectorstore/factory.py
#
# Phase 1 — Multi-Tenancy Foundation
#
# CHANGES vs previous version:
#   - get_vector_store() now accepts an optional `tenant_slug` parameter.
#   - When tenant_slug is provided, the Qdrant collection name becomes
#     rag_docs_{tenant_slug}, giving full collection-level tenant isolation.
#   - LanceDB and ChromaDB vendors similarly scope their table/collection
#     name using the slug when provided.
#   - Falls back to settings.qdrant_collection (single-tenant dev mode) when
#     tenant_slug is None — fully backward compatible.
#
# All other behaviour is UNCHANGED.
#
# Usage:
#   from vectorstore.factory import get_vector_store
#
#   # Single-tenant / dev mode (unchanged)
#   local_store = get_vector_store(vendor="qdrant", mode="local", embedder=emb)
#
#   # Multi-tenant (new)
#   tenant_store = get_vector_store(vendor="qdrant", mode="local",
#                                   embedder=emb, tenant_slug="acme_shipping")
#   # → uses Qdrant collection "rag_docs_acme_shipping"

from __future__ import annotations

from vectorstore.base import BaseVectorStore


def get_vector_store(
    vendor      : str    = None,
    mode        : str    = "local",
    embedder    : object = None,
    tenant_slug : str    = None,    # NEW — scopes collection/table per tenant
    **kwargs,
) -> BaseVectorStore:
    """
    Instantiate and return the configured vector store.

    Args:
        vendor:       "qdrant" | "lancedb" | "chroma".
                      Defaults to settings.vector_store_vendor.
        mode:         "local" | "cloud".
        embedder:     BaseEmbedder instance. If None, the store creates its own.
        tenant_slug:  Optional tenant identifier. When provided:
                        - Qdrant    → collection  "rag_docs_{tenant_slug}"
                        - LanceDB   → table        "rag_docs_{tenant_slug}"
                        - ChromaDB  → collection  "rag_docs_{tenant_slug}"
                      When None, falls back to settings.qdrant_collection
                      (single-tenant dev mode — backward compatible).
        **kwargs:     Passed through to the store constructor for overrides
                      (e.g. path=, collection_name=, uri=).

    Returns:
        Configured BaseVectorStore instance.

    Raises:
        ValueError:   if vendor is unrecognised.
        ImportError:  if the vendor's package is not installed.
    """
    from config import settings

    _vendor = (vendor or settings.vector_store_vendor).lower().strip()

    # Derive a scoped collection / table name for this tenant.
    # Falls back to the global default when no slug is given (dev / single-tenant).
    def _collection_name(default: str) -> str:
        return f"rag_docs_{tenant_slug}" if tenant_slug else default

    if _vendor == "qdrant":
        from vectorstore.qdrant_store import QdrantVectorStore

        collection = _collection_name(settings.qdrant_collection)
        return QdrantVectorStore(
            embedder        = embedder,
            mode            = mode,
            collection_name = collection,   # scoped per tenant
            **kwargs,
        )

    elif _vendor == "lancedb":
        from vectorstore.lancedb_store import LanceDBVectorStore

        table_name = _collection_name(settings.qdrant_collection)  # reuse slug logic
        return LanceDBVectorStore(
            embedder   = embedder,
            mode       = mode,
            table_name = table_name,        # scoped per tenant
            **kwargs,
        )

    elif _vendor in ("chroma", "chromadb"):
        from vectorstore.chroma_store import ChromaVectorStore

        collection = _collection_name(settings.qdrant_collection)
        return ChromaVectorStore(
            embedder        = embedder,
            mode            = mode,
            collection_name = collection,   # scoped per tenant
            **kwargs,
        )

    else:
        raise ValueError(
            f"Unknown vector store vendor: '{_vendor}'. "
            f"Choose from: qdrant, lancedb, chroma."
        )


__all__ = ["get_vector_store"]