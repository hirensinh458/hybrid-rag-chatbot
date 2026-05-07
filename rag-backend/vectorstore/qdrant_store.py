# vectorstore/qdrant_store.py
#
# Person A — Phase 2 (Day 2-3)
#
# CHANGES vs previous version:
#   1. BaseVectorStore imported from vectorstore.base (not defined inline here).
#      qdrant_store.py now only defines QdrantVectorStore.
#
#   2. Cloud mode added.
#      QdrantVectorStore.__init__ accepts `mode: str = "local"`.
#        mode="local"  → QdrantClient(path=...)          same as before
#        mode="cloud"  → QdrantClient(url=..., api_key=...) Qdrant Cloud
#      All other methods are identical — the Qdrant Python client presents
#      the same API for both modes.
#
#   3. New sync-engine methods:
#        get_all_ids()         — scroll all point IDs + optional payload subset
#        get_points_by_ids()   — fetch full vector + payload for given IDs
#        upsert_from_points()  — upsert pre-computed vectors (no re-embedding)
#        delete_by_ids()       — delete specific point IDs
#
#   4. make_chunk_dict() used in _payload_to_dict() to centralise the
#      key mapping and ensure schema consistency across vendors.
#
# FIX — Cloud write timeout during ingest:
#   PROBLEM:
#     When uploading a large PDF to Qdrant Cloud, ingest crashes with:
#       httpx.WriteTimeout: The write operation timed out
#     Root cause 1: QdrantClient(url=...) uses httpx default timeout (~5s).
#       Sending 100 vectors with large parent_content payloads over the
#       internet easily exceeds this.
#     Root cause 2: batch_size=100 sends too much data per HTTP request.
#       Each point carries a full payload including parent_content (~1500 chars),
#       bbox, section_path etc. 100 × ~3KB ≈ 300KB per request over a cloud
#       connection — this is too large for the default timeout.
#
#   FIX:
#     1. Cloud client: QdrantClient(url=..., api_key=..., timeout=60)
#        60 seconds is generous — gives slow connections plenty of headroom.
#        Local client keeps no explicit timeout (uses library default, fast path).
#
#     2. _upsert_batched(): self.mode-aware batch size.
#        Cloud → batch_size=25   (smaller payload per request, fewer timeouts)
#        Local → batch_size=100  (local socket, large batches are fine)
#
#     Together these eliminate the WriteTimeout for any realistic PDF size.
#     The batch count increases (e.g. 512 vectors → 21 cloud batches vs 6 local)
#     but each request is small and fast, which is more reliable than one
#     large slow request that risks timing out mid-write.
#
# FIX — get_vectors_for_export returned 0 matches:
#   PROBLEM:
#     Qdrant point IDs are random UUIDs (uuid4()) assigned at ingest time.
#     The /kb/export endpoint builds chunk_id from BM25 chunk dicts using
#     c.get("id") or f"{source}_{page}_{i}" — never a UUID.
#     So id_to_vec.get(chunk_id) always returned None → 0 vectors on mobile.
#
#   FIX:
#     Introduce _content_hash(source, page, content) — a stable MD5 key derived
#     from fields present in BOTH BM25 chunks and Qdrant payloads.
#     get_vectors_for_export() now scrolls with_payload=["source","page","content"]
#     and keys by this hash. The /kb/export endpoint builds the SAME hash for
#     each BM25 chunk so the lookup always hits.
#     _content_hash is exported at module level so kb.py can import it directly.

import hashlib
import os
import sys
import uuid
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams,
    PointStruct, PointIdsList,
    Filter, FieldCondition, MatchValue,
)

from embeddings.embedder    import BaseEmbedder, EmbedderFactory
from config                 import QDRANT_COLLECTION, EMBEDDING_DIM, settings
from vectorstore.base       import BaseVectorStore, make_chunk_dict


# ─────────────────────────────────────────────────────────────────────────────
# BATCH SIZE CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Local Qdrant: large batches are fine — data never leaves the machine.
_LOCAL_UPSERT_BATCH  = 100

