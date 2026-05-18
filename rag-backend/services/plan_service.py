# services/plan_service.py
#
# Phase 3 — Plan & Usage Enforcement
#
# Centralises all plan-limit checks and usage accounting.
# Called by routers/ingest.py before and after every ingestion.
#
# SUPABASE RPC FUNCTIONS REQUIRED (run in Supabase SQL editor before deploying):
#
#   -- Atomic increment (no race condition)
#   create or replace function increment_vector_count(p_tenant_id uuid, p_count int)
#   returns void as $$
#     update tenant_usage
#     set vector_count = vector_count + p_count,
#         updated_at   = now()
#     where tenant_id = p_tenant_id;
#   $$ language sql;
#
#   -- Atomic decrement (floors at 0)
#   create or replace function decrement_vector_count(p_tenant_id uuid, p_count int)
#   returns void as $$
#     update tenant_usage
#     set vector_count = greatest(0, vector_count - p_count),
#         updated_at   = now()
#     where tenant_id = p_tenant_id;
#   $$ language sql;
#
# Usage:
#   from services.plan_service import PlanService
#   from services.supabase_client import get_supabase_admin
#
#   plan_svc = PlanService(get_supabase_admin())
#   ok, err  = plan_svc.check_vector_capacity(tenant_id, estimated_chunks)
#   if not ok:
#       raise HTTPException(402, {"code": "over_quota", "message": err})

from __future__ import annotations

from utils.logger import get_logger

logger = get_logger(__name__)


