# routers/super_admin.py
#
# Phase 5-B — Super Admin Portal API
#
# PURPOSE:
#   All /super-admin/* endpoints.  Restricted exclusively to users whose
#   Supabase JWT carries role="super_admin".  IP-allowlist enforcement and
#   audit logging are handled by the require_super_admin dependency
#   (middleware/super_admin_auth.py) — every route in this file inherits
#   both automatically.
#
# ENDPOINT GROUPS:
#   Tenant management  — list, detail, patch, reconcile, impersonate, delete doc
#   Plan management    — list, create, patch, retire
#   Member management  — list, remove, promote
#   Bulk operations    — plan-change, trial-extend, suspend, config-push
#   Activity & alerts  — audit_log feed, unread alerts, mark-read
#
# DEPENDENCY CHAIN:
#   Phase 1 — middleware/tenant_resolver.py   (resolve_tenant, via require_super_admin)
#   Phase 1 — services/supabase_client.py     (get_supabase_admin)
#   Phase 1 — services/rag_service.py         (get_tenant_stores, for reconcile)
#   Phase 3 — services/plan_service.py        (PlanService, for reconcile + doc delete)
#   Phase 3 — services/supabase_storage.py    (delete_pdf_from_supabase)
#   Phase 5 — middleware/super_admin_auth.py  (require_super_admin)
#   Phase 5 — services/audit_service.py       (log_audit)
#
# NEVER TOUCH:
#   chains/rag_chain.py, embeddings/, generation/, ingestion/, retrieval/ —
#   the RAG pipeline is zero-touch.  Only the objects passed into it change.
#
# PAGINATION CONVENTION:
#   All list endpoints accept ?page=1&page_size=25 query params.
#   Responses always include { total, page, page_size, items: [...] }.
#
# ERROR RESPONSES:
#   All errors follow FastAPI's default { "detail": "..." } shape.
#   Where more context is needed the detail is a dict: { "code": ..., "message": ... }.

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from middleware.super_admin_auth import require_super_admin
from services.audit_service import log_audit
from services.supabase_client import get_supabase_admin
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Router — every route is protected by require_super_admin ─────────────────
router = APIRouter(
    prefix="/super-admin",
    tags=["super-admin"],
    dependencies=[Depends(require_super_admin)],
)


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class PatchTenantBody(BaseModel):
    display_name    : str | None = None
    plan_id         : str | None = None
    status          : str | None = None   # trial | active | over_quota | suspended
    config_overrides: dict | None = None
    trial_ends_at   : str | None = None   # ISO-8601 string


class CreatePlanBody(BaseModel):
    name          : str
    max_users     : int = 5
    max_vectors   : int = 10_000
    max_batch_pdfs: int = 3
    allowed_modes : list[str] = ["online"]
    price_monthly : float = 0.0


class PatchPlanBody(BaseModel):
    name          : str | None = None
    max_users     : int | None = None
    max_vectors   : int | None = None
    max_batch_pdfs: int | None = None
    allowed_modes : list[str] | None = None
    price_monthly : float | None = None


class PromoteMemberBody(BaseModel):
    role: str   # 'admin' | 'user' | 'super_admin'


class BulkPlanChangeBody(BaseModel):
    tenant_ids: list[str]
    plan_id   : str


class BulkTrialExtendBody(BaseModel):
    tenant_ids: list[str]
    days      : int = 7


class BulkSuspendBody(BaseModel):
    tenant_ids: list[str]


class BulkConfigPushBody(BaseModel):
    plan_id      : str
    config_patch : dict   # keys to set on config_overrides (only for tenants that haven't customised)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _paginate(query_result: list, page: int, page_size: int) -> dict:
    """Slice a list into a paginated response envelope."""
    total = len(query_result)
    start = (page - 1) * page_size
    end   = start + page_size
    return {
        "total"    : total,
        "page"     : page,
        "page_size": page_size,
        "items"    : query_result[start:end],
    }


def _actor(request: Request) -> str:
    return getattr(request.state, "user_email", "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# TENANT MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/tenants", summary="List all tenants (paginated + filtered)")
