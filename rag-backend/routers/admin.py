# routers/admin.py
#
# Phase 4 — Backend: Admin API (Document Management + Stats)
#
# CHANGES vs Phase 3 version:
#   NEW endpoints:
#     GET    /admin/documents               — list documents from `documents` DB table
#     DELETE /admin/documents/{doc_id}      — full delete by UUID: vectors + BM25 +
#                                             Supabase Storage + DB row + usage decrement
#     GET    /admin/usage                   — plan usage stats with limits and percentages
#
# CHANGES vs Phase 4 initial version (quota enforcement):
#   admin_ingest now has TWO pre-flight checks (matching routers/ingest.py):
#     1. check_batch_size  — rejects if file count exceeds plan limit (HTTP 400)
#     2. quota pre-check   — rejects immediately if already at/over vector limit (HTTP 402)
#   admin_ingest now passes vector_cap (remaining capacity) to _ingest_files_sync
#     so ingestion stops at the limit rather than exceeding it mid-batch.
#   admin_ingest handles the quota_hit / remaining_files result fields and:
#     - Calls _check_and_update_quota_status to flip tenant to over_quota instantly.
#     - Returns IngestResponse with status="partial" and the list of skipped files.
#
# All Phase 2/3 endpoints are UNCHANGED:
#     GET    /admin/join-code
#     POST   /admin/join-code/regenerate
#     POST   /admin/ingest
#     DELETE /admin/file/{filename}
#     DELETE /admin/collection
#     GET    /admin/files
#     GET    /admin/stats
#
# Why two delete endpoints?
#   DELETE /admin/file/{filename}   — legacy; uses filename directly from Qdrant
#                                     sources; requires cloud store to be configured.
#   DELETE /admin/documents/{doc_id} — Phase 4; uses doc UUID from the `documents`
#                                       table; works without cloud store; correct
#                                       usage accounting via chunk_count from DB.
#
# Auth: all routes require a valid Supabase JWT with role == 'admin' | 'super_admin'.
# Obtain via POST /auth/admin/login.

import random
import shutil
import string
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from middleware.tenant_resolver import require_admin_role, resolve_tenant
from services                  import rag_service
from services.rag_service      import get_tenant_stores
from services.supabase_client  import get_supabase_admin
from services.plan_service     import PlanService
from schemas                   import IngestResponse, DeleteFileResponse, WipeResponse
from config                    import settings
from utils.logger              import get_logger

# Re-use all the existing ingest helpers from routers/ingest.py
# so there is no logic duplication — admin routes call the same
# internal functions that the original /ingest endpoints use.
from routers.ingest import (
    _ingest_files_sync,
    _store_pdf_file,
    _delete_pdf_file,
    _remove_hash_for_file,
    _wipe_hashes,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix       = "/admin",
    tags         = ["admin"],
    dependencies = [
        Depends(resolve_tenant),      # validates JWT, sets request.state.*
        Depends(require_admin_role),  # enforces role == 'admin' | 'super_admin'
    ],
)


# ── Join code helpers ──────────────────────────────────────────────────────────

_JOIN_WORDS: list[str] = [
    "SHIP", "DOCK", "CREW", "MAST", "SAIL", "PORT", "HULL", "DECK",
    "KEEL", "HELM", "TIDE", "WAVE", "REEF", "BUOY", "LANE", "WIND",
    "BOLT", "CRANE", "LOCK", "PIER", "ROPE", "TANK", "YARD", "STAR",
    "GULF", "CAPE", "COVE", "ISLE", "QUAY", "BRIG",
]


def _gen_unique_join_code(sb) -> str:
    """Generate a join code that doesn't already exist in the DB."""
    for _ in range(20):
        word   = random.choice(_JOIN_WORDS)
        digits = random.randint(1000, 9999)
        code   = f"{word}-{digits}"
        result = sb.table("tenants").select("id").eq("join_code", code).execute()
        if not result.data:
            return code
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


