# tasks/nightly_reconciliation.py
#
# Phase 3 — Plan & Usage Enforcement
#
# BACKGROUND:
#   The tenant_usage.vector_count is incremented/decremented via Supabase RPC
#   calls in plan_service.py after every ingest and delete. These operations are
#   atomic but not infallible — a process crash mid-ingest, a failed RPC call,
#   or an admin directly modifying Qdrant can cause drift between the stored
#   count and the real count in Qdrant.
#
#   This task runs nightly (scheduled via APScheduler in main.py) to detect and
#   correct any such drift. If the stored count differs from the real Qdrant
#   count by more than 5%, it:
#     1. Corrects tenant_usage.vector_count to the real value.
#     2. Re-evaluates over_quota status.
#     3. Inserts an alert row so the super admin can see what happened.
#
# SCHEDULING (add to main.py lifespan):
#
#   from apscheduler.schedulers.asyncio import AsyncIOScheduler
#   from tasks.nightly_reconciliation import reconcile_all_tenants
#
#   scheduler = AsyncIOScheduler()
#   scheduler.add_job(
#       reconcile_all_tenants,
#       trigger  = "cron",
#       hour     = 0,     # midnight
#       minute   = 0,
#       id       = "nightly_reconciliation",
#       replace_existing = True,
#   )
#   scheduler.start()
#
# REQUIREMENTS:
#   pip install apscheduler
#   (Add apscheduler to requirements.txt)
#
# MANUAL TRIGGER (for testing or on-demand reconciliation):
#   import asyncio
#   from tasks.nightly_reconciliation import reconcile_all_tenants
#   asyncio.run(reconcile_all_tenants())
#
# SUPER ADMIN ON-DEMAND:
#   POST /super-admin/tenants/{tenant_id}/reconcile calls reconcile_single_tenant()

from __future__ import annotations

import asyncio
from functools import partial

from utils.logger import get_logger

logger = get_logger(__name__)

# Drift threshold: if stored count differs from real count by more than this
# percentage, correct and alert.
_DRIFT_THRESHOLD_PCT = 5.0


async def reconcile_all_tenants() -> dict:
    """
    Nightly job: reconcile Qdrant real vector counts against tenant_usage.

    For each tenant:
      1. Get real vector count from Qdrant (vector_store.count()).
      2. Compare to tenant_usage.vector_count.
      3. If drift > 5%, correct the stored value and write an alert.
      4. Re-evaluate over_quota status.

    Runs in a threadpool because the Supabase Python SDK and Qdrant client
    are synchronous.

    Returns:
        Summary dict {
            "tenants_checked" : int,
            "tenants_corrected": int,
            "tenants_failed"  : int,
            "corrections"     : list[{slug, real, stored, drift_pct}]
        }
    """
    logger.info("[RECONCILE] Starting nightly vector count reconciliation...")

    try:
        from services.supabase_client import get_supabase_admin
        from services.plan_service import PlanService
        supabase = get_supabase_admin()
    except Exception as exc:
        logger.error("[RECONCILE] Cannot start — Supabase not configured: %s", exc)
        return {"error": str(exc)}

    loop = asyncio.get_event_loop()

    # Fetch all tenants (id + slug)
    try:
        tenants_result = await loop.run_in_executor(
            None,
            lambda: supabase.table("tenants").select("id, slug, status").execute(),
        )
        tenants = tenants_result.data or []
    except Exception as exc:
        logger.error("[RECONCILE] Failed to fetch tenants list: %s", exc)
        return {"error": str(exc)}

    logger.info("[RECONCILE] Checking %d tenants...", len(tenants))

    checked   = 0
    corrected = 0
    failed    = 0
    corrections: list[dict] = []

    for tenant in tenants:
        tenant_id   = tenant["id"]
        tenant_slug = tenant["slug"]
        try:
            result = await loop.run_in_executor(
                None,
                partial(_reconcile_single_tenant_sync, supabase, tenant_id, tenant_slug),
            )
            checked += 1
            if result.get("corrected"):
                corrected += 1
                corrections.append(result)
        except Exception as exc:
            failed += 1
            logger.error(
                "[RECONCILE] Failed for tenant %s: %s",
                tenant_slug, exc,
            )

    logger.info(
        "[RECONCILE] ✅ Complete — checked=%d  corrected=%d  failed=%d",
        checked, corrected, failed,
    )
    return {
        "tenants_checked"  : checked,
        "tenants_corrected": corrected,
        "tenants_failed"   : failed,
        "corrections"      : corrections,
    }