class PlanService:
    """
    Enforces plan limits for a tenant.

    All methods are synchronous (Supabase Python SDK is synchronous).
    Call from a threadpool when used inside async FastAPI handlers:
        await run_in_threadpool(plan_svc.increment_vectors, tenant_id, count)

    Args:
        supabase_admin_client: a supabase.Client instance created with the
            service_role key so RLS is bypassed for server-side reads/writes.
    """

    def __init__(self, supabase_admin_client):
        self.sb = supabase_admin_client

    # ── READ HELPERS ──────────────────────────────────────────────────────────

    def get_usage(self, tenant_id: str) -> dict:
        """
        Return the tenant_usage row for a given tenant.

        Returns:
            dict with at minimum: vector_count (int), user_count (int).

        Raises:
            RuntimeError if no usage row exists for this tenant.
        """
        result = (
            self.sb
            .table("tenant_usage")
            .select("*")
            .eq("tenant_id", tenant_id)
            .single()
            .execute()
        )
        if not result.data:
            raise RuntimeError(
                f"No tenant_usage row found for tenant_id={tenant_id}. "
                "Was the tenant created correctly (missing tenant_usage insert)?"
            )
        return result.data

    def get_plan(self, tenant_id: str) -> dict:
        """
        Return the plan dict for a given tenant (joined via tenants → plans).

        Returns:
            dict with at minimum: max_vectors (int), max_users (int),
            max_batch_pdfs (int), allowed_modes (list[str]), name (str).

        Raises:
            RuntimeError if tenant or plan is not found.
        """
        result = (
            self.sb
            .table("tenants")
            .select("*, plans(*)")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = result.data
        if not tenant:
            raise RuntimeError(f"Tenant not found: {tenant_id}")
        plan = tenant.get("plans")
        if not plan:
            raise RuntimeError(f"Plan not found for tenant: {tenant_id}")
        return plan

    # ── PRE-FLIGHT CHECKS ─────────────────────────────────────────────────────

    def check_vector_capacity(
        self,
        tenant_id: str,
        incoming_chunk_estimate: int,
    ) -> tuple[bool, str]:
        """
        Pre-flight check: will this ingestion push the tenant over their
        vector limit?

        Args:
            tenant_id:              UUID string of the tenant.
            incoming_chunk_estimate: Estimated number of chunks the upload will
                produce. Calculated in the router as a rough heuristic
                (~2 chunks per KB of PDF).

        Returns:
            (True, "")               — OK to proceed.
            (False, "error message") — Quota exceeded; message is user-visible.
        """
        try:
            usage = self.get_usage(tenant_id)
            plan  = self.get_plan(tenant_id)
        except RuntimeError as exc:
            logger.error("[PLAN] check_vector_capacity failed: %s", exc)
            # Fail open so a misconfigured DB doesn't block every upload.
            # Log and allow — the nightly reconciliation will correct drift.
            return True, ""

        current   = usage.get("vector_count", 0)
        limit     = plan.get("max_vectors", 0)
        projected = current + incoming_chunk_estimate

        if projected > limit:
            remaining = max(0, limit - current)
            msg = (
                f"Vector limit reached. You have {remaining:,} vectors "
                f"remaining out of {limit:,}."
            )
            logger.warning(
                "[PLAN] Vector quota pre-flight FAIL — tenant=%s  "
                "current=%d  estimate=%d  limit=%d",
                tenant_id, current, incoming_chunk_estimate, limit,
            )
            return False, msg

        logger.debug(
            "[PLAN] Vector quota pre-flight OK — tenant=%s  "
            "current=%d  estimate=%d  limit=%d",
            tenant_id, current, incoming_chunk_estimate, limit,
        )
        return True, ""

    def check_user_capacity(self, tenant_id: str) -> tuple[bool, str]:
        """
        Check whether the tenant has room for one more user (used at signup).

        Returns:
            (True, "")               — seat available.
            (False, "error message") — seat limit reached.
        """
        try:
            usage = self.get_usage(tenant_id)
            plan  = self.get_plan(tenant_id)
        except RuntimeError as exc:
            logger.error("[PLAN] check_user_capacity failed: %s", exc)
            return True, ""

        current = usage.get("user_count", 0)
        limit   = plan.get("max_users", 0)

        if current >= limit:
            msg = (
                f"Seat limit reached ({limit} users). "
                "Upgrade your plan to add more members."
            )
            logger.warning(
                "[PLAN] Seat quota FAIL — tenant=%s  current=%d  limit=%d",
                tenant_id, current, limit,
            )
            return False, msg

        return True, ""

    def check_batch_size(
        self,
        tenant_id: str,
        file_count: int,
    ) -> tuple[bool, str]:
        """
        Check whether the uploaded batch fits within the plan's per-batch PDF
        limit.

        Args:
            tenant_id:  UUID string of the tenant.
            file_count: Number of files in the current upload.

        Returns:
            (True, "")               — batch size OK.
            (False, "error message") — too many files.
        """
        try:
            plan = self.get_plan(tenant_id)
        except RuntimeError as exc:
            logger.error("[PLAN] check_batch_size failed: %s", exc)
            return True, ""

        limit = plan.get("max_batch_pdfs", 999)

        if file_count > limit:
            msg = (
                f"Your plan allows {limit} file(s) per batch. "
                f"You uploaded {file_count}. Please split into smaller uploads."
            )
            logger.warning(
                "[PLAN] Batch size FAIL — tenant=%s  uploaded=%d  limit=%d",
                tenant_id, file_count, limit,
            )
            return False, msg

        return True, ""

    # ── USAGE ACCOUNTING ──────────────────────────────────────────────────────

    def increment_vectors(self, tenant_id: str, count: int) -> None:
        """
        Post-ingestion: atomically add `count` to the tenant's vector_count,
        then check whether the tenant has crossed into over_quota status.

        Uses a Supabase RPC function (increment_vector_count) to avoid
        read-modify-write races when multiple ingestions run concurrently.

        Args:
            tenant_id: UUID string of the tenant.
            count:     Actual number of new chunks/vectors added.
        """
        if count <= 0:
            return

        try:
            self.sb.rpc(
                "increment_vector_count",
                {"p_tenant_id": tenant_id, "p_count": count},
            ).execute()
            logger.info(
                "[PLAN] Vector count incremented — tenant=%s  +%d",
                tenant_id, count,
            )
        except Exception as exc:
            logger.error(
                "[PLAN] increment_vectors RPC failed — tenant=%s  count=%d: %s",
                tenant_id, count, exc,
            )
            # Don't raise — the ingest already succeeded; a usage drift is
            # correctable by the nightly reconciliation task.
            return

        # Sync quota status after every change
        self._check_and_update_quota_status(tenant_id)

    def decrement_vectors(self, tenant_id: str, count: int) -> None:
        """
        On document delete: atomically subtract `count` from vector_count
        (floored at 0), then re-evaluate over_quota status.

        Args:
            tenant_id: UUID string of the tenant.
            count:     Number of vectors being freed (from documents.chunk_count).
        """
        if count <= 0:
            return

        try:
            self.sb.rpc(
                "decrement_vector_count",
                {"p_tenant_id": tenant_id, "p_count": count},
            ).execute()
            logger.info(
                "[PLAN] Vector count decremented — tenant=%s  -%d",
                tenant_id, count,
            )
        except Exception as exc:
            logger.error(
                "[PLAN] decrement_vectors RPC failed — tenant=%s  count=%d: %s",
                tenant_id, count, exc,
            )
            return

        self._check_and_update_quota_status(tenant_id)

    def record_document(
        self,
        tenant_id: str,
        tenant_slug: str,
        filename: str,
        chunk_count: int,
        file_size: int = None,
        status: str = "success",
    ) -> None:
        """
        Insert a row into the `documents` table after a successful ingestion.

        This is the audit record that lets admins see what's been indexed and
        lets DELETE /admin/documents/{doc_id} look up chunk_count for decrement.

        Args:
            tenant_id:   UUID of the tenant.
            tenant_slug: Slug used to build the storage path.
            filename:    Original PDF filename.
            chunk_count: Actual number of chunks produced.
            file_size:   File size in bytes (optional).
            status:      "success" | "failed" | "partial".
        """
        row = {
            "tenant_id"   : tenant_id,
            "filename"    : filename,
            "storage_path": f"pdfs/{tenant_slug}/{filename}",
            "chunk_count" : chunk_count,
            "status"      : status,
        }
        if file_size is not None:
            row["file_size"] = file_size

        try:
            self.sb.table("documents").insert(row).execute()
            logger.info(
                "[PLAN] Document recorded — tenant=%s  file=%s  chunks=%d",
                tenant_id, filename, chunk_count,
            )
        except Exception as exc:
            logger.error(
                "[PLAN] Failed to record document — tenant=%s  file=%s: %s",
                tenant_id, filename, exc,
            )
            # Non-fatal — ingest succeeded; document metadata can be re-derived
            # from Qdrant during reconciliation.

    # ── QUOTA STATUS SYNC ─────────────────────────────────────────────────────

    def _check_and_update_quota_status(self, tenant_id: str) -> None:
        """
        Compare the current vector_count against the plan limit and update
        `tenants.status` to reflect the accurate quota state.

        Transition rules:
          - active   → over_quota  : when vector_count > max_vectors
          - over_quota → active    : when vector_count ≤ max_vectors (vectors freed)
          - trial / suspended      : never changed here (those are admin actions)

        Called after every increment or decrement so the status is always fresh.
        Errors are logged and swallowed — a stale status is corrected nightly.
        """
        try:
            usage  = self.get_usage(tenant_id)
            plan   = self.get_plan(tenant_id)
            status_result = (
                self.sb
                .table("tenants")
                .select("status")
                .eq("id", tenant_id)
                .single()
                .execute()
            )
            current_status = status_result.data.get("status", "active")

            vector_count = usage.get("vector_count", 0)
            max_vectors  = plan.get("max_vectors", 0)

            if vector_count > max_vectors and current_status not in ("suspended", "over_quota"):
                self.sb.table("tenants").update(
                    {"status": "over_quota"}
                ).eq("id", tenant_id).execute()
                logger.warning(
                    "[PLAN] Tenant %s transitioned to over_quota "
                    "(vectors=%d > limit=%d  was_status=%s)",
                    tenant_id, vector_count, max_vectors, current_status,
                )

            elif vector_count <= max_vectors and current_status == "over_quota":
                self.sb.table("tenants").update(
                    {"status": "active"}
                ).eq("id", tenant_id).execute()
                logger.info(
                    "[PLAN] Tenant %s restored to active "
                    "(vectors=%d ≤ limit=%d)",
                    tenant_id, vector_count, max_vectors,
                )

        except Exception as exc:
            logger.error(
                "[PLAN] _check_and_update_quota_status failed — tenant=%s: %s",
                tenant_id, exc,
            )


__all__ = ["PlanService"]