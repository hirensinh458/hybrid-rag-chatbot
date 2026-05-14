# rag-backend/routers/ingest.py
#
# Phase 3 — Plan & Usage Enforcement
#
# CHANGES vs Phase 1 version:
#
#   /ingest endpoint (POST) — now requires JWT auth via resolve_tenant dependency:
#     PRE-FLIGHT CHECKS (before any processing):
#       1. Batch size check: file_count ≤ plan.max_batch_pdfs
#       2. Vector capacity check: current + estimated ≤ plan.max_vectors
#          Estimate: ~2 chunks per KB of PDF (conservative heuristic)
#
#     POST-FLIGHT ACCOUNTING (after _ingest_files_sync succeeds):
#       3. increment_vectors: adds ACTUAL chunk count to tenant_usage.vector_count
#       4. record_document:   inserts a row into documents table per indexed file
#
#     Store isolation:
#       - vector_store and bm25_store are now tenant-scoped (via get_tenant_stores)
#       - Supabase Storage upload path is now pdfs/{tenant_slug}/{filename}
#
#   /ingest/{filename} DELETE — protected by resolve_tenant + require_admin_role.
#     Decrements tenant_usage.vector_count by the document's chunk_count after delete.
#
#   All pre-flight failures return clear user-visible error messages.
#   Post-flight accounting failures are logged but non-fatal (ingest already succeeded;
#   nightly reconciliation corrects any drift).
#
# UNCHANGED:
#   _ingest_files_sync() core logic (chunking, embedding, BM25, Qdrant)
#   Hash registry for same-batch duplicate detection
#   PDF viewer copy logic
#   /ingest/sync and /ingest/status endpoints

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
    require_active_subscription,
    require_admin_role,
    resolve_tenant,
)
from services import rag_service as _svc
from services.plan_service import PlanService
from services.rag_service import get_tenant_stores
from services.supabase_client import get_supabase_admin
from ingestion.pdf_loader import PDFLoader
from schemas import DeleteFileResponse, IngestResponse, IngestStatusResponse
from services import rag_service
from utils.logger import get_logger

logger = get_logger(__name__)

# NO import from routers.kb here — use local imports inside functions instead

router = APIRouter(
    tags=["ingest"],
    # All ingest routes require a valid JWT + admin role.
    # resolve_tenant populates request.state; require_admin_role gates non-admins.
    dependencies=[
        Depends(resolve_tenant),
        Depends(require_admin_role),
    ],
)

# ── Hash registry ─────────────────────────────────────────────
_HASH_FILE = Path(settings.qdrant_path).parent / "file_hashes.json"

# ── PDFs directory ────────────────────────────────────────────
_PDFS_DIR = Path(PDFS_DIR)