async def list_tenants(
    request  : Request,
    page     : int = Query(1,  ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search   : str = Query(""),
    plan_id  : str = Query(""),
    status   : str = Query(""),   # trial | active | over_quota | suspended
):
    """
    Returns a paginated, filterable list of all tenants with their joined
    plan data and usage row.

    Filters are additive (AND).  Leave a filter blank to skip it.
    """
    def _fetch():
        sb = get_supabase_admin()
        q  = sb.table("tenants").select("*, plans(*), tenant_usage(*)")

        if search:
            # Supabase PostgREST: ilike on display_name or slug
            q = q.or_(f"display_name.ilike.%{search}%,slug.ilike.%{search}%")
        if plan_id:
            q = q.eq("plan_id", plan_id)
        if status:
            q = q.eq("status", status)

        result = q.order("created_at", desc=True).execute()
        return result.data or []

    tenants = await run_in_threadpool(_fetch)
    return _paginate(tenants, page, page_size)


@router.get("/tenants/{tenant_id}", summary="Full tenant detail")
async def get_tenant(tenant_id: str):
    """
    Returns the full tenant record including plan, usage, members, and
    documents for the given tenant_id.
    """
    def _fetch():
        sb = get_supabase_admin()
        tenant = (
            sb.table("tenants")
            .select("*, plans(*), tenant_usage(*)")
            .eq("id", tenant_id)
            .single()
            .execute()
            .data
        )
        if not tenant:
            return None, None, None

        members = (
            sb.table("tenant_members")
            .select("*")
            .eq("tenant_id", tenant_id)
            .execute()
            .data or []
        )
        documents = (
            sb.table("documents")
            .select("*")
            .eq("tenant_id", tenant_id)
            .order("ingested_at", desc=True)
            .execute()
            .data or []
        )
        return tenant, members, documents

    tenant, members, documents = await run_in_threadpool(_fetch)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    return {
        **tenant,
        "members"  : members,
        "documents": documents,
    }


@router.patch("/tenants/{tenant_id}", summary="Update tenant plan, status, config, or trial")
async def patch_tenant(
    tenant_id: str,
    body     : PatchTenantBody,
    request  : Request,
):
    """
    Update one or more mutable fields on a tenant row.

    Fields:
      - display_name    : rename the tenant's organisation.
      - plan_id         : change billing plan (takes effect immediately).
      - status          : manually set trial | active | over_quota | suspended.
      - config_overrides: merge-patch the tenant's config (deep-merged on read,
                          stored as provided here — caller must send full object
                          if they want to preserve existing keys).
      - trial_ends_at   : ISO-8601 datetime string.

    All changes are written to the audit_log.
    """
    # Build the update dict from non-None fields only
    updates: dict[str, Any] = {}
    if body.display_name is not None:
        updates["display_name"] = body.display_name
    if body.plan_id is not None:
        updates["plan_id"] = body.plan_id
    if body.status is not None:
        valid_statuses = {"trial", "active", "over_quota", "suspended"}
        if body.status not in valid_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{body.status}'. Must be one of {sorted(valid_statuses)}.",
            )
        updates["status"] = body.status
    if body.config_overrides is not None:
        updates["config_overrides"] = body.config_overrides
    if body.trial_ends_at is not None:
        updates["trial_ends_at"] = body.trial_ends_at

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    def _update():
        sb = get_supabase_admin()
        # Verify tenant exists
        existing = (
            sb.table("tenants")
            .select("id, slug, status, plan_id")
            .eq("id", tenant_id)
            .single()
            .execute()
            .data
        )
        if not existing:
            return None
        result = (
            sb.table("tenants")
            .update(updates)
            .eq("id", tenant_id)
            .execute()
        )
        return existing

    existing = await run_in_threadpool(_update)
    if not existing:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    # Audit — record what changed
    await log_audit(
        actor_email = _actor(request),
        action      = "tenant_updated",
        tenant_id   = tenant_id,
        payload     = {"changes": updates, "previous": existing},
    )

    logger.info(
        "[SUPER_ADMIN] Tenant updated — id=%s  changes=%s  actor=%s",
        tenant_id, list(updates.keys()), _actor(request),
    )
    return {"updated": True, "changes": updates}