# Cloud Qdrant: each point carries a large payload (parent_content ~1500 chars,
# bbox, section_path, etc.). 100 × ~3KB ≈ 300KB per HTTP request over the
# internet, which regularly hits httpx's default write timeout.
# 25 points × ~3KB ≈ 75KB per request — well within timeout margins.
_CLOUD_UPSERT_BATCH  = 25

# HTTP timeout in seconds for cloud connections.
# 60s is conservative; most batches complete in < 5s, but slow connections
# or large payloads can be slow. Better to wait than to crash mid-ingest.
_CLOUD_HTTP_TIMEOUT  = 60


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _content_hash(source: str, page: int, content: str) -> str:
    """
    Stable lookup key shared between get_vectors_for_export() and /kb/export.

    WHY: Qdrant point IDs are random UUIDs — BM25 chunks have no knowledge
    of them. We need a key derivable from fields present in BOTH stores.
    source + page + first 80 chars of content is unique for any realistic
    chunk set and cheap to compute on both sides.

    Exported at module level so kb.py can import and use the same function,
    guaranteeing the keys always match without duplication.
    """
    key = f"{source}|{page}|{content[:80]}"
    return hashlib.md5(key.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# QDRANT VECTOR STORE
# ─────────────────────────────────────────────────────────────────────────────

class QdrantVectorStore(BaseVectorStore):
    """
    Qdrant vector store — supports both local (on-disk) and cloud modes.

    Local mode  (default, mode="local"):
        Connects to a local Qdrant file-based DB.
        Used by the ship server and by the phone's local cache layer.
        Config key: QDRANT_PATH

    Cloud mode  (mode="cloud"):
        Connects to Qdrant Cloud via REST.
        Used as the authoritative source for the sync engine.
        Config keys: QDRANT_CLOUD_URL, QDRANT_CLOUD_API_KEY

    The Python client is identical for both modes — only the constructor
    argument differs. This is why Qdrant is the default vendor.

    Each chunk's full canonical dict (see vectorstore.base.CHUNK_KEYS)
    becomes the Qdrant point payload, including:
      - parent_content (stored inline by HierarchicalChunker)
      - bbox / page_width / page_height (stored by PDFLoader)
    """

    def __init__(
        self,
        embedder        : BaseEmbedder = None,
        collection_name : str          = QDRANT_COLLECTION,
        embedding_dim   : int          = EMBEDDING_DIM,
        path            : str          = None,
        mode            : str          = "local",
        cloud_url       : str          = None,
        cloud_api_key   : str          = None,
    ):
        super().__init__(embedder)
        self.collection    = collection_name
        self.embedding_dim = embedding_dim
        self.mode          = mode

        if mode == "cloud":
            _url     = cloud_url     or settings.qdrant_cloud_url
            _api_key = cloud_api_key or settings.qdrant_cloud_api_key
            if not _url:
                raise ValueError(
                    "QdrantVectorStore(mode='cloud') requires QDRANT_CLOUD_URL "
                    "to be set in .env or passed explicitly."
                )
            print(f"\n  [QDRANT] Connecting to Qdrant Cloud at: {_url}")
            # FIX: timeout=60 prevents httpx.WriteTimeout on large PDF ingests.
            # The default httpx timeout (~5s) is too short for cloud upserts
            # with large payloads (parent_content + bbox metadata per point).
            self.client = QdrantClient(
                url     = _url,
                api_key = _api_key,
                timeout = _CLOUD_HTTP_TIMEOUT,
            )
            self.path = None
        else:
            _path = path or settings.qdrant_path
            print(f"\n  [QDRANT] Connecting to local DB at: {_path}")
            # Local: no timeout needed — socket operations don't timeout on loopback
            self.client = QdrantClient(path=_path)
            self.path   = _path

        self._ensure_collection()

    # ── SETUP ─────────────────────────────────────────────────────────────

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self.client.get_collections().collections]
        
        if self.collection not in existing:
            # Create the collection
            self.client.create_collection(
                collection_name = self.collection,
                vectors_config  = VectorParams(
                    size     = self.embedding_dim,
                    distance = Distance.COSINE,
                ),
            )
            print(f"  [QDRANT] Created new collection: '{self.collection}'")

            self.client.create_payload_index(
                collection_name = self.collection,
                field_name      = "source",
                field_schema    = "keyword",      # required for delete_by_source()
            )
            print(f"  [QDRANT] ✅ Created payload index on 'source'")

            # Optional but highly recommended indexes
            self.client.create_payload_index(
                collection_name = self.collection,
                field_name      = "page",
                field_schema    = "integer",
            )
            self.client.create_payload_index(
                collection_name = self.collection,
                field_name      = "type",
                field_schema    = "keyword",
            )
            print(f"  [QDRANT] ✅ Created payload indexes on 'page' and 'type'")

        else:
            print(f"  [QDRANT] Using existing collection: '{self.collection}'")
            
            # Make sure indexes exist even on old collections (safe to call again)
            try:
                self.client.create_payload_index(
                    collection_name = self.collection,
                    field_name      = "source",
                    field_schema    = "keyword",
                )
            except Exception:
                pass  # index already exists

    def reset(self) -> None:
        """Wipe and recreate the collection."""
        self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name = self.collection,
            vectors_config  = VectorParams(
                size     = self.embedding_dim,
                distance = Distance.COSINE,
            ),
        )
        print(f"  [QDRANT] Collection reset: '{self.collection}'")

    # Keep old name as alias so existing call sites don't break.
    def reset_collection(self) -> None:
        self.reset()

    # ── WRITE ─────────────────────────────────────────────────────────────

    def add_documents(self, chunks: list[dict]) -> None:
        """Embed chunks and upsert into the collection."""
        if not chunks:
            print("  [QDRANT] No chunks to add.")
            return

        texts   = [c["content"] for c in chunks]
        vectors = self.embedder.embed_documents(texts)

        points: list[PointStruct] = []
        for chunk, vector in zip(chunks, vectors):
            payload = {k: v for k, v in chunk.items()}
            points.append(
                PointStruct(
                    id      = str(uuid.uuid4()),
                    vector  = vector,
                    payload = payload,
                )
            )

        self._upsert_batched(points)
        print(f"  [QDRANT] ✅ Added {len(points)} vectors to '{self.collection}'")

    def upsert_from_points(self, points: list[dict]) -> None:
        """
        Upsert pre-computed vectors + payloads without re-embedding.

        Each item in `points` must have:
            id      : str
            vector  : list[float]
            payload : dict

        Used by the sync engine to copy cloud points to the local store.
        """
        if not points:
            return

        structs = [
            PointStruct(
                id      = p["id"],
                vector  = p["vector"],
                payload = p["payload"],
            )
            for p in points
        ]
        self._upsert_batched(structs)
        print(f"  [QDRANT] ✅ Upserted {len(structs)} pre-computed points")

    def _upsert_batched(self, points: list[PointStruct]) -> None:
        """
        Upsert points in batches sized appropriately for local vs cloud.

        FIX: batch_size is now mode-aware.
          Cloud: 25 points per request — each point has a large payload
                 (parent_content ~1500 chars + bbox + metadata ≈ 3KB).
                 25 × 3KB = ~75KB per HTTP request — safe for cloud timeouts.
          Local: 100 points per request — local socket, no network latency,
                 large batches are fine and faster overall.

        The `timeout` argument is NOT passed to client.upsert() here because
        it is already set on the QdrantClient instance at construction time
        (timeout=_CLOUD_HTTP_TIMEOUT for cloud). Per-call timeout overrides
        are not needed.
        """
        batch_size = _CLOUD_UPSERT_BATCH if self.mode == "cloud" else _LOCAL_UPSERT_BATCH
        total      = len(points)

        for i in range(0, total, batch_size):
            batch = points[i : i + batch_size]
            self.client.upsert(
                collection_name = self.collection,
                points          = batch,
            )
            # Progress log for large ingests so it's clear the upload is running
            if total > batch_size:
                end = min(i + batch_size, total)
                print(f"  [QDRANT] Upserted {end}/{total} points...")

    def delete_by_source(self, filename: str) -> int:
        """
        Delete all vectors whose payload 'source' field equals filename.
        Returns number of vectors deleted.
        """
        before = self.count()
        self.client.delete(
            collection_name = self.collection,
            points_selector = Filter(
                must=[FieldCondition(
                    key   = "source",
                    match = MatchValue(value=filename),
                )]
            ),
        )
        after   = self.count()
        deleted = before - after
        print(f"  [QDRANT] Deleted {deleted} vectors for source='{filename}'")
        return deleted

    def delete_by_ids(self, ids: list[str]) -> int:
        """
        Delete vectors by their point IDs.
        Returns number of points deleted.
        """
        if not ids:
            return 0
        batch_size = 200
        total_deleted = 0
        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            self.client.delete(
                collection_name = self.collection,
                points_selector = PointIdsList(points=batch),
            )
            total_deleted += len(batch)
        print(f"  [QDRANT] Deleted {total_deleted} vectors by ID")
        return total_deleted

    # ── SYNC ENGINE HELPERS ───────────────────────────────────────────────

    def get_all_ids(
        self,
        with_payload_fields: list[str] = None,
    ) -> list[dict]:
        """
        Scroll through the entire collection and return point IDs (+ optional
        payload fields).

        Uses Qdrant scroll API with `with_vectors=False` for efficiency —
        we only fetch the fields we need for the diff.

        Args:
            with_payload_fields: e.g. ["source", "sha256"]. None → id only.

        Returns:
            List of dicts, e.g.:
            [{"id": "abc-123", "source": "engine.pdf", "sha256": "deadbeef"}, ...]
        """
        payload_selector = with_payload_fields if with_payload_fields else False

        all_points: list[dict] = []
        offset = None

        while True:
            result, next_offset = self.client.scroll(
                collection_name = self.collection,
                limit           = 1000,
                offset          = offset,
                with_vectors    = False,
                with_payload    = payload_selector,
            )

            for pt in result:
                entry = {"id": str(pt.id)}
                if with_payload_fields and pt.payload:
                    for field in with_payload_fields:
                        entry[field] = pt.payload.get(field)
                all_points.append(entry)

            if next_offset is None:
                break
            offset = next_offset

        return all_points

    def get_points_by_ids(self, ids: list[str]) -> list[dict]:
        """
        Fetch full vector + payload for a list of point IDs.

        Returns list of dicts with 'id', 'vector', 'payload' keys —
        ready to feed into upsert_from_points() on another store.
        """
        if not ids:
            return []

        result: list[dict] = []
        batch_size = 100

        for i in range(0, len(ids), batch_size):
            batch = ids[i : i + batch_size]
            points = self.client.retrieve(
                collection_name = self.collection,
                ids             = batch,
                with_vectors    = True,
                with_payload    = True,
            )
            for pt in points:
                result.append({
                    "id"     : str(pt.id),
                    "vector" : list(pt.vector),
                    "payload": dict(pt.payload) if pt.payload else {},
                })

        return result

    # ── PAYLOAD HELPER ────────────────────────────────────────────────────

    @staticmethod
    def _payload_to_dict(r) -> dict:
        """
        Convert a Qdrant search result point into a canonical chunk dict.

        Delegates to make_chunk_dict() from vectorstore.base so the
        key list is maintained in one place across all vendors.
        """
        return make_chunk_dict(r.payload or {}, score=r.score)

    # ── READ ──────────────────────────────────────────────────────────────

    def search(
        self,
        query_vector: list[float],
        top_k       : int = 5,
    ) -> list[dict]:
        results = self.client.query_points(
            collection_name = self.collection,
            query           = query_vector,
            limit           = top_k,
            with_payload    = True,
        ).points
        return [self._payload_to_dict(r) for r in results]

    def search_with_filter(
        self,
        query_vector: list[float],
        filter_by   : str,
        filter_val  : str,
        top_k       : int = 5,
    ) -> list[dict]:
        results = self.client.query_points(
            collection_name = self.collection,
            query           = query_vector,
            query_filter    = Filter(
                must=[FieldCondition(
                    key   = filter_by,
                    match = MatchValue(value=filter_val),
                )]
            ),
            limit        = top_k,
            with_payload = True,
        ).points
        return [self._payload_to_dict(r) for r in results]

    # ── STATS ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        info  = self.client.get_collection(self.collection)
        total = info.points_count or info.vectors_count or 0
        return {
            "collection"   : self.collection,
            "total_vectors": total,
            "dimensions"   : self.embedding_dim,
            "distance"     : "cosine",
            "storage_path" : self.path,
            "mode"         : self.mode,
        }

    def count(self) -> int:
        try:
            return self.client.count(collection_name=self.collection).count
        except Exception:
            return 0

    def list_sources(self) -> list[str]:
        try:
            result = self.client.scroll(
                collection_name = self.collection,
                limit           = 10_000,
                with_payload    = ["source"],
                with_vectors    = False,
            )
            sources = {pt.payload.get("source", "") for pt in result[0]}
            return sorted(s for s in sources if s)
        except Exception:
            return []

    def delete_collection(self) -> None:
        self.client.delete_collection(self.collection)
        print(f"  [QDRANT] Deleted collection: '{self.collection}'")

    # ── MOBILE EXPORT HELPERS ─────────────────────────────────────────────

    def get_vectors_for_export(self) -> dict[str, list[float]]:
        id_to_vec: dict[str, list[float]] = {}
        _sample_logged = False  # log only a few examples to avoid log spam

        try:
            offset = None
            while True:
                results, next_offset = self.client.scroll(
                    collection_name=self.collection,
                    limit=500,
                    offset=offset,
                    with_vectors=True,
                    with_payload=["source", "page", "content"],
                )
                for point in results:
                    if point.vector is None:
                        continue
                    payload = point.payload or {}

                    raw_source  = payload.get("source",  "")
                    raw_page    = payload.get("page",    0)
                    raw_content = payload.get("content", "")

                    # ── DEEP DEBUG ────────────────────────────────────────────
                    if not _sample_logged:
                        _sample_logged = True
                        print(f"\n  [DEBUG/qdrant] === FIRST POINT PAYLOAD SAMPLE ===")
                        print(f"  [DEBUG/qdrant] source type={type(raw_source).__name__!r}  repr={repr(raw_source)}")
                        print(f"  [DEBUG/qdrant] page   type={type(raw_page).__name__!r}  value={raw_page!r}")
                        print(f"  [DEBUG/qdrant] content type={type(raw_content).__name__!r}  first 120 chars repr:")
                        print(f"  [DEBUG/qdrant]   {repr(raw_content[:120])}")
                        print(f"  [DEBUG/qdrant] content[:80] repr for hash: {repr(raw_content[:80])}")
                        _key = f"{raw_source}|{raw_page}|{raw_content[:80]}"
                        print(f"  [DEBUG/qdrant] hash input repr: {repr(_key)}")
                        print(f"  [DEBUG/qdrant] resulting hash : {hashlib.md5(_key.encode()).hexdigest()}")
                        print(f"  [DEBUG/qdrant] ==========================================\n")
                    # ── END DEEP DEBUG ────────────────────────────────────────

                    hash_key = _content_hash(
                        source=raw_source,
                        page=raw_page,
                        content=raw_content,
                    )
                    id_to_vec[hash_key] = point.vector

                if next_offset is None:
                    break
                offset = next_offset

        except Exception as e:
            print(f"  [QDRANT] get_vectors_for_export failed: {e}")

        print(f"  [QDRANT] get_vectors_for_export: {len(id_to_vec)} vectors ready")
        return id_to_vec

    def get_export_etag(self) -> str:
        """
        MD5 hash of sorted point IDs — changes whenever any doc is added or deleted.

        Returned in the X-Export-Etag header by /kb/export.
        The mobile app sends this back in If-None-Match on the next poll.
        If it matches, the server returns 304 and the app skips the DB write.

        This is cheap: we only need IDs, not vectors or payload.
        """
        try:
            # get_all_ids() returns list of {"id": ...} dicts — extract the id values
            ids = self.get_all_ids()
            return hashlib.md5(
                ','.join(sorted(str(i["id"]) for i in ids)).encode()
            ).hexdigest()
        except Exception:
            return ""


__all__ = ["QdrantVectorStore", "_content_hash"]