async def reconcile_single_tenant(tenant_id: str, tenant_slug: str) -> dict:
    """
    On-demand reconciliation for a single tenant.

    Called by:
      - POST /super-admin/tenants/{tenant_id}/reconcile
      - Tests

    Returns:
        {
            "tenant_slug" : str,
            "real_count"  : int,
            "stored_count": int,
            "drift_pct"   : float,
            "corrected"   : bool,
        }
    """
    from services.supabase_client import get_supabase_admin
    supabase = get_supabase_admin()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        partial(_reconcile_single_tenant_sync, supabase, tenant_id, tenant_slug),
    )


def _reconcile_single_tenant_sync(
    supabase,
    tenant_id  : str,
    tenant_slug: str,
) -> dict:
    """
    Synchronous core of reconcile_single_tenant().

    Runs in a threadpool executor to avoid blocking the event loop.

    Steps:
    1. Get real vector count from Qdrant for this tenant's collection.
    2. Get stored count from tenant_usage.
    3. If drift > threshold: update stored count + write alert.
    4. Re-evaluate over_quota status via PlanService.

    Returns:
        {
            "tenant_slug" : str,
            "real_count"  : int,
            "stored_count": int,
            "drift_pct"   : float,
            "corrected"   : bool,
        }
    """
    from vectorstore.factory import get_vector_store
    from services.plan_service import PlanService

    plan_svc = PlanService(supabase)

    # ── Step 1: Real count from Qdrant ───────────────────
    try:
        vs        = get_vector_store(tenant_slug=tenant_slug)
        real_count = vs.count()
    except Exception as exc:
        logger.warning(
            "[RECONCILE] Cannot get Qdrant count for %s: %s — skipping",
            tenant_slug, exc,
        )
        raise

    # ── Step 2: Stored count from tenant_usage ───────────
    try:
        usage_result  = (
            supabase
            .table("tenant_usage")
            .select("vector_count")
            .eq("tenant_id", tenant_id)
            .single()
            .execute()
        )
        stored_count = usage_result.data.get("vector_count", 0) if usage_result.data else 0
    except Exception as exc:
        logger.warning(
            "[RECONCILE] Cannot get stored count for %s: %s — skipping",
            tenant_slug, exc,
        )
        raise

    # ── Step 3: Drift check ───────────────────────────────
    denominator = max(real_count, 1)   # avoid division by zero
    drift_pct   = abs(real_count - stored_count) / denominator * 100

    result = {
        "tenant_slug" : tenant_slug,
        "real_count"  : real_count,
        "stored_count": stored_count,
        "drift_pct"   : round(drift_pct, 2),
        "corrected"   : False,
    }

    if drift_pct <= _DRIFT_THRESHOLD_PCT:
        logger.debug(
            "[RECONCILE] %s OK — real=%d  stored=%d  drift=%.1f%%",
            tenant_slug, real_count, stored_count, drift_pct,
        )
        return result

    # ── Drift detected — correct it ───────────────────────
    logger.warning(
        "[RECONCILE] Drift detected for %s: real=%d  stored=%d  drift=%.1f%% — correcting",
        tenant_slug, real_count, stored_count, drift_pct,
    )

    try:
        # Update stored count to match Qdrant reality
        supabase.table("tenant_usage").update({
            "vector_count": real_count,
            "updated_at"  : "now()",
        }).eq("tenant_id", tenant_id).execute()

        # Write an alert so super admins can see what was corrected
        supabase.table("alerts").insert({
            "tenant_id": tenant_id,
            "type"     : "reconciliation_drift",
            "message"  : (
                f"Vector count drifted {drift_pct:.1f}% for '{tenant_slug}'. "
                f"Corrected: stored={stored_count} → real={real_count}."
            ),
            "is_read"  : False,
        }).execute()

        result["corrected"] = True
        logger.info(
            "[RECONCILE] ✅ Corrected %s — %d → %d",
            tenant_slug, stored_count, real_count,
        )

    except Exception as exc:
        logger.error(
            "[RECONCILE] Failed to correct drift for %s: %s",
            tenant_slug, exc,
        )
        # Don't re-raise — partial failure is acceptable; retry next night.
        return result

    # ── Step 4: Re-evaluate over_quota status ────────────
    try:
        plan_svc._check_and_update_quota_status(tenant_id)
    except Exception as exc:
        logger.warning(
            "[RECONCILE] Failed to update quota status for %s: %s",
            tenant_slug, exc,
        )

    return result


__all__ = [
    "reconcile_all_tenants",
    "reconcile_single_tenant",
    "_reconcile_single_tenant_sync",
]