@router.post("/tenants/{tenant_id}/reconcile", summary="Force vector count reconciliation")
async def reconcile_tenant(tenant_id: str, request: Request):
    """
    Immediately reconcile the stored vector_count in tenant_usage against the
    real point count in the vector store.  Updates the stored count if drift
    exceeds 5 % and writes an alert.

    This is the same logic as the nightly reconciliation task but scoped to
    one tenant and triggered on-demand.
    """
    def _reconcile():
        from services.supabase_client import get_supabase_admin
        from vectorstore.factory import get_vector_store
        from services.rag_service import get_tenant_stores

        sb = get_supabase_admin()

        # Fetch tenant slug
        tenant_row = (
            sb.table("tenants")
            .select("id, slug")
            .eq("id", tenant_id)
            .single()
            .execute()
            .data
        )
        if not tenant_row:
            return None

        slug = tenant_row["slug"]

        # Get real count from vector store
        vs, _ = get_tenant_stores(slug)
        real_count = vs.count()

        # Get stored count
        usage = (
            sb.table("tenant_usage")
            .select("vector_count")
            .eq("tenant_id", tenant_id)
            .single()
            .execute()
            .data
        )
        stored_count = usage["vector_count"] if usage else 0

        drift_pct = (
            abs(real_count - stored_count) / max(real_count, 1) * 100
            if real_count > 0
            else (100.0 if stored_count > 0 else 0.0)
        )

        corrected = False
        if drift_pct > 5:
            sb.table("tenant_usage").update({
                "vector_count": real_count,
                "updated_at"  : datetime.now(timezone.utc).isoformat(),
            }).eq("tenant_id", tenant_id).execute()

            # Insert alert
            sb.table("alerts").insert({
                "tenant_id": tenant_id,
                "type"     : "manual_reconciliation",
                "message"  : (
                    f"Super-admin manual reconciliation: "
                    f"count corrected from {stored_count:,} to {real_count:,} "
                    f"({drift_pct:.1f}% drift)."
                ),
            }).execute()
            corrected = True

        return {
            "tenant_id"    : tenant_id,
            "slug"         : slug,
            "real_count"   : real_count,
            "stored_count" : stored_count,
            "drift_pct"    : round(drift_pct, 2),
            "corrected"    : corrected,
        }

    result = await run_in_threadpool(_reconcile)
    if result is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "tenant_reconciled",
        tenant_id   = tenant_id,
        payload     = result,
    )

    logger.info(
        "[SUPER_ADMIN] Reconcile — tenant=%s  real=%d  stored=%d  corrected=%s",
        tenant_id,
        result["real_count"],
        result["stored_count"],
        result["corrected"],
    )
    return result


@router.post("/tenants/{tenant_id}/impersonate", summary="Read-only impersonation view")
async def impersonate_tenant(tenant_id: str, request: Request):
    """
    Returns a read-only snapshot of a tenant's current state.

    This endpoint performs NO writes on behalf of the tenant — it is strictly
    an observation tool.  Every call is written to the audit_log.

    Returns the same shape as GET /admin/usage + document list + join code.
    """
    def _snapshot():
        sb = get_supabase_admin()

        tenant = (
            sb.table("tenants")
            .select("*, plans(*), tenant_usage(*)")
            .eq("id", tenant_id)
            .single()
            .execute()
            .data
        )
        if not tenant:
            return None

        documents = (
            sb.table("documents")
            .select("*")
            .eq("tenant_id", tenant_id)
            .order("ingested_at", desc=True)
            .execute()
            .data or []
        )
        return {
            "tenant_id"  : tenant_id,
            "slug"       : tenant["slug"],
            "display_name": tenant["display_name"],
            "status"     : tenant["status"],
            "join_code"  : tenant["join_code"],
            "plan"       : tenant.get("plans", {}),
            "usage"      : tenant.get("tenant_usage", {}),
            "documents"  : documents,
            "_impersonation_warning": (
                "READ-ONLY VIEW — no actions are performed on behalf of this tenant."
            ),
        }

    snapshot = await run_in_threadpool(_snapshot)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "tenant_impersonated",
        tenant_id   = tenant_id,
        payload     = {"path": str(request.url.path)},
    )

    return snapshot