def _load_hashes() -> dict:
    if _HASH_FILE.exists():
        try:
            return json.loads(_HASH_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_hashes(hashes: dict) -> None:
    _HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HASH_FILE.write_text(json.dumps(hashes, indent=2))


def _wipe_hashes() -> None:
    if _HASH_FILE.exists():
        _HASH_FILE.unlink()


def _remove_hash_for_file(filename: str) -> None:
    hashes  = _load_hashes()
    updated = {h: f for h, f in hashes.items() if f != filename}
    _save_hashes(updated)


# ── PDF file management ───────────────────────────────────────

def _store_pdf_file(tmp_path: str, filename: str) -> Path | None:
    _PDFS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _PDFS_DIR / filename
    try:
        shutil.copy2(tmp_path, dest)
        logger.info("[INGEST] PDF stored for viewer: data/pdfs/%s", filename)
        return dest
    except Exception as e:
        logger.warning("[INGEST] Could not store PDF for viewer: %s", e)
        return None


def _delete_pdf_file(filename: str) -> None:
    pdf_path = _PDFS_DIR / filename
    if pdf_path.exists():
        try:
            pdf_path.unlink()
            logger.info("[INGEST] PDF deleted from viewer store: data/pdfs/%s", filename)
        except Exception as e:
            logger.warning("[INGEST] Could not delete PDF from viewer store: %s", e)


# ── Loader dispatch ───────────────────────────────────────────

def _get_loader(tmp_path: str, filename: str):
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return PDFLoader(tmp_path)
    return None


# ── Core ingest logic (runs in threadpool) ────────────────────

def _ingest_files_sync(
    file_paths  : list[tuple[str, str]],
    tenant_slug : str,
    vector_store: object,
    bm25_store  : object,
) -> dict:
    """
    Ingest one or more files into the tenant-scoped vector store.

    PHASE 3 CHANGE: Now accepts tenant_slug, vector_store, and bm25_store
    as parameters instead of reading global singletons. This ensures each
    tenant's data lands in their own Qdrant collection and BM25 pickle file.

    PHASE 3 CHANGE: Supabase Storage upload path is now
    pdfs/{tenant_slug}/{filename} instead of pdfs/{filename}.

    Everything else is unchanged from the Phase 1 version.

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

    hashes = _load_hashes()
    chunker = _svc.get_chunker()

    files_indexed : list[str] = []
    skipped       : list[str] = []
    all_children  : list[dict] = []
    file_chunks   : dict[str, int] = {}

    # Save original PDFs for viewer (early pass — keeps existing behaviour)
    pdfs_dir = Path(settings.qdrant_path).parent / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    for tmp_path_str, filename in file_paths:
        tmp_path = Path(tmp_path_str)
        if tmp_path.exists():
            shutil.copy2(tmp_path, pdfs_dir / filename)

    # Track sha256s seen in THIS batch only — prevents same file twice in one upload
    batch_hashes: set[str] = set()

    for tmp_path, filename in file_paths:

        # ── Step 1: Remove existing data for this filename BEFORE the hash check
        # ─────────────────────────────────────────────────────────────────────
        existing_sources = vector_store.list_sources()
        if filename in existing_sources:
            logger.info(
                "[INGEST] Overwrite detected for '%s' — removing old vectors and BM25 entries",
                filename,
            )
            vector_store.delete_by_source(filename)
            bm25_store.delete_by_source(filename)
            _remove_hash_for_file(filename)
            hashes = _load_hashes()
            logger.info("[INGEST] Old data cleared for '%s' — re-indexing fresh", filename)

        # ── Step 2: Duplicate check (within THIS batch only)
        raw   = Path(tmp_path).read_bytes()
        fhash = hashlib.sha256(raw).hexdigest()

        if fhash in batch_hashes:
            logger.info("[INGEST] Skipping duplicate in batch: %s", filename)
            skipped.append(filename)
            continue

        batch_hashes.add(fhash)

        # ── Step 3: Load ──────────────────────────────────
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

        # ── Step 4: Chunk ─────────────────────────────────
        from ingestion.chunker import HierarchicalChunker
        if isinstance(chunker, HierarchicalChunker):
            children = chunker.chunk_hierarchical(blocks)
        else:
            children = chunker.chunk_documents(blocks)

        # ── Step 5: Upload to Supabase Storage ────────────
        # PHASE 3: path is now pdfs/{tenant_slug}/{filename}
        source_url = ""
        if _supabase_import_ok and Path(filename).suffix.lower() == ".pdf":
            try:
                public_url = upload_pdf_to_supabase(
                    file_path   = tmp_path,
                    tenant_slug = tenant_slug,
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

        # ── Step 6: Inject source_url into every chunk ────
        for child in children:
            child["source_url"] = source_url

        all_children.extend(children)
        files_indexed.append(filename)
        file_chunks[filename] = len(children)
        hashes[fhash] = filename

        # ── Step 7: Copy PDF to local viewer store ────────
        if Path(filename).suffix.lower() == ".pdf":
            _store_pdf_file(tmp_path, filename)

    # ── Step 8: Index all new chunks ─────────────────────
    if all_children:
        vector_store.add_documents(all_children)
        bm25_store.add(all_children)

    _save_hashes(hashes)

    return {
        "files_indexed": files_indexed,
        "skipped"      : skipped,
        "total_chunks" : len(all_children),
        "total_parents": len(all_children),
        "file_chunks"  : file_chunks,   # NEW — per-file counts for document records
    }


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse)
async def ingest(request: Request, files: list[UploadFile] = File(...)):
    """
    Upload one or more PDF files into the tenant's knowledge base.

    PHASE 3 ADDITIONS:
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

    # ── Plan service setup ────────────────────────────────
    supabase  = get_supabase_admin()
    plan_svc  = PlanService(supabase)

    # ── PRE-FLIGHT CHECK 1: Batch size ───────────────────
    ok, err = await run_in_threadpool(
        plan_svc.check_batch_size, tenant_id, len(files)
    )
    if not ok:
        logger.warning(
            "[INGEST] Batch size check FAILED — tenant=%s  files=%d  err=%s",
            tenant_slug, len(files), err,
        )
        raise HTTPException(status_code=400, detail=err)

    # ── PRE-FLIGHT CHECK 2: Vector capacity ───────────────
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

    # ── Get tenant-scoped stores ──────────────────────────
    vector_store, bm25_store = get_tenant_stores(tenant_slug)

    # ── Save PDFs early for viewer (existing behaviour) ───
    pdfs_dir = Path(settings.qdrant_path).parent / "pdfs"
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
    file_sizes : dict[str, int]        = {}

    try:
        for upload in files:
            tmp_path = tmp_dir / upload.filename
            content  = await upload.read()
            tmp_path.write_bytes(content)
            file_paths.append((str(tmp_path), upload.filename))
            file_sizes[upload.filename] = len(content)

        result = await run_in_threadpool(
            _ingest_files_sync,
            file_paths,
            tenant_slug,
            vector_store,
            bm25_store,
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── POST-FLIGHT: Usage accounting ─────────────────────
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

    # ── POST-FLIGHT: Record each indexed file in documents table ──
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
    from routers.kb import _vec_cache, _source_hash_cache
    _vec_cache.clear()
    _source_hash_cache.clear()

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
async def delete_file(filename: str, request: Request):
    """
    Delete a file from the knowledge base.

    PHASE 3 CHANGE: After deletion, decrements tenant_usage.vector_count
    by the document's chunk_count (looked up from the documents table).
    Also deletes the documents table row.

    RULES (unchanged from Phase 1):
      - Must be ONLINE to delete (cloud is authoritative).
      - Must have a cloud store configured (QDRANT_CLOUD_URL set).
      - Deletes from CLOUD only — local vectors are cleaned up by the
        next sync run.
      - BM25 index and local PDF file are cleaned up immediately.
    """
    tenant_id   = request.state.tenant_id
    tenant_slug = request.state.tenant_slug

    if not rag_service.is_online():
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = (
                f"Cannot delete '{filename}' while offline. "
                "Deletion requires a cloud connection so the change is "
                "applied to the authoritative store. Please reconnect and try again."
            ),
        )

    cloud_store = rag_service.get_cloud_store()
    if cloud_store is None:
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = (
                f"Cannot delete '{filename}': no cloud store is configured. "
                "Set QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY in .env to enable deletion."
            ),
        )

    cloud_sources = cloud_store.list_sources()
    if filename not in cloud_sources:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"File '{filename}' not found in the cloud knowledge base.",
        )

    # ── Look up chunk_count from documents table BEFORE deleting ──
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
    result = await run_in_threadpool(rag_service.delete_file_from_cloud, filename)

    _remove_hash_for_file(filename)
    _delete_pdf_file(filename)

    # ── POST-DELETION: Decrement vector count ─────────────
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

    # ── Delete document row ───────────────────────────────
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
    from routers.kb import _vec_cache, _source_hash_cache
    _vec_cache.clear()
    _source_hash_cache.clear()

    return DeleteFileResponse(
        status          = "ok",
        filename        = filename,
        vectors_deleted = result["vectors_deleted"],
        message         = (
            f"Deleted '{filename}' from cloud: "
            f"{result['vectors_deleted']} vectors removed. "
            f"Local vectors will be cleaned up on next sync."
        ),
    )


@router.get("/ingest/status/{task_id}", response_model=IngestStatusResponse)
async def ingest_status(task_id: str):
    task = rag_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return IngestStatusResponse(**task)


@router.post("/ingest/sync")
async def trigger_sync(request: Request):
    """
    Manually trigger a Cloud→Local sync.
    Called automatically by NetworkMonitor when internet is detected.
    """
    from services.sync_service import SyncService
    import asyncio
    sync = SyncService()

    if rag_service.get_cloud_store() is None:
        return {"status": "skipped", "message": "Cloud store not configured."}

    asyncio.create_task(run_in_threadpool(sync.run))
    return {"status": "triggered", "message": "Sync started in background."}