# ── GET /admin/join-code ──────────────────────────────────────────────────────

@router.get("/join-code")
async def get_join_code(request: Request):
    """
    Return the current join code for this tenant.

    Employees use this code when signing up via POST /auth/mobile/signup.
    Only tenant admins can view the join code.
    """
    tenant_id = request.state.tenant_id
    sb        = get_supabase_admin()

    result = (
        sb.table("tenants")
        .select("join_code, slug, display_name")
        .eq("id", tenant_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Tenant not found.",
        )

    data = result.data
    logger.info(
        "[ADMIN/JOIN-CODE] GET — tenant=%s  code=%s",
        data.get("slug"), data.get("join_code"),
    )
    return {
        "join_code"   : data["join_code"],
        "slug"        : data["slug"],
        "display_name": data["display_name"],
        "hint"        : "Share this code with employees so they can sign up.",
    }


# ── POST /admin/join-code/regenerate ─────────────────────────────────────────

@router.post("/join-code/regenerate")
async def regenerate_join_code(request: Request):
    """
    Generate a new join code for this tenant, replacing the old one.

    The old join code is immediately invalidated.
    Employees who already signed up are unaffected — their JWTs carry
    tenant_id directly and do not depend on the join code.
    """
    tenant_id = request.state.tenant_id
    sb        = get_supabase_admin()

    new_code = _gen_unique_join_code(sb)

    result = (
        sb.table("tenants")
        .update({"join_code": new_code})
        .eq("id", tenant_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Failed to update join code. Please try again.",
        )

    logger.info(
        "[ADMIN/JOIN-CODE] Regenerated — tenant=%s  new_code=%s",
        request.state.tenant_slug, new_code,
    )
    return {
        "join_code": new_code,
        "message"  : "Join code rotated. Share the new code with new employees.",
    }


# ── GET /admin/documents ──────────────────────────────────────────────────────
#
# Phase 4: Returns rows from the `documents` metadata table for this tenant.
# Richer than GET /admin/files which only returns filenames from Qdrant sources.
# Includes chunk_count, file_size, ingestion status, and storage path — enough
# for the admin dashboard to render a full document management table.

@router.get("/documents")
async def admin_list_documents(request: Request):
    """
    List all ingested documents for this tenant from the metadata DB.

    Returns full document records (id, filename, chunk_count, file_size,
    status, ingested_at, storage_path) — not just filenames.

    Use DELETE /admin/documents/{doc_id} to remove a document by its UUID.
    Use GET /admin/files to get a simple filename list from the vector store.

    Response shape:
        {
          "documents": [
            {
              "id"          : "uuid",
              "filename"    : "engine_manual.pdf",
              "chunk_count" : 142,
              "file_size"   : 2048576,     // bytes, null if unknown
              "status"      : "success",   // success | failed | partial
              "ingested_at" : "2025-01-15T10:30:00Z",
              "storage_path": "pdfs/acme_shipping/engine_manual.pdf"
            }, ...
          ],
          "total": 3
        }
    """
    tenant_id = request.state.tenant_id
    sb        = get_supabase_admin()

    result = (
        sb.table("documents")
        .select("id, filename, chunk_count, file_size, status, ingested_at, storage_path")
        .eq("tenant_id", tenant_id)
        .order("ingested_at", desc=True)
        .execute()
    )

    docs = result.data or []

    logger.debug(
        "[ADMIN/DOCUMENTS] tenant=%s  count=%d",
        request.state.tenant_slug, len(docs),
    )
    return {
        "documents": docs,
        "total"    : len(docs),
    }


# ── DELETE /admin/documents/{doc_id} ─────────────────────────────────────────
#
# Phase 4: Delete a document by its UUID from the `documents` table.
# This is the canonical delete endpoint for the admin dashboard.
# Performs a complete cleanup: vectors → BM25 → Supabase Storage → local PDF
# → documents DB row → usage decrement.
#
# Contrast with DELETE /admin/file/{filename}: that endpoint requires cloud
# store to be configured and uses filename directly from Qdrant. This endpoint
# works with local-only deployments and correctly accounts for usage via
# the chunk_count stored in the documents table.

@router.delete("/documents/{doc_id}")
async def admin_delete_document(request: Request, doc_id: str):
    """
    Fully delete a document by its UUID (from the documents metadata table).

    Performs these steps in order:
      1. Fetch document row — verify it belongs to this tenant (security check).
      2. Delete vectors from tenant Qdrant collection.
      3. Delete BM25 index entries for this file.
      4. Delete PDF from Supabase Storage (scoped path: pdfs/{slug}/{filename}).
      5. Delete local PDF from data/pdfs/ viewer store.
      6. Remove SHA-256 hash record (allows re-ingestion of the same file later).
      7. Delete row from `documents` table.
      8. Decrement tenant_usage.vector_count by doc.chunk_count.

    Returns:
        { "deleted": true, "filename": "...", "vectors_freed": N, "doc_id": "..." }
    """
    tenant_id   = request.state.tenant_id
    tenant_slug = request.state.tenant_slug
    sb          = get_supabase_admin()

    # ── 1. Fetch document row — verify ownership ──────────────────────────────
    doc_result = (
        sb.table("documents")
        .select("id, filename, chunk_count, file_size, status, storage_path")
        .eq("id", doc_id)
        .eq("tenant_id", tenant_id)   # SECURITY: scope to this tenant only
        .single()
        .execute()
    )

    if not doc_result.data:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = (
                f"Document '{doc_id}' not found. "
                "It may have already been deleted or belong to a different tenant."
            ),
        )

    doc      = doc_result.data
    filename = doc["filename"]
    chunk_count = doc.get("chunk_count", 0)

    logger.info(
        "[ADMIN/DELETE-DOC] Starting delete — tenant=%s  doc_id=%s  file=%s  chunks=%d",
        tenant_slug, doc_id, filename, chunk_count,
    )

    # ── 2. Delete vectors from tenant Qdrant collection ───────────────────────
    vs, bm25 = get_tenant_stores(tenant_slug)

    vectors_deleted = 0
    try:
        vectors_deleted = await run_in_threadpool(vs.delete_by_source, filename)
        logger.info(
            "[ADMIN/DELETE-DOC] Vectors deleted — tenant=%s  file=%s  count=%d",
            tenant_slug, filename, vectors_deleted,
        )
    except Exception as exc:
        logger.error(
            "[ADMIN/DELETE-DOC] Vector delete failed — tenant=%s  file=%s: %s",
            tenant_slug, filename, exc,
        )
        # Non-fatal: continue cleanup — partial state is correctable by reconciliation.

    # ── 3. Delete BM25 entries ────────────────────────────────────────────────
    try:
        bm25_removed = bm25.delete_by_source(filename)
        logger.info(
            "[ADMIN/DELETE-DOC] BM25 entries removed — file=%s  count=%d",
            filename, bm25_removed,
        )
    except Exception as exc:
        logger.warning(
            "[ADMIN/DELETE-DOC] BM25 delete failed — file=%s: %s", filename, exc,
        )

    # ── 4. Delete from Supabase Storage (tenant-scoped path) ─────────────────
    try:
        from services.supabase_storage import delete_pdf_from_supabase
        deleted_storage = delete_pdf_from_supabase(filename, tenant_slug=tenant_slug)
        if deleted_storage:
            logger.info(
                "[ADMIN/DELETE-DOC] Supabase Storage deleted — path=pdfs/%s/%s",
                tenant_slug, filename,
            )
        else:
            logger.warning(
                "[ADMIN/DELETE-DOC] Supabase Storage delete returned False — "
                "path=pdfs/%s/%s (may not have existed or storage not configured)",
                tenant_slug, filename,
            )
    except Exception as exc:
        logger.warning(
            "[ADMIN/DELETE-DOC] Supabase Storage delete exception — file=%s: %s",
            filename, exc,
        )

    # ── 5. Delete local PDF from viewer store (data/pdfs/) ───────────────────
    try:
        _delete_pdf_file(filename)
        logger.info("[ADMIN/DELETE-DOC] Local PDF removed — file=%s", filename)
    except Exception as exc:
        logger.warning(
            "[ADMIN/DELETE-DOC] Local PDF delete failed — file=%s: %s", filename, exc,
        )

    # ── 6. Remove SHA-256 hash record (allows future re-ingestion) ───────────
    try:
        _remove_hash_for_file(filename)
    except Exception as exc:
        logger.warning(
            "[ADMIN/DELETE-DOC] Hash removal failed — file=%s: %s", filename, exc,
        )

    # ── 7. Delete row from `documents` table ─────────────────────────────────
    try:
        sb.table("documents").delete().eq("id", doc_id).execute()
        logger.info(
            "[ADMIN/DELETE-DOC] documents row deleted — doc_id=%s", doc_id,
        )
    except Exception as exc:
        logger.error(
            "[ADMIN/DELETE-DOC] documents row delete failed — doc_id=%s: %s",
            doc_id, exc,
        )
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Failed to remove document record. Please try again.",
        )

    # ── 8. Decrement tenant_usage.vector_count ────────────────────────────────
    if chunk_count > 0:
        try:
            plan_svc = PlanService(sb)
            await run_in_threadpool(plan_svc.decrement_vectors, tenant_id, chunk_count)
            logger.info(
                "[ADMIN/DELETE-DOC] Usage decremented — tenant=%s  -%d vectors",
                tenant_slug, chunk_count,
            )
        except Exception as exc:
            # Non-fatal: nightly reconciliation will correct any drift.
            logger.error(
                "[ADMIN/DELETE-DOC] Usage decrement failed — tenant=%s: %s",
                tenant_slug, exc,
            )

    logger.info(
        "[ADMIN/DELETE-DOC] ✅ Complete — tenant=%s  file=%s  "
        "vectors_freed=%d  doc_id=%s",
        tenant_slug, filename, vectors_deleted, doc_id,
    )

    return {
        "deleted"      : True,
        "doc_id"       : doc_id,
        "filename"     : filename,
        "vectors_freed": vectors_deleted,
        "message"      : (
            f"'{filename}' deleted. {vectors_deleted} vectors freed."
        ),
    }