@router.delete(
    "/tenants/{tenant_id}/documents/{doc_id}",
    summary="Delete a document on behalf of a tenant",
)
async def delete_tenant_document(
    tenant_id: str,
    doc_id   : str,
    request  : Request,
):
    """
    Delete a specific document on behalf of a tenant.

    Steps (mirrors Phase 4 admin delete, but scoped by super-admin):
      1. Fetch document row — verify it belongs to the given tenant.
      2. Delete vectors from the tenant's vector store by source filename.
      3. Remove entries from the tenant's BM25 index and rebuild.
      4. Delete the file from Supabase Storage.
      5. Delete the document row from the documents table.
      6. Decrement tenant_usage.vector_count by doc.chunk_count.
      7. Write audit log entry.
    """
    def _delete():
        sb = get_supabase_admin()

        # 1. Fetch + verify ownership
        doc = (
            sb.table("documents")
            .select("*")
            .eq("id", doc_id)
            .eq("tenant_id", tenant_id)
            .single()
            .execute()
            .data
        )
        if not doc:
            return None, "Document not found or does not belong to this tenant."

        tenant_row = (
            sb.table("tenants")
            .select("slug")
            .eq("id", tenant_id)
            .single()
            .execute()
            .data
        )
        if not tenant_row:
            return None, "Tenant not found."

        slug     = tenant_row["slug"]
        filename = doc["filename"]
        chunks   = doc.get("chunk_count", 0)

        errors: list[str] = []

        # 2. Delete vectors
        try:
            from services.rag_service import get_tenant_stores
            vs, bm25 = get_tenant_stores(slug)
            vs.delete_by_source(filename)
        except Exception as exc:
            errors.append(f"Vector delete failed: {exc}")
            logger.error("[SUPER_ADMIN] Vector delete error — doc=%s: %s", doc_id, exc)

        # 3. BM25 cleanup + rebuild
        try:
            bm25.delete_by_source(filename)
        except Exception as exc:
            errors.append(f"BM25 delete failed: {exc}")
            logger.warning("[SUPER_ADMIN] BM25 delete error — doc=%s: %s", doc_id, exc)

        # 4. Supabase Storage delete
        try:
            from services.supabase_storage import delete_pdf_from_supabase
            delete_pdf_from_supabase(filename=filename, tenant_slug=slug)
        except Exception as exc:
            errors.append(f"Storage delete failed: {exc}")
            logger.warning("[SUPER_ADMIN] Storage delete error — doc=%s: %s", doc_id, exc)

        # 5. Delete document row
        sb.table("documents").delete().eq("id", doc_id).execute()

        # 6. Decrement usage
        try:
            from services.plan_service import PlanService
            plan_svc = PlanService(sb)
            plan_svc.decrement_vectors(tenant_id, chunks)
        except Exception as exc:
            errors.append(f"Usage decrement failed: {exc}")
            logger.warning("[SUPER_ADMIN] Usage decrement error — doc=%s: %s", doc_id, exc)

        return doc, errors

    result, errors_or_msg = await run_in_threadpool(_delete)

    if result is None:
        raise HTTPException(status_code=404, detail=errors_or_msg)

    await log_audit(
        actor_email = _actor(request),
        action      = "document_deleted",
        tenant_id   = tenant_id,
        payload     = {
            "doc_id"  : doc_id,
            "filename": result["filename"],
            "chunks"  : result.get("chunk_count", 0),
            "errors"  : errors_or_msg,
        },
    )

    return {
        "deleted"      : True,
        "doc_id"       : doc_id,
        "filename"     : result["filename"],
        "vectors_freed": result.get("chunk_count", 0),
        "warnings"     : errors_or_msg,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PLAN MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/plans", summary="List all plan definitions")
async def list_plans():
    """Returns all plans (including retired ones) ordered by price."""
    def _fetch():
        return (
            get_supabase_admin()
            .table("plans")
            .select("*")
            .order("price_monthly")
            .execute()
            .data or []
        )

    return await run_in_threadpool(_fetch)


@router.post("/plans", status_code=status.HTTP_201_CREATED, summary="Create a new plan")
async def create_plan(body: CreatePlanBody, request: Request):
    """
    Create a new plan definition.  The plan is immediately available for
    tenant assignment.  Does not retroactively change any tenant's plan.
    """
    def _create():
        sb = get_supabase_admin()
        # Guard uniqueness on name
        existing = (
            sb.table("plans")
            .select("id")
            .eq("name", body.name)
            .execute()
            .data
        )
        if existing:
            return None
        result = (
            sb.table("plans")
            .insert({
                "name"          : body.name,
                "max_users"     : body.max_users,
                "max_vectors"   : body.max_vectors,
                "max_batch_pdfs": body.max_batch_pdfs,
                "allowed_modes" : body.allowed_modes,
                "price_monthly" : body.price_monthly,
                "is_active"     : True,
            })
            .execute()
        )
        return result.data[0] if result.data else None

    plan = await run_in_threadpool(_create)
    if plan is None:
        raise HTTPException(
            status_code=409,
            detail=f"A plan named '{body.name}' already exists.",
        )

    await log_audit(
        actor_email = _actor(request),
        action      = "plan_created",
        payload     = {"plan_id": plan.get("id"), "name": body.name},
    )
    return plan


@router.patch("/plans/{plan_id}", summary="Edit a plan definition")
async def patch_plan(plan_id: str, body: PatchPlanBody, request: Request):
    """
    Update a plan's limits or pricing.

    Changes apply to ALL tenants on this plan immediately — their limits
    are re-evaluated on the next request.  Use with caution; lowering limits
    may cause tenants to immediately enter over_quota status.
    """
    updates: dict[str, Any] = {}
    if body.name           is not None: updates["name"]           = body.name
    if body.max_users      is not None: updates["max_users"]      = body.max_users
    if body.max_vectors    is not None: updates["max_vectors"]    = body.max_vectors
    if body.max_batch_pdfs is not None: updates["max_batch_pdfs"] = body.max_batch_pdfs
    if body.allowed_modes  is not None: updates["allowed_modes"]  = body.allowed_modes
    if body.price_monthly  is not None: updates["price_monthly"]  = body.price_monthly

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    def _update():
        sb = get_supabase_admin()
        existing = (
            sb.table("plans")
            .select("*")
            .eq("id", plan_id)
            .single()
            .execute()
            .data
        )
        if not existing:
            return None
        sb.table("plans").update(updates).eq("id", plan_id).execute()
        return existing

    existing = await run_in_threadpool(_update)
    if not existing:
        raise HTTPException(status_code=404, detail="Plan not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "plan_updated",
        payload     = {"plan_id": plan_id, "changes": updates, "previous": existing},
    )
    return {"updated": True, "plan_id": plan_id, "changes": updates}


@router.patch("/plans/{plan_id}/retire", summary="Retire a plan (no new signups)")
async def retire_plan(plan_id: str, request: Request):
    """
    Mark a plan as inactive (is_active=False).  Existing tenants on this plan
    are unaffected — only new signups are blocked from choosing it.
    """
    def _retire():
        sb = get_supabase_admin()
        existing = (
            sb.table("plans")
            .select("id, name, is_active")
            .eq("id", plan_id)
            .single()
            .execute()
            .data
        )
        if not existing:
            return None
        sb.table("plans").update({"is_active": False}).eq("id", plan_id).execute()
        return existing

    existing = await run_in_threadpool(_retire)
    if not existing:
        raise HTTPException(status_code=404, detail="Plan not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "plan_retired",
        payload     = {"plan_id": plan_id, "name": existing.get("name")},
    )
    return {"retired": True, "plan_id": plan_id, "name": existing.get("name")}


# ─────────────────────────────────────────────────────────────────────────────
# MEMBER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/tenants/{tenant_id}/members", summary="List all members of a tenant")
async def list_members(tenant_id: str):
    """
    Returns all tenant_members rows for the given tenant, joined with
    Supabase auth user data (email, last_sign_in_at).
    """
    def _fetch():
        sb = get_supabase_admin()
        # Fetch member rows
        members = (
            sb.table("tenant_members")
            .select("*")
            .eq("tenant_id", tenant_id)
            .execute()
            .data or []
        )
        # Enrich with auth user data for each member
        enriched = []
        for m in members:
            user_id = m.get("user_id")
            user_data: dict = {}
            try:
                user_resp = sb.auth.admin.get_user_by_id(user_id)
                if user_resp and user_resp.user:
                    u = user_resp.user
                    user_data = {
                        "email"          : u.email,
                        "last_sign_in_at": u.last_sign_in_at,
                        "created_at"     : u.created_at,
                    }
            except Exception as exc:
                logger.warning(
                    "[SUPER_ADMIN] Could not fetch auth user %s: %s", user_id, exc
                )
            enriched.append({**m, **user_data})
        return enriched

    return await run_in_threadpool(_fetch)


@router.delete(
    "/tenants/{tenant_id}/members/{user_id}",
    summary="Remove a member from a tenant",
)
async def remove_member(tenant_id: str, user_id: str, request: Request):
    """
    Remove a user from tenant_members and decrement tenant_usage.user_count.

    Does NOT delete the Supabase Auth user — the user may belong to another
    tenant or may rejoin later with a new join code.
    """
    def _remove():
        sb = get_supabase_admin()
        existing = (
            sb.table("tenant_members")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("user_id", user_id)
            .single()
            .execute()
            .data
        )
        if not existing:
            return None

        sb.table("tenant_members").delete().eq("tenant_id", tenant_id).eq("user_id", user_id).execute()

        # Invalidate the user's active sessions immediately so their existing
        # JWT stops working right now, not after it naturally expires (~1 hour).
        # This also clears tenant_id and role from their app_metadata so the
        # next login gets a clean token with no stale claims.
        try:
            sb.auth.admin.update_user_by_id(
                user_id,
                {"app_metadata": {"tenant_id": None, "role": None}},
            )
            sb.auth.admin.sign_out(user_id, scope="global")
        except Exception as exc:
            logger.warning(
                "[SUPER_ADMIN] Could not invalidate session for user=%s: %s",
                user_id, exc,
            )

        # Decrement user count (floor at 0)
        current_usage = (
            sb.table("tenant_usage")
            .select("user_count")
            .eq("tenant_id", tenant_id)
            .single()
            .execute()
            .data
        )
        if current_usage:
            new_count = max(0, current_usage["user_count"] - 1)
            sb.table("tenant_usage").update({"user_count": new_count}).eq("tenant_id", tenant_id).execute()

        return existing

    existing = await run_in_threadpool(_remove)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail="Member not found in this tenant.",
        )

    await log_audit(
        actor_email = _actor(request),
        action      = "member_removed",
        tenant_id   = tenant_id,
        payload     = {"user_id": user_id, "previous_role": existing.get("role")},
    )
    return {"removed": True, "user_id": user_id}


