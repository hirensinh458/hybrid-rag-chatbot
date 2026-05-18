# rag-backend/routers/ingest.py
#
# Phase 3 — Plan & Usage Enforcement
#
# CHANGES vs Phase 2 version:
#
#   /ingest endpoint (POST) — now includes plan enforcement:
#     PRE-FLIGHT CHECKS (before any processing):
#       1. Batch size check: file_count ≤ plan.max_batch_pdfs
#       2. Vector capacity check: current + estimated ≤ plan.max_vectors
#          Estimate: ~2 chunks per KB of PDF (conservative heuristic)
#
#     POST-FLIGHT ACCOUNTING (after _ingest_files_sync succeeds):
#       3. increment_vectors: adds ACTUAL chunk count to tenant_usage.vector_count
#       4. record_document:   inserts a row into documents table per indexed file
#
#     Store isolation (from Phase 2):
#       - vector_store and bm25_store are tenant-scoped (via get_tenant_stores)
#       - Supabase Storage upload path is pdfs/{tenant_slug}/{filename}
#
#   /ingest/{filename} DELETE — now includes vector count decrement and
#     document record cleanup after successful deletion.
#
#   All pre-flight failures return clear user-visible error messages.
#   Post-flight accounting failures are logged but non-fatal (ingest already succeeded;
#   nightly reconciliation corrects any drift).
#
# PHASE 2 CHANGES RETAINED:
#   - POST   /ingest             — requires JWT auth (resolve_tenant + require_admin_role)
#   - DELETE /ingest/{filename}  — requires JWT auth (resolve_tenant + require_admin_role)
#   - _ingest_files_sync()       — accepts tenant_slug parameter for tenant-scoped stores.
#     When provided, uses get_tenant_stores(tenant_slug) instead of global singletons.
#     Backward compatible: tenant_slug=None → falls back to global stores (dev mode).
#   - Supabase upload path scoped per-tenant: pdfs/{tenant_slug}/{filename}
#
# Read-only routes (GET /ingest/status, POST /ingest/sync) remain open —
# they carry no write risk and are used by the sync engine.
#
# All existing ingest logic (hash dedup, chunking, BM25, vector store) is UNCHANGED.

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from config import settings, PDFS_DIR
from middleware.tenant_resolver import (
    resolve_tenant,
    require_admin_role,
)
from services import rag_service as _svc
from services import rag_service
from services.plan_service import PlanService
from services.rag_service import get_tenant_stores
from services.supabase_client import get_supabase_admin
from ingestion.pdf_loader import PDFLoader
from schemas import DeleteFileResponse, IngestResponse, IngestStatusResponse
from utils.logger import get_logger

# NOTE: kb.py caches are imported locally inside functions to avoid circular deps

logger = get_logger(__name__)

router = APIRouter(
    tags=["ingest"],
    # All ingest routes require a valid JWT + admin role.
    # resolve_tenant populates request.state; require_admin_role gates non-admins.
    dependencies=[
        Depends(resolve_tenant),
        Depends(require_admin_role),
    ],
)


# ── PDFs directory ────────────────────────────────────────────────────────────

_PDFS_DIR = Path(PDFS_DIR)
_DATA_DIR = Path(settings.qdrant_path).parent


def _hash_file_path(tenant_slug: str | None) -> Path:
    """Return the per-tenant hash registry path."""
    if tenant_slug:
        return _DATA_DIR / f"file_hashes_{tenant_slug}.json"
    return _DATA_DIR / "file_hashes.json"   # legacy / dev fallback


def _load_hashes(tenant_slug: str | None = None) -> dict:
    path = _hash_file_path(tenant_slug)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save_hashes(hashes: dict, tenant_slug: str | None = None) -> None:
    path = _hash_file_path(tenant_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hashes, indent=2))


def _wipe_hashes(tenant_slug: str | None = None) -> None:
    path = _hash_file_path(tenant_slug)
    if path.exists():
        path.unlink()