# ── GET /admin/usage ──────────────────────────────────────────────────────────
#
# Phase 4: Returns structured usage stats for this tenant, merged with plan
# limits and expressed as percentages. Provides all the data an admin dashboard
# needs to render usage meters.

@router.get("/usage")
async def admin_usage(request: Request):
    """
    Return plan usage stats for this tenant merged with plan limits.

    The response includes raw counts, plan limits, and percentage utilisation
    for both vectors and user seats. Also includes tenant status and plan name.

    Response shape:
        {
          "vectors": {
            "used"   : 15000,
            "limit"  : 200000,
            "percent": 7.5
          },
          "users": {
            "used"   : 8,
            "limit"  : 50,
            "percent": 16.0
          },
          "status"         : "active",   // trial | active | over_quota | suspended
          "plan"           : "Growth",
          "plan_id"        : "uuid",
          "trial_ends_at"  : "2025-02-01T00:00:00Z" | null,
          "last_ingestion" : "2025-01-15T10:30:00Z" | null
        }
    """
    tenant_id = request.state.tenant_id
    sb        = get_supabase_admin()

    # ── Fetch tenant (status, trial_ends_at) + plan (limits, name) ────────────
    tenant_result = (
        sb.table("tenants")
        .select("status, trial_ends_at, plan_id, plans(id, name, max_vectors, max_users)")
        .eq("id", tenant_id)
        .single()
        .execute()
    )

    if not tenant_result.data:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Tenant record not found.",
        )

    tenant = tenant_result.data
    plan   = tenant.get("plans") or {}

    max_vectors = plan.get("max_vectors", 0)
    max_users   = plan.get("max_users", 0)

    # ── Fetch usage row ────────────────────────────────────────────────────────
    usage_result = (
        sb.table("tenant_usage")
        .select("vector_count, user_count, last_ingestion")
        .eq("tenant_id", tenant_id)
        .single()
        .execute()
    )

    usage = usage_result.data or {}
    used_vectors = usage.get("vector_count", 0)
    used_users   = usage.get("user_count",   0)
    last_ingestion = usage.get("last_ingestion")

    # ── Calculate percentages (guard div/0) ───────────────────────────────────
    def _pct(used: int, limit: int) -> float:
        if limit <= 0:
            return 0.0
        return round((used / limit) * 100, 2)

    logger.debug(
        "[ADMIN/USAGE] tenant=%s  vectors=%d/%d  users=%d/%d  status=%s",
        request.state.tenant_slug,
        used_vectors, max_vectors,
        used_users, max_users,
        tenant.get("status"),
    )

    return {
        "vectors": {
            "used"   : used_vectors,
            "limit"  : max_vectors,
            "percent": _pct(used_vectors, max_vectors),
        },
        "users": {
            "used"   : used_users,
            "limit"  : max_users,
            "percent": _pct(used_users, max_users),
        },
        "status"       : tenant.get("status"),
        "plan"         : plan.get("name"),
        "plan_id"      : plan.get("id"),
        "display_name" : request.state.tenant.get("display_name", ""),
        "slug"         : request.state.tenant_slug,
        "trial_ends_at": tenant.get("trial_ends_at"),
        "last_ingestion": last_ingestion,
    }