@router.patch(
    "/tenants/{tenant_id}/members/{user_id}/promote",
    summary="Change a member's role",
)
async def promote_member(
    tenant_id: str,
    user_id  : str,
    body     : PromoteMemberBody,
    request  : Request,
):
    """
    Change a member's role within a tenant.

    Valid roles: 'user', 'admin', 'super_admin'.
    The Supabase custom-claims hook fires on the user's next sign-in, so their
    JWT will carry the new role after they log in again.
    """
    valid_roles = {"user", "admin"}
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{body.role}'. Must be one of {sorted(valid_roles)}.",
        )

    def _promote():
        sb = get_supabase_admin()
        existing = (
            sb.table("tenant_members")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("user_id", user_id)
            .single()
            .execute()
            .data
        )
        if not existing:
            return None
        sb.table("tenant_members").update({"role": body.role}).eq("tenant_id", tenant_id).eq("user_id", user_id).execute()

        # Push the new role into app_metadata immediately so the user's next
        # JWT (on any new request/refresh) carries the updated role without
        # requiring a full logout → login cycle.
        try:
            sb.auth.admin.update_user_by_id(
                user_id,
                {"app_metadata": {"role": body.role}},
            )
        except Exception as exc:
            logger.warning(
                "[SUPER_ADMIN] Could not update app_metadata for user=%s: %s",
                user_id, exc,
            )
        return existing

    existing = await run_in_threadpool(_promote)
    if not existing:
        raise HTTPException(status_code=404, detail="Member not found in this tenant.")

    await log_audit(
        actor_email = _actor(request),
        action      = "member_role_changed",
        tenant_id   = tenant_id,
        payload     = {
            "user_id"      : user_id,
            "old_role"     : existing.get("role"),
            "new_role"     : body.role,
        },
    )
    return {"updated": True, "user_id": user_id, "new_role": body.role}