def _remove_hash_for_file(filename: str, tenant_slug: str | None = None) -> None:
    hashes  = _load_hashes(tenant_slug)
    updated = {h: f for h, f in hashes.items() if f != filename}
    _save_hashes(updated, tenant_slug)


# ── PDF file management ───────────────────────────────────────────────────────

def _tenant_pdfs_dir(tenant_slug: str | None) -> Path:
    if tenant_slug:
        return _PDFS_DIR / tenant_slug
    return _PDFS_DIR   # legacy / dev fallback


def _store_pdf_file(tmp_path: str, filename: str, tenant_slug: str | None = None) -> Path | None:
    dest_dir = _tenant_pdfs_dir(tenant_slug)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    try:
        shutil.copy2(tmp_path, dest)
        logger.info(
            "[INGEST] PDF stored for viewer: data/pdfs/%s/%s",
            tenant_slug or "(global)", filename,
        )
        return dest
    except Exception as e:
        logger.warning("[INGEST] Could not store PDF for viewer: %s", e)
        return None


def _delete_pdf_file(filename: str, tenant_slug: str | None = None) -> None:
    pdf_path = _tenant_pdfs_dir(tenant_slug) / filename
    if pdf_path.exists():
        try:
            pdf_path.unlink()
            logger.info(
                "[INGEST] PDF deleted from viewer store: data/pdfs/%s/%s",
                tenant_slug or "(global)", filename,
            )
        except Exception as e:
            logger.warning("[INGEST] Could not delete PDF from viewer store: %s", e)


# ── Loader dispatch ───────────────────────────────────────────────────────────

def _get_loader(tmp_path: str, filename: str):
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return PDFLoader(tmp_path)
    return None


# ── Core ingest logic (runs in threadpool) ────────────────────────────────────