# ── POST /admin/ingest ────────────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse)
async def admin_ingest(request: Request, files: list[UploadFile] = File(...)):
    """
    Upload and index one or more PDFs into the knowledge base.
    Admin only — requires a valid admin JWT.

    Pre-flight checks (Phase 4 addition):
      1. Batch size — rejects with HTTP 400 if file count exceeds plan limit.
      2. Quota      — rejects with HTTP 402 if the tenant is already at or over
                      their vector limit (no point ingesting anything).

    Partial ingestion (Phase 4 addition):
      If there IS remaining capacity but it is less than what the full batch
      would consume, _ingest_files_sync is called with vector_cap set to the
      remaining headroom. Files are indexed until the cap is reached; any files
      that couldn't be processed are reported in remaining_files and the response
      status is set to "partial".

    Post-flight (after _ingest_files_sync succeeds):
      - Increments tenant_usage.vector_count by actual chunk count.
      - Inserts one row into the documents table per successfully indexed file.
      - Calls _check_and_update_quota_status to flip tenant to over_quota
        immediately if the cap was hit mid-batch.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    tenant_id   = request.state.tenant_id
    tenant_slug = request.state.tenant_slug

    # ── Initialise PlanService (used in both pre-flight and post-flight) ───────
    supabase = get_supabase_admin()
    plan_svc = PlanService(supabase)

    # ── PRE-FLIGHT CHECK 1 — Batch size ───────────────────────────────────────
    # Reject if the number of files exceeds the plan's per-batch PDF limit.
    ok, err = await run_in_threadpool(
        plan_svc.check_batch_size, tenant_id, len(files)
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    # ── PRE-FLIGHT CHECK 2 — Hard quota gate ──────────────────────────────────
    # If the tenant is already AT or OVER their vector limit, block the upload
    # immediately — there is zero remaining capacity so partial ingestion would
    # yield nothing useful.
    try:
        remaining_capacity = await run_in_threadpool(
            plan_svc.get_remaining_vectors, tenant_id
        )
    except Exception as exc:
        # get_remaining_vectors already returns a large sentinel on error, but
        # the run_in_threadpool wrapper could also raise. Fail open so a DB
        # hiccup doesn't block every admin upload.
        logger.error(
            "[ADMIN/INGEST] get_remaining_vectors raised unexpectedly — "
            "tenant=%s: %s — failing open",
            tenant_slug, exc,
        )
        remaining_capacity = 999_999_999

    if remaining_capacity <= 0:
        # Fetch the actual limit for a user-friendly error message.
        try:
            plan_data   = await run_in_threadpool(plan_svc.get_plan, tenant_id)
            max_vectors = plan_data.get("max_vectors", 0)
        except Exception:
            max_vectors = 0

        logger.warning(
            "[ADMIN/INGEST] Pre-flight quota block — tenant=%s  "
            "remaining=%d  limit=%d",
            tenant_slug, remaining_capacity, max_vectors,
        )
        raise HTTPException(
            status_code = status.HTTP_402_PAYMENT_REQUIRED,
            detail      = {
                "code"   : "over_quota",
                "message": (
                    f"Vector limit reached. You have 0 vectors remaining "
                    f"out of {max_vectors:,}. "
                    "Upgrade your plan or delete documents to continue."
                ),
            },
        )

    logger.info(
        "[ADMIN/INGEST] Pre-flight OK — tenant=%s  remaining_capacity=%d  files=%d",
        tenant_slug, remaining_capacity, len(files),
    )

    # ── Capture file sizes NOW — before any .read() calls consume the stream ─
    file_sizes: dict[str, int] = {
        f.filename: (f.size or 0)
        for f in files
    }

    # ── Save original PDFs for the viewer ─────────────────────────────────────
    pdfs_dir = Path(settings.qdrant_path).parent / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    for file in files:
        if file.filename.lower().endswith(".pdf"):
            dest_path = pdfs_dir / file.filename
            content   = await file.read()
            with open(dest_path, "wb") as f:
                f.write(content)
            await file.seek(0)

    tmp_dir    = Path("/tmp") / f"rag_admin_ingest_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    file_paths: list[tuple[str, str]] = []

    try:
        for upload in files:
            tmp_path = tmp_dir / upload.filename
            content  = await upload.read()
            tmp_path.write_bytes(content)
            file_paths.append((str(tmp_path), upload.filename))

        # ── Run ingestion with remaining capacity as a hard cap ───────────────
        # _ingest_files_sync accepts an optional vector_cap kwarg (Phase 4).
        # When set it stops indexing once that many chunks have been added,
        # returning quota_hit=True and remaining_files=[...] for skipped files.
        result = await run_in_threadpool(
            _ingest_files_sync,
            file_paths,
            tenant_slug,
            remaining_capacity,   # vector_cap positional arg — see routers/ingest.py
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── POST-FLIGHT: Usage accounting ─────────────────────────────────────────
    actual_chunks  = result["total_chunks"]
    file_chunk_map = result.get("file_chunks", {})
    quota_hit      = result.get("quota_hit", False)
    remaining_files = result.get("remaining_files", [])

    if actual_chunks > 0:
        try:
            await run_in_threadpool(
                plan_svc.increment_vectors, tenant_id, actual_chunks
            )
            logger.info(
                "[ADMIN/INGEST] Vector count incremented — tenant=%s  +%d chunks",
                tenant_slug, actual_chunks,
            )
        except Exception as exc:
            logger.error(
                "[ADMIN/INGEST] increment_vectors failed — tenant=%s: %s",
                tenant_slug, exc,
            )

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
            logger.info(
                "[ADMIN/INGEST] Document recorded — tenant=%s  file=%s  chunks=%d",
                tenant_slug, filename, chunk_count,
            )
        except Exception as exc:
            logger.error(
                "[ADMIN/INGEST] record_document failed — tenant=%s  file=%s: %s",
                tenant_slug, filename, exc,
            )

    # ── POST-FLIGHT: Quota status sync ────────────────────────────────────────
    # If the cap was hit mid-batch, flip the tenant to over_quota immediately
    # rather than waiting for the nightly reconciliation task.
    if quota_hit:
        try:
            await run_in_threadpool(
                plan_svc._check_and_update_quota_status, tenant_id
            )
            logger.info(
                "[ADMIN/INGEST] Quota status synced after partial ingest — tenant=%s",
                tenant_slug,
            )
        except Exception as exc:
            logger.error(
                "[ADMIN/INGEST] _check_and_update_quota_status failed — tenant=%s: %s",
                tenant_slug, exc,
            )

    logger.info(
        "[ADMIN/INGEST] ✅ tenant=%s  files=%s  chunks=%d  quota_hit=%s  skipped_quota=%s",
        tenant_slug,
        result["files_indexed"],
        actual_chunks,
        quota_hit,
        remaining_files,
    )

    # Build a human-readable message that covers both the normal and partial cases.
    base_msg = (
        f"Indexed {len(result['files_indexed'])} file(s). "
        f"Skipped {len(result['skipped'])} duplicate(s)."
    )
    quota_msg = (
        f" Vector quota reached — {len(remaining_files)} file(s) not indexed: "
        f"{', '.join(remaining_files)}. Upgrade your plan to continue."
        if quota_hit else ""
    )

    return IngestResponse(
        status          = "partial" if quota_hit else "ok",
        files_indexed   = result["files_indexed"],
        total_chunks    = result["total_chunks"],
        total_parents   = result["total_parents"],
        message         = base_msg + quota_msg,
        quota_hit       = quota_hit,
        remaining_files = remaining_files,
    )

# ── DELETE /admin/file/{filename} ─────────────────────────────────────────────

@router.delete("/file/{filename}", response_model=DeleteFileResponse)
async def admin_delete_file(request: Request, filename: str):
    """
    Delete a file from the knowledge base by filename.
    Admin only — requires a valid admin JWT.

    NOTE: For Phase 4+, prefer DELETE /admin/documents/{doc_id} which uses
    the documents table for accurate usage accounting. This endpoint is kept
    for backward compatibility and requires cloud store to be configured.
    """
    tenant_slug = request.state.tenant_slug
    vs, bm25    = get_tenant_stores(tenant_slug)

    # ── Guard 1: must be online ───────────────────────────────────────────────
    if not rag_service.is_online():
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = (
                f"Cannot delete '{filename}' while offline. "
                "Deletion requires a cloud connection. Please reconnect and try again."
            ),
        )

    # ── Guard 2: cloud store must be configured ───────────────────────────────
    cloud_store = rag_service.get_cloud_store()
    if cloud_store is None:
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail      = (
                f"Cannot delete '{filename}': no cloud store is configured. "
                "Set QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY in .env."
            ),
        )

    # ── Guard 3: file must exist in tenant store ──────────────────────────────
    tenant_sources = vs.list_sources()
    if filename not in tenant_sources:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = f"File '{filename}' not found in the knowledge base.",
        )

    # ── Delete from tenant vector store ──────────────────────────────────────
    vectors_deleted = await run_in_threadpool(vs.delete_by_source, filename)

    # ── Clean up tenant BM25 immediately ─────────────────────────────────────
    bm25.delete_by_source(filename)

    # ── Also clean up global cloud store if it mirrors this tenant's data ─────
    # (belt-and-suspenders — harmless no-op if cloud has no matching docs)
    try:
        cloud_sources = cloud_store.list_sources()
        if filename in cloud_sources:
            await run_in_threadpool(rag_service.delete_file_from_cloud, filename)
    except Exception as exc:
        logger.warning(
            "[ADMIN/DELETE] Cloud store cleanup skipped: %s", exc
        )

    _remove_hash_for_file(filename)
    _delete_pdf_file(filename)

    logger.info(
        "[ADMIN/DELETE] ✅ tenant=%s  file=%s  vectors_deleted=%d",
        tenant_slug, filename, vectors_deleted,
    )

    return DeleteFileResponse(
        status          = "ok",
        filename        = filename,
        vectors_deleted = vectors_deleted,
        message         = (
            f"Deleted '{filename}': {vectors_deleted} vectors removed."
        ),
    )


# ── DELETE /admin/collection ──────────────────────────────────────────────────

@router.delete("/collection", response_model=WipeResponse)
async def admin_wipe(request: Request):
    """
    Wipe the entire knowledge base (vectors + BM25 + hash registry) for this tenant.
    Admin only — irreversible.
    """
    tenant_slug = request.state.tenant_slug
    vs, bm25    = get_tenant_stores(tenant_slug)

    logger.warning(
        "[ADMIN/WIPE] ⚠  Wipe requested — tenant=%s  THIS IS IRREVERSIBLE",
        tenant_slug,
    )

    vs.reset_collection()
    bm25.reset()
    _wipe_hashes()

    logger.info("[ADMIN/WIPE] ✅ Knowledge base wiped — tenant=%s", tenant_slug)
    return WipeResponse(status="ok", message="Knowledge base wiped.")


# ── GET /admin/files ──────────────────────────────────────────────────────────

@router.get("/files")
async def admin_list_files(request: Request):
    """
    List all indexed files in this tenant's knowledge base.
    Admin only — requires a valid admin JWT.

    Returns simple filename list from the vector store (Qdrant sources).
    For richer metadata (chunk_count, file_size, status), use GET /admin/documents.
    """
    tenant_slug = request.state.tenant_slug
    vs, _       = get_tenant_stores(tenant_slug)
    files       = vs.list_sources()

    logger.debug(
        "[ADMIN/FILES] tenant=%s  count=%d", tenant_slug, len(files)
    )
    return {"files": files}


# ── GET /admin/stats ──────────────────────────────────────────────────────────

@router.get("/stats")
async def admin_stats(request: Request):
    """
    Full system stats for this tenant's knowledge base.
    Admin only — requires a valid admin JWT.
    """
    tenant_slug = request.state.tenant_slug
    vs, bm25    = get_tenant_stores(tenant_slug)

    stats = {
        "tenant_slug"    : tenant_slug,
        "total_vectors"  : vs.count(),
        "bm25_docs"      : len(bm25),
        "indexed_files"  : vs.list_sources(),
        "embedding_model": settings.embedding_model,
        "llm_model"      : settings.groq_model,
        "collection"     : f"rag_docs_{tenant_slug}",
    }

    logger.debug(
        "[ADMIN/STATS] tenant=%s  vectors=%d  bm25=%d",
        tenant_slug, stats["total_vectors"], stats["bm25_docs"],
    )
    return stats


# ── POST /admin/onboarding-complete ──────────────────────────────────────────
#
# Called by OnboardingPage after the wizard finishes.
# Sets app_metadata.onboarding_complete = True on the Supabase auth user
# so the frontend JWT (after refreshSession) picks up the flag and
# isOnboardingComplete() returns True.

@router.post("/onboarding-complete")
async def admin_onboarding_complete(request: Request):
    """
    Mark onboarding as complete for the current admin user.

    Updates Supabase auth.users app_metadata with onboarding_complete: true.
    The frontend calls supabase.auth.refreshSession() after this so the new
    JWT carries the flag and AuthRedirect stops sending the user to /onboarding.

    Returns:
        { "message": "Onboarding complete." }
    """
    user_id = request.state.user_id
    sb      = get_supabase_admin()

    try:
        sb.auth.admin.update_user_by_id(
            user_id,
            {"app_metadata": {"onboarding_complete": True}},
        )
        logger.info(
            "[ADMIN/ONBOARDING] ✅ Marked complete — user=%s  tenant=%s",
            user_id, request.state.tenant_slug,
        )
    except Exception as exc:
        logger.error(
            "[ADMIN/ONBOARDING] Failed to set onboarding flag — user=%s: %s",
            user_id, exc,
        )
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Could not complete onboarding. Please try again.",
        )

    return {"message": "Onboarding complete."}


__all__ = ["router"]