# ─────────────────────────────────────────────────────────────────────────────
# BULK OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/bulk/plan-change", summary="Change plan for multiple tenants at once")
async def bulk_plan_change(body: BulkPlanChangeBody, request: Request):
    """
    Move a list of tenants to a new plan in one operation.

    Returns a per-tenant result map: { tenant_id: "ok" | "not_found" }.
    The plan is applied even if some tenant_ids are invalid — partial success
    is returned with per-item status.
    """
    def _apply():
        sb = get_supabase_admin()
        # Verify plan exists
        plan = (
            sb.table("plans")
            .select("id, name")
            .eq("id", body.plan_id)
            .single()
            .execute()
            .data
        )
        if not plan:
            return None, {}

        results = {}
        for tid in body.tenant_ids:
            try:
                updated = (
                    sb.table("tenants")
                    .update({"plan_id": body.plan_id})
                    .eq("id", tid)
                    .execute()
                )
                results[tid] = "ok" if updated.data else "not_found"
            except Exception as exc:
                results[tid] = f"error: {exc}"
        return plan, results

    plan, results = await run_in_threadpool(_apply)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan '{body.plan_id}' not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "bulk_plan_change",
        payload     = {
            "plan_id"   : body.plan_id,
            "plan_name" : plan.get("name"),
            "tenant_ids": body.tenant_ids,
            "results"   : results,
        },
    )
    return {"plan_id": body.plan_id, "results": results}


