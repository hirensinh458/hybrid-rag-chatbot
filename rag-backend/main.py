# rag-backend/main.py
#
# Phase 3 — Plan & Usage Enforcement
#
# CHANGES vs Phase 1 version:
#
#   1. Quota warning response middleware (NEW):
#      After every response, checks request.state.quota_warning.
#      If True, appends "X-Quota-Warning: over_quota" header to the response.
#      The mobile app reads this header and shows a dismissible banner:
#        "Your organization is over its plan limit.
#         New documents cannot be added. Contact your admin to upgrade."
#
#   2. Nightly reconciliation APScheduler job (NEW):
#      Schedules reconcile_all_tenants() to run at midnight every day.
#      Uses APScheduler's AsyncIOScheduler (pip install apscheduler).
#      The scheduler is started after startup() and shut down on shutdown.
#
#   3. tasks/ package init (NEW):
#      Creates tasks/__init__.py so the tasks module is importable.
#
# ALL OTHER CODE IS UNCHANGED.
#
# REQUIREMENTS ADDITION:
#   apscheduler>=3.10.0
#   (Add to requirements.txt)

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import asyncio
import time
import uuid as _uuid
from contextlib import asynccontextmanager
from pathlib    import Path

from fastapi               import FastAPI, Request
from fastapi.middleware.cors    import CORSMiddleware
from fastapi.responses     import Response
from fastapi.staticfiles        import StaticFiles

from services.sync_service import SyncService
from services.rag_service  import startup
from services              import rag_service
from routers               import chat, ingest, kb
from routers               import sync  as sync_router
from routers               import admin as admin_router

# ── Logging bootstrap ─────────────────────────────────────────────────────────
from utils.logger import configure_logging, get_logger, set_request_id, clear_request_id
logger = get_logger(__name__)

# ── P5: Periodic Cloud→Local sync interval ────────────────────────────────────
BACKEND_SYNC_INTERVAL_S = 20 * 60  # 20 minutes


async def _periodic_cloud_sync():
    """
    Background asyncio task: syncs Cloud Qdrant → Local Qdrant every 20 minutes.

    Runs only when:
      - A cloud vector store is configured (rag_service.get_cloud_store() is not None)
      - The server has internet access (rag_service.is_online() returns True)

    Skipped silently otherwise (e.g. at-sea with local-only Qdrant).
    The 30s initial delay lets the lifespan startup() finish before the first sync.
    """
    logger.info(
        "[PERIODIC SYNC] Task started — waiting 30 s for startup to complete "
        "before first sync attempt"
    )
    await asyncio.sleep(30)  # wait for startup to settle

    while True:
        try:
            cloud_store = (
                rag_service.get_cloud_store()
                if hasattr(rag_service, "get_cloud_store")
                else None
            )
            is_online = (
                rag_service.is_online()
                if hasattr(rag_service, "is_online")
                else False
            )

            if cloud_store is not None and is_online:
                logger.info(
                    "[PERIODIC SYNC] Conditions met (cloud_store=configured, "
                    "is_online=True) — starting Cloud→Local vector sync"
                )
                loop = asyncio.get_event_loop()
                sync = SyncService()
                await loop.run_in_executor(None, sync.run)
                logger.info("[PERIODIC SYNC] ✅ Sync complete")
            else:
                skip_reason = (
                    "cloud store not configured"
                    if cloud_store is None
                    else "server offline (no internet)"
                )
                logger.info(
                    "[PERIODIC SYNC] Skipped — %s (next attempt in %d min)",
                    skip_reason,
                    BACKEND_SYNC_INTERVAL_S // 60,
                )

        except Exception as e:
            logger.error(
                "[PERIODIC SYNC] ❌ Error (will retry in %d min): %s",
                BACKEND_SYNC_INTERVAL_S // 60,
                e,
                exc_info=True,
            )

        logger.debug(
            "[PERIODIC SYNC] Sleeping %d s until next sync cycle",
            BACKEND_SYNC_INTERVAL_S,
        )
        await asyncio.sleep(BACKEND_SYNC_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Step 1: Configure logging FIRST so all subsequent startup logs are captured
    configure_logging()
    logger.info("=" * 60)
    logger.info("[STARTUP] RAG Chatbot API starting up...")
    logger.info("=" * 60)

    # ── Step 2: Run RAG service startup (loads embedder, vector stores, chain…)
    logger.info("[STARTUP] Initialising RAG service singletons...")
    await startup()
    logger.info("[STARTUP] ✅ RAG service startup complete")

    # ── Step 3: Launch periodic Cloud→Local background sync loop
    sync_task = asyncio.create_task(_periodic_cloud_sync())
    logger.info(
        "[STARTUP] ✅ Cloud sync background task scheduled "
        "(interval=%d min)",
        BACKEND_SYNC_INTERVAL_S // 60,
    )

    # ── Step 4 (PHASE 3): Schedule nightly reconciliation ─────────────────────
    # Creates the tasks/ package directory/init if it doesn't exist,
    # then schedules reconcile_all_tenants() at midnight via APScheduler.
    #
    # APScheduler is optional — if not installed, reconciliation is skipped with
    # a warning. Install it with: pip install apscheduler
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from tasks.nightly_reconciliation import reconcile_all_tenants

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            reconcile_all_tenants,
            trigger          = "cron",
            hour             = 0,
            minute           = 0,
            id               = "nightly_reconciliation",
            replace_existing = True,
        )
        scheduler.start()
        logger.info(
            "[STARTUP] ✅ Nightly reconciliation scheduler started "
            "(runs daily at 00:00)"
        )
    except ImportError:
        logger.warning(
            "[STARTUP] ⚠  APScheduler not installed — nightly reconciliation disabled. "
            "Run: pip install apscheduler"
        )
    except Exception as exc:
        logger.error(
            "[STARTUP] ❌ Failed to start reconciliation scheduler: %s", exc
        )

    # ── Hand control back to FastAPI — server is now serving requests ──────────
    yield

    # ── Clean shutdown ─────────────────────────────────────────────────────────
    logger.info("[SHUTDOWN] Cancelling periodic sync task...")
    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass

    if scheduler is not None:
        logger.info("[SHUTDOWN] Shutting down reconciliation scheduler...")
        scheduler.shutdown(wait=False)

    logger.info("[SHUTDOWN] ✅ RAG Chatbot API shut down cleanly")