def _ingest_files_sync(
    file_paths  : list[tuple[str, str]],
    tenant_slug : str = None,           # Phase 2 — thread tenant scope through ingest
) -> dict:
    """
    Ingest one or more files into the tenant-scoped vector store and BM25 index.

    Phase 2 change:
      When tenant_slug is provided, uses get_tenant_stores(tenant_slug) to get
      the tenant-scoped vector store and BM25 index. This ensures documents are
      stored in the correct per-tenant Qdrant collection (rag_docs_{tenant_slug})
      and the correct per-tenant BM25 file (bm25_{tenant_slug}.pkl).

      When tenant_slug is None (legacy / single-tenant dev mode), falls back to
      the global singletons from rag_service — backward compatible.

    Phase 3 change:
      Supabase Storage upload path is now pdfs/{tenant_slug}/{filename}
      instead of pdfs/{filename}.

    For each PDF:
      1. Remove existing vectors/BM25 chunks for this filename (overwrite support).
      2. Duplicate check (SHA-256) — guards against same file twice in one batch.
      3. Load blocks via PDFLoader.
      4. Chunk (hierarchical or other strategy).
      5. [Supabase] Upload to Supabase Storage → get public_url (skipped if not configured).
      6. [Supabase] Inject source_url into every chunk dict.
      7. Add chunks to vector store + BM25.
      8. Copy PDF to data/pdfs/ for the local viewer.

    Returns:
        {
            "files_indexed" : list[str],
            "skipped"       : list[str],
            "total_chunks"  : int,      ← actual chunk count (for usage accounting)
            "total_parents" : int,
            "file_chunks"   : {filename: int},  ← per-file chunk counts
        }
    """
    try:
        from services.supabase_storage import upload_pdf_to_supabase
        _supabase_import_ok = True
    except ImportError:
        _supabase_import_ok = False
        logger.warning("[INGEST] supabase_storage import failed — Supabase upload disabled")

    # ── Resolve stores: tenant-scoped or global fallback ─────────────────────
    if tenant_slug:
        vector_store, bm25_store = get_tenant_stores(tenant_slug)
        logger.info(
            "[INGEST] Using tenant-scoped stores — slug=%s", tenant_slug
        )
    else:
        vector_store = rag_service.get_vector_store()
        bm25_store   = rag_service.get_bm25_store()
        logger.info("[INGEST] Using global stores (single-tenant / dev mode)")

    hashes       = _load_hashes(tenant_slug)
    chunker      = _svc.get_chunker()

    files_indexed : list[str] = []
    skipped       : list[str] = []
    all_children  : list[dict] = []
    file_chunks   : dict[str, int] = {}

    # Save original PDFs for viewer (early pass — tenant-scoped)
    pdfs_dir = _tenant_pdfs_dir(tenant_slug)
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    for tmp_path_str, filename in file_paths:
        tmp_path = Path(tmp_path_str)
        if tmp_path.exists():
            shutil.copy2(tmp_path, pdfs_dir / filename)

    # Track sha256s seen in THIS batch only
    batch_hashes: set[str] = set()

    for tmp_path, filename in file_paths:

        # ── 1. Remove existing data for this filename before hash check ───────
        existing_sources = vector_store.list_sources()
        if filename in existing_sources:
            logger.info(
                "[INGEST] Overwrite detected for '%s' — removing old vectors and BM25 entries",
                filename,
            )
            vector_store.delete_by_source(filename)
            bm25_store.delete_by_source(filename)
            _remove_hash_for_file(filename, tenant_slug)
            hashes = _load_hashes(tenant_slug)
            logger.info("[INGEST] Old data cleared for '%s' — re-indexing fresh", filename)

        # ── 2. Duplicate check (within THIS batch only) ───────────────────────
        raw   = Path(tmp_path).read_bytes()
        fhash = hashlib.sha256(raw).hexdigest()

        if fhash in batch_hashes:
            logger.info("[INGEST] Skipping duplicate in batch: %s", filename)
            skipped.append(filename)
            continue

        batch_hashes.add(fhash)

        # ── 3. Load ───────────────────────────────────────────────────────────
        loader = _get_loader(tmp_path, filename)
        if not loader:
            logger.warning("[INGEST] Unsupported file type: %s", filename)
            skipped.append(filename)
            continue

        try:
            blocks = loader.load()
        except Exception as e:
            logger.error("[INGEST] Load failed for %s: %s", filename, e)
            skipped.append(filename)
            continue

        if not blocks:
            skipped.append(filename)
            continue

        for b in blocks:
            b["source"] = filename

        # ── 4. Chunk ──────────────────────────────────────────────────────────
        from ingestion.chunker import HierarchicalChunker
        if isinstance(chunker, HierarchicalChunker):
            children = chunker.chunk_hierarchical(blocks)
        else:
            children = chunker.chunk_documents(blocks)

        # ── 5. Upload to Supabase Storage (tenant-scoped path) ────────────────
        # Phase 3: path is now pdfs/{tenant_slug}/{filename}
        source_url = ""
        if _supabase_import_ok and Path(filename).suffix.lower() == ".pdf":
            try:
                # Phase 2/3: Pass tenant_slug so upload path is pdfs/{slug}/{filename}
                public_url = upload_pdf_to_supabase(
                    file_path   = tmp_path,
                    tenant_slug = tenant_slug or "",
                )
                if public_url:
                    source_url = public_url
                    logger.info(
                        "[INGEST] [SUPABASE] source_url set for '%s': %s",
                        filename, source_url,
                    )
                else:
                    logger.warning(
                        "[INGEST] [SUPABASE] Upload returned None for '%s' — source_url left empty",
                        filename,
                    )
            except Exception as exc:
                logger.warning(
                    "[INGEST] [SUPABASE] Upload exception for '%s': %s — continuing without source_url",
                    filename, exc,
                )

        # ── 6. Inject source_url into every chunk ─────────────────────────────
        for child in children:
            child["source_url"] = source_url

        all_children.extend(children)
        files_indexed.append(filename)
        file_chunks[filename] = len(children)
        hashes[fhash] = filename

        # ── 7. Copy PDF to local viewer store ─────────────────────────────────
        if Path(filename).suffix.lower() == ".pdf":
            _store_pdf_file(tmp_path, filename, tenant_slug)

    # ── 8. Index all new chunks ───────────────────────────────────────────────
    if all_children:
        vector_store.add_documents(all_children)
        bm25_store.add(all_children)

    _save_hashes(hashes, tenant_slug)

    logger.info(
        "[INGEST] Complete — tenant=%s  indexed=%s  chunks=%d  skipped=%s",
        tenant_slug or "(global)",
        files_indexed,
        len(all_children),
        skipped,
    )

    return {
        "files_indexed": files_indexed,
        "skipped"      : skipped,
        "total_chunks" : len(all_children),
        "total_parents": len(all_children),
        "file_chunks"  : file_chunks,   # Phase 3 — per-file counts for document records
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse)
async def ingest(request: Request, files: list[UploadFile] = File(...)):
    """
    Upload one or more PDF files into the tenant's knowledge base.

    Phase 2: Requires a valid admin JWT (Authorization: Bearer <access_token>).
    Documents are stored in the tenant-scoped vector collection and BM25 index.

    Phase 3 ADDITIONS:
    - Pre-flight batch size check (returns 400 if too many files).
    - Pre-flight vector capacity check (returns 402 if quota would be exceeded).
    - Post-flight: actual chunk count added to tenant_usage.vector_count.
    - Post-flight: one documents row inserted per successfully indexed file.
    - Tenant-scoped vector store and BM25 used throughout.
    """
    # ── Read tenant context set by resolve_tenant ─────────
    tenant_id   = request.state.tenant_id
    tenant_slug = request.state.tenant_slug

    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    # ── PHASE 3: Plan service setup ───────────────────────
    supabase  = get_supabase_admin()
    plan_svc  = PlanService(supabase)

    # ── PHASE 3: PRE-FLIGHT CHECK 1 — Batch size ──────────
    ok, err = await run_in_threadpool(
        plan_svc.check_batch_size, tenant_id, len(files)
    )
    if not ok:
        logger.warning(
            "[INGEST] Batch size check FAILED — tenant=%s  files=%d  err=%s",
            tenant_slug, len(files), err,
        )
        raise HTTPException(status_code=400, detail=err)

    # ── PHASE 3: PRE-FLIGHT CHECK 2 — Vector capacity ─────
    # Rough heuristic: read all file sizes to estimate chunk count.
    # We read .size from UploadFile headers (available without reading bytes yet).
    # ~2 chunks per KB of PDF is a conservative upper-bound estimate.
    total_size_bytes = sum(
        f.size for f in files
        if f.size is not None
    )
    estimated_chunks = max(1, total_size_bytes // 500)   # ~2 chunks per KB

    ok, err = await run_in_threadpool(
        plan_svc.check_vector_capacity, tenant_id, estimated_chunks
    )
    if not ok:
        logger.warning(
            "[INGEST] Vector quota pre-flight FAILED — tenant=%s  estimate=%d  err=%s",
            tenant_slug, estimated_chunks, err,
        )
        raise HTTPException(
            status_code=402,
            detail={"code": "over_quota", "message": err},
        )

    # ── GUARD: Block upload if tenant is already over_quota ───────────────
    tenant_status = request.state.tenant.get("status")
    if tenant_status == "over_quota":
        logger.warning(
            "[INGEST] Upload blocked — tenant=%s is over_quota",
            tenant_slug,
        )
        raise HTTPException(
            status_code=402,
            detail={
                "code"   : "over_quota",
                "message": (
                    "Your organization has exceeded its vector limit. "
                    "Delete documents or contact your admin to upgrade your plan."
                ),
            },
        )

    # ── Phase 2: Save PDFs for the viewer (tenant-scoped) ─
    pdfs_dir = _tenant_pdfs_dir(tenant_slug)
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    for file in files:
        if file.filename.lower().endswith(".pdf"):
            dest_path = pdfs_dir / file.filename
            content   = await file.read()
            with open(dest_path, "wb") as f:
                f.write(content)
            await file.seek(0)

    # ── Write uploads to a temp directory ─────────────────
    tmp_dir    = Path("/tmp") / f"rag_ingest_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    file_paths : list[tuple[str, str]] = []
    file_sizes : dict[str, int]        = {}  # Phase 3 — track sizes for document records

    try:
        for upload in files:
            tmp_path = tmp_dir / upload.filename
            content  = await upload.read()
            tmp_path.write_bytes(content)
            file_paths.append((str(tmp_path), upload.filename))
            file_sizes[upload.filename] = len(content)

        result = await run_in_threadpool(
            _ingest_files_sync, file_paths, tenant_slug
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── PHASE 3: POST-FLIGHT — Usage accounting ───────────
    actual_chunks = result["total_chunks"]
    file_chunk_map = result.get("file_chunks", {})

    if actual_chunks > 0:
        try:
            # Increment vector count atomically
            await run_in_threadpool(
                plan_svc.increment_vectors, tenant_id, actual_chunks
            )
            logger.info(
                "[INGEST] Vector count incremented — tenant=%s  +%d chunks",
                tenant_slug, actual_chunks,
            )
        except Exception as exc:
            # Non-fatal: log and continue. Nightly reconciliation corrects drift.
            logger.error(
                "[INGEST] Failed to increment vector count — tenant=%s: %s",
                tenant_slug, exc,
            )

    # ── PHASE 3: POST-FLIGHT — Record each indexed file ───
    for filename in result["files_indexed"]:
        chunk_count = file_chunk_map.get(filename, 0)
        file_size   = file_sizes.get(filename)
        try:
            await run_in_threadpool(
                plan_svc.record_document,
                tenant_id,
                tenant_slug,
                filename,
                chunk_count,
                file_size,
                "success",
            )
        except Exception as exc:
            logger.error(
                "[INGEST] Failed to record document — tenant=%s  file=%s: %s",
                tenant_slug, filename, exc,
            )

    # ── Invalidate KB caches ──────────────────────────────
    try:
        from routers.kb import _vec_cache, _source_hash_cache
        _vec_cache.clear()
        _source_hash_cache.clear()
    except Exception:
        pass

    logger.info(
        "[INGEST] ✅ Complete — tenant=%s  indexed=%d  skipped=%d  chunks=%d",
        tenant_slug,
        len(result["files_indexed"]),
        len(result["skipped"]),
        actual_chunks,
    )

    return IngestResponse(
        status        = "ok",
        files_indexed = result["files_indexed"],
        total_chunks  = result["total_chunks"],
        total_parents = result["total_parents"],
        message       = (
            f"Indexed {len(result['files_indexed'])} file(s). "
            f"Skipped {len(result['skipped'])} duplicate(s)."
        ),
    )


@router.delete("/ingest/{filename}", response_model=DeleteFileResponse)
async def delete_file(request: Request, filename: str):
    """
    Delete a file from the knowledge base.

    Phase 2: Requires a valid admin JWT.

    Phase 3 CHANGE: After deletion, decrements tenant_usage.vector_count
    by the document's chunk_count (looked up from the documents table).
    Also deletes the documents table row.

    RULES (unchanged from Phase 1):
      - Must be ONLINE to delete (cloud is authoritative).
      - Must have a cloud store configured (QDRANT_CLOUD_URL set).
      - Deletes from CLOUD only — local vectors are cleaned up by the
        next sync run.
      - BM25 index and local PDF file are cleaned up immediately.
    """
    tenant_id   = request.state.tenant_id  # Phase 3
    tenant_slug = request.state.tenant_slug
    vs, bm25    = get_tenant_stores(tenant_slug)  # Phase 2

    if not rag_service.is_online():
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = (
                f"Cannot delete '{filename}' while offline. "
                "Deletion requires a cloud connection. Please reconnect and try again."
            ),
        )

    cloud_store = rag_service.get_cloud_store()
    if cloud_store is None:
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = (
                f"Cannot delete '{filename}': no cloud store is configured. "
                "Set QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY in .env."
            ),
        )

    # Check existence in the tenant's store
    tenant_sources = vs.list_sources()
    if filename not in tenant_sources:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"File '{filename}' not found in the knowledge base.",
        )

    # ── PHASE 3: Look up chunk_count from documents table BEFORE deleting ──
    # We need it to decrement the usage counter accurately.
    chunk_count  = 0
    doc_id       = None
    supabase     = get_supabase_admin()

    try:
        doc_result = (
            supabase
            .table("documents")
            .select("id, chunk_count")
            .eq("tenant_id", tenant_id)
            .eq("filename", filename)
            .single()
            .execute()
        )
        if doc_result.data:
            doc_id      = doc_result.data.get("id")
            chunk_count = doc_result.data.get("chunk_count", 0)
    except Exception as exc:
        logger.warning(
            "[INGEST] Could not look up chunk_count for '%s': %s — decrement skipped",
            filename, exc,
        )

    # ── Delete vectors from cloud ─────────────────────────
    result = await run_in_threadpool(rag_service.delete_file_from_cloud, filename, tenant_slug)

    # Phase 2: Clean up tenant BM25 immediately
    bm25.delete_by_source(filename)

    # (duplicate delete guard removed — delete_file_from_cloud handles this atomically)

    _remove_hash_for_file(filename, tenant_slug)
    _delete_pdf_file(filename, tenant_slug)

    # ── PHASE 3: POST-DELETION — Decrement vector count ───
    if chunk_count > 0:
        plan_svc = PlanService(supabase)
        try:
            await run_in_threadpool(
                plan_svc.decrement_vectors, tenant_id, chunk_count
            )
            logger.info(
                "[INGEST] Vector count decremented — tenant=%s  -%d chunks",
                tenant_slug, chunk_count,
            )
        except Exception as exc:
            logger.error(
                "[INGEST] Failed to decrement vector count — tenant=%s: %s",
                tenant_slug, exc,
            )

    # ── PHASE 3: Delete document row ──────────────────────
    if doc_id:
        try:
            supabase.table("documents").delete().eq("id", doc_id).execute()
            logger.info(
                "[INGEST] Document record deleted — tenant=%s  file=%s",
                tenant_slug, filename,
            )
        except Exception as exc:
            logger.warning(
                "[INGEST] Failed to delete document record — tenant=%s  file=%s: %s",
                tenant_slug, filename, exc,
            )

    # ── Invalidate KB caches ──────────────────────────────
    try:
        from routers.kb import _vec_cache, _source_hash_cache
        _vec_cache.clear()
        _source_hash_cache.clear()
    except Exception:
        pass

    logger.info(
        "[INGEST/DELETE] ✅ tenant=%s  file=%s  vectors_deleted=%d",
        tenant_slug, filename, result.get("vectors_deleted", 0),
    )

    return DeleteFileResponse(
        status          = "ok",
        filename        = filename,
        vectors_deleted = result.get("vectors_deleted", 0),
        message         = (
            f"Deleted '{filename}': {result.get('vectors_deleted', 0)} vectors removed. "
            "Local vectors will be cleaned up on next sync."
        ),
    )


@router.get("/ingest/status/{task_id}", response_model=IngestStatusResponse)
async def ingest_status(task_id: str):
    """Return status of an async ingest task by ID."""
    task = rag_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return IngestStatusResponse(**task)


@router.post("/ingest/sync")
async def trigger_sync(request: Request):
    """
    Manually trigger a Cloud→Local sync for the requesting tenant.
    Requires a valid admin JWT — called by the admin panel when the
    admin wants to force a manual sync.
    The router-level dependencies (resolve_tenant + require_admin_role)
    already enforce this.
    """
    from services.sync_service import SyncService
    import asyncio
    sync = SyncService()

    if rag_service.get_cloud_store() is None:
        return {"status": "skipped", "message": "Cloud store not configured."}

    asyncio.create_task(run_in_threadpool(sync.run))
    return {"status": "triggered", "message": "Sync started in background."}


__all__ = ["router", "_ingest_files_sync", "_store_pdf_file", "_delete_pdf_file",
           "_remove_hash_for_file", "_wipe_hashes"]