@router.post("/bulk/trial-extend", summary="Extend trial period for multiple tenants")
async def bulk_trial_extend(body: BulkTrialExtendBody, request: Request):
    """
    Push trial_ends_at forward by `days` days for each tenant in the list.

    If a tenant's trial has already expired, the new end date is calculated
    from now() + days.  If it hasn't expired yet, days are added to the
    current trial_ends_at.
    """
    def _extend():
        sb = get_supabase_admin()
        results = {}
        for tid in body.tenant_ids:
            try:
                tenant = (
                    sb.table("tenants")
                    .select("id, trial_ends_at")
                    .eq("id", tid)
                    .single()
                    .execute()
                    .data
                )
                if not tenant:
                    results[tid] = "not_found"
                    continue

                # Parse current trial_ends_at or use now() as base
                current_end_str = tenant.get("trial_ends_at")
                try:
                    current_end = datetime.fromisoformat(
                        current_end_str.replace("Z", "+00:00")
                    ) if current_end_str else datetime.now(timezone.utc)
                except (ValueError, AttributeError):
                    current_end = datetime.now(timezone.utc)

                base   = max(current_end, datetime.now(timezone.utc))
                new_end = (base + timedelta(days=body.days)).isoformat()

                sb.table("tenants").update({
                    "trial_ends_at": new_end,
                    "status"       : "trial",
                }).eq("id", tid).execute()

                results[tid] = new_end
            except Exception as exc:
                results[tid] = f"error: {exc}"
        return results

    results = await run_in_threadpool(_extend)

    await log_audit(
        actor_email = _actor(request),
        action      = "bulk_trial_extend",
        payload     = {
            "days"      : body.days,
            "tenant_ids": body.tenant_ids,
            "results"   : results,
        },
    )
    return {"extended_by_days": body.days, "results": results}


@router.post("/bulk/suspend", summary="Suspend multiple tenants at once")
async def bulk_suspend(body: BulkSuspendBody, request: Request):
    """
    Set status='suspended' for each tenant in the list.

    Suspended tenants receive a 402 on any chat/ingest request until
    reactivated.  Use PATCH /super-admin/tenants/{id} with status='active'
    to reactivate individual tenants.
    """
    def _suspend():
        sb = get_supabase_admin()
        results = {}
        for tid in body.tenant_ids:
            try:
                updated = (
                    sb.table("tenants")
                    .update({"status": "suspended"})
                    .eq("id", tid)
                    .execute()
                )
                results[tid] = "suspended" if updated.data else "not_found"
            except Exception as exc:
                results[tid] = f"error: {exc}"
        return results

    results = await run_in_threadpool(_suspend)

    await log_audit(
        actor_email = _actor(request),
        action      = "bulk_suspend",
        payload     = {"tenant_ids": body.tenant_ids, "results": results},
    )
    return {"results": results}