app = FastAPI(
    title   = "RAG Chatbot API",
    version = "3.2.0",   # bumped for Phase 3
    lifespan= lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = False,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── Request logging middleware ────────────────────────────────────────────────
_mw_logger = get_logger("middleware.request")

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    rid = _uuid.uuid4().hex[:12]
    set_request_id(rid)

    t0 = time.perf_counter()
    _mw_logger.info(
        "→ %s %s  client=%s",
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )

    try:
        response: Response = await call_next(request)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        _mw_logger.error(
            "← %s %s  ERROR after %.1f ms: %s",
            request.method,
            request.url.path,
            elapsed_ms,
            exc,
            exc_info=True,
        )
        raise
    finally:
        clear_request_id()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    _mw_logger.info(
        "← %s %s  status=%d  %.1f ms",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


# ── PHASE 3: Quota warning response middleware ─────────────────────────────────
# After every response, check if require_active_subscription() set
# request.state.quota_warning = True (happens when tenant status is "over_quota").
# If so, append X-Quota-Warning: over_quota to the response headers.
#
# The mobile app reads this header and shows a dismissible banner to the user:
#   "Your organization is over its plan limit.
#    New documents cannot be added. Contact your admin to upgrade."
#
# This is a non-blocking signal — the request was still served successfully.
# The header is only present on over_quota tenants; absent otherwise.

@app.middleware("http")
async def _quota_warning_header(request: Request, call_next):
    response: Response = await call_next(request)

    # Safely check for the flag (request.state may not have it if resolve_tenant
    # was not in the dependency chain for this route, e.g. /health)
    if getattr(request.state, "quota_warning", False):
        response.headers["X-Quota-Warning"] = "over_quota"
        _mw_logger.info(
            "[QUOTA] X-Quota-Warning header added for tenant=%s",
            getattr(request.state, "tenant_slug", "unknown"),
        )

    return response


# ── Static files ──────────────────────────────────────────────────────────────
images_dir = Path(__file__).parent / "data" / "images"
images_dir.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(images_dir)), name="images")

pdfs_dir = Path(__file__).parent / "data" / "pdfs"
pdfs_dir.mkdir(parents=True, exist_ok=True)
app.mount("/pdfs", StaticFiles(directory=str(pdfs_dir)), name="pdfs")

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(admin_router.router)
app.include_router(chat.router)
app.include_router(ingest.router)
app.include_router(kb.router)
app.include_router(sync_router.router)

logger.debug("[STARTUP] All routers registered — ready to accept connections")