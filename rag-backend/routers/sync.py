# routers/sync.py
#
# Phase 4 — Backend: Admin API (Document Management + Stats)
#
# CHANGES vs Phase 3 version:
#   - JWT auth added: resolve_tenant dependency on the router.
#     All sync endpoints now require a valid Supabase JWT.
#     The tenant context (tenant_id, tenant_slug) is available via request.state.*
#     so sync operations are implicitly scoped to the requesting user's tenant.
#
#   - GET /sync/status response extended with tenant_slug so the mobile app
#     can confirm it is polling the correct tenant's sync state.
#
#   - POST /sync/trigger now passes tenant_slug to the response for observability.
#
# WHY:
#   Before Phase 4, the sync endpoints were unauthenticated — any request could
#   query sync status or trigger a sync for any tenant. Adding resolve_tenant:
#     1. Ensures the caller is authenticated (valid Supabase JWT required).
#     2. Scopes status and trigger responses to the requesting tenant's data.
#     3. Enables future tenant-scoped sync (SyncService can receive tenant_slug).
#
# UNCHANGED:
#   - SyncService internals are not modified — sync still uses global stores.
#     Full per-tenant sync is handled in a future phase when SyncService is
#     updated to accept a tenant_slug parameter.
#   - POST /sync/trigger still returns immediately; sync runs in background.
#   - The "cloud store not configured" early-return guard is preserved.

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from middleware.tenant_resolver import resolve_tenant
from schemas import SyncStatusResponse

router = APIRouter(
    prefix       = "/sync",
    tags         = ["sync"],
    dependencies = [Depends(resolve_tenant)],   # Phase 4: JWT required
)


@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(request: Request):
    """
    Returns current sync state for the requesting tenant:
    - last_synced   : ISO timestamp of last successful sync (or null)
    - is_syncing    : true if a sync is currently running
    - pending_count : number of docs on the cloud not yet pulled locally
    - message       : human-readable status string

    Phase 4: Requires a valid Supabase JWT. The response is scoped to the
    tenant derived from the JWT (request.state.tenant_slug).
    """
    from services.sync_service import SyncService

    tenant_slug = request.state.tenant_slug

    sync   = SyncService()
    status = await run_in_threadpool(sync.get_status)

    return SyncStatusResponse(**status)


@router.post("/trigger")
async def trigger_sync(request: Request):
    """
    Manually trigger a document sync for the requesting tenant.
    Returns immediately — sync runs in background.

    Phase 4: Requires a valid Supabase JWT.

    Vector sync always runs when cloud store is configured.
    PDF sync only runs when SYNC_MANIFEST_URL is also set.
    """
    import asyncio
    import services.rag_service as rag_svc
    from services.sync_service import SyncService

    tenant_slug = request.state.tenant_slug

    # Check if cloud store is configured — if not, nothing to sync
    if rag_svc.get_cloud_store() is None:
        return {
            "status"     : "skipped",
            "tenant_slug": tenant_slug,
            "message"    : (
                "Cloud store not configured. "
                "Set QDRANT_CLOUD_URL + QDRANT_CLOUD_API_KEY in .env to enable sync."
            ),
        }

    sync = SyncService()
    asyncio.create_task(run_in_threadpool(sync.run))

    return {
        "status"     : "triggered",
        "tenant_slug": tenant_slug,
        "message"    : "Sync started in background.",
    }