@router.post("/bulk/config-push", summary="Push config patch to all tenants on a plan")
async def bulk_config_push(body: BulkConfigPushBody, request: Request):
    """
    For every tenant on the given plan, merge `config_patch` into their
    config_overrides — BUT only for keys the tenant has NOT already customised.

    This allows safe propagation of new plan-level defaults without clobbering
    per-tenant customisations.  For example, if you add a new setting
    'reranker_top_k: 10' to the Growth plan, you can push it without
    overwriting tenants who have already set their own value.
    """
    def _push():
        sb = get_supabase_admin()

        # Verify plan exists
        plan = (
            sb.table("plans")
            .select("id, name")
            .eq("id", body.plan_id)
            .single()
            .execute()
            .data
        )
        if not plan:
            return None, {}

        # Fetch all tenants on this plan
        tenants = (
            sb.table("tenants")
            .select("id, slug, config_overrides")
            .eq("plan_id", body.plan_id)
            .execute()
            .data or []
        )

        results = {}
        for t in tenants:
            tid             = t["id"]
            current_config  = t.get("config_overrides") or {}
            merged          = {**body.config_patch, **current_config}  # tenant wins on key clash
            try:
                sb.table("tenants").update({"config_overrides": merged}).eq("id", tid).execute()
                results[tid] = "updated"
            except Exception as exc:
                results[tid] = f"error: {exc}"

        return plan, results

    plan, results = await run_in_threadpool(_push)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan '{body.plan_id}' not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "bulk_config_push",
        payload     = {
            "plan_id"     : body.plan_id,
            "plan_name"   : plan.get("name"),
            "config_patch": body.config_patch,
            "affected"    : len(results),
            "results"     : results,
        },
    )
    return {
        "plan_id"     : body.plan_id,
        "config_patch": body.config_patch,
        "results"     : results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVITY FEED & ALERTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/activity", summary="Paginated global audit_log feed")
async def get_activity(
    page     : int = Query(1,  ge=1),
    page_size: int = Query(25, ge=1, le=100),
    tenant_id: str = Query(""),   # filter to one tenant
    action   : str = Query(""),   # filter by action string
):
    """
    Returns a paginated, reverse-chronological feed of all audit_log entries.

    Optional filters:
      - tenant_id: show only entries for one tenant (pass UUID).
      - action:    exact match on the action field (e.g. "tenant_suspended").
    """
    def _fetch():
        sb = get_supabase_admin()
        q  = sb.table("audit_log").select("*")

        if tenant_id:
            q = q.eq("tenant_id", tenant_id)
        if action:
            q = q.eq("action", action)

        result = q.order("created_at", desc=True).limit(page_size * page + 100).execute()
        return result.data or []

    entries = await run_in_threadpool(_fetch)
    return _paginate(entries, page, page_size)


@router.get("/alerts", summary="All unread alerts across all tenants")
async def get_alerts(
    page     : int = Query(1,  ge=1),
    page_size: int = Query(25, ge=1, le=100),
    unread_only: bool = Query(True),
):
    """
    Returns system alerts (quota drift, trial expiry, reconciliation issues)
    across all tenants, most recent first.
    """
    def _fetch():
        sb = get_supabase_admin()
        q  = sb.table("alerts").select("*")
        if unread_only:
            q = q.eq("is_read", False)
        return q.order("created_at", desc=True).execute().data or []

    alerts = await run_in_threadpool(_fetch)
    return _paginate(alerts, page, page_size)


@router.patch("/alerts/{alert_id}/read", summary="Mark an alert as read")
async def mark_alert_read(alert_id: str, request: Request):
    """
    Mark a single alert as read so it stops appearing in the unread feed.
    """
    def _mark():
        sb = get_supabase_admin()
        existing = (
            sb.table("alerts")
            .select("id")
            .eq("id", alert_id)
            .single()
            .execute()
            .data
        )
        if not existing:
            return False
        sb.table("alerts").update({"is_read": True}).eq("id", alert_id).execute()
        return True

    found = await run_in_threadpool(_mark)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found.")

    await log_audit(
        actor_email = _actor(request),
        action      = "alert_read",
        payload     = {"alert_id": alert_id},
    )
    return {"marked_read": True, "alert_id": alert_id}


__all__ = ["router"]