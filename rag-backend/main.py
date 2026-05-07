# rag-backend/main.py
#
# CHANGES FROM PREVIOUS VERSION:
#   P5 — Added _periodic_cloud_sync() background task (Cloud→Local Qdrant sync)
#        Runs every 20 minutes while server has internet. Skipped gracefully
#        when cloud store is not configured or server is offline.
#
# ORIGINAL CHANGES (kept):
#   - CORS allow_origins=["*"] for mobile LAN clients
#   - Admin router under /admin prefix
#   - Static files for /images and /pdfs
#
# LOGGING CHANGES:
#   - configure_logging() called FIRST in lifespan so every module that
#     imports after startup gets a properly configured logger.
#   - Every major lifecycle event (startup, sync trigger, shutdown) is logged
#     at INFO.  Errors are logged at ERROR with exc_info so the stack trace
#     appears in the log file.
#   - A FastAPI middleware logs every incoming request + response status code
#     and wall-clock latency so you can trace a question from HTTP entry to
#     response — including request-ID injection into the ContextVar used by
#     utils.logger so ALL log lines for one request share the same req-id.

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
# Import FIRST — before any other module that might create loggers.
from utils.logger import configure_logging, get_logger, set_request_id, clear_request_id

# configure_logging() is called inside lifespan (after startup) so the log
# directory (data/logs/) is guaranteed to exist by the time we try to open it.
# get_logger() calls before configure_logging() still work — they just write to
# the unconfigured root logger (no output), which is fine for module-level code.
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
    # Log that the background task has started its initial sleep
    logger.info(
        "[PERIODIC SYNC] Task started — waiting 30 s for startup to complete "
        "before first sync attempt"
    )
    await asyncio.sleep(30)  # wait for startup to settle

    while True:
        try:
            # Check whether cloud store is configured and we have internet
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
                # Both conditions met — run the sync
                logger.info(
                    "[PERIODIC SYNC] Conditions met (cloud_store=configured, "
                    "is_online=True) — starting Cloud→Local vector sync"
                )
                loop = asyncio.get_event_loop()
                sync = SyncService()
                await loop.run_in_executor(None, sync.run)
                logger.info("[PERIODIC SYNC] ✅ Sync complete")
            else:
                # Log why we skipped so operators can diagnose missing syncs
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
            # Never let a sync error crash the background loop
            logger.error(
                "[PERIODIC SYNC] ❌ Error (will retry in %d min): %s",
                BACKEND_SYNC_INTERVAL_S // 60,
                e,
                exc_info=True,   # include full traceback in log file
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
    task = asyncio.create_task(_periodic_cloud_sync())
    logger.info(
        "[STARTUP] ✅ Cloud sync background task scheduled "
        "(interval=%d min)",
        BACKEND_SYNC_INTERVAL_S // 60,
    )

    # ── Hand control back to FastAPI — server is now serving requests
    yield

    # ── Clean shutdown — cancel background sync task
    logger.info("[SHUTDOWN] Cancelling periodic sync task...")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass   # expected — task was cancelled cleanly
    logger.info("[SHUTDOWN] ✅ Periodic sync task stopped")
    logger.info("[SHUTDOWN] RAG Chatbot API shut down cleanly")


app = FastAPI(
    title   = "RAG Chatbot API",
    version = "3.1.0",
    lifespan= lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# allow_origins=["*"] — permits mobile clients on any LAN IP.
# allow_credentials must be False when using wildcard origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = False,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── Request logging middleware ────────────────────────────────────────────────
# Logs every incoming HTTP request with method, path, and a unique request-ID.
# Logs the response status code and wall-clock latency once the response is sent.
# Injects the request-ID into the logging ContextVar so ALL log lines emitted
# during this request (across all modules) carry the same req=<id> tag.

_mw_logger = get_logger("middleware.request")

@app.middleware("http")
async def _log_requests(request: Request, call_next):
    # Generate a short unique ID for this request so logs can be correlated
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
        # Always clear the request-ID when the request is done
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


# ── Static files ──────────────────────────────────────────────────────────────
images_dir = Path(__file__).parent / "data" / "images"
images_dir.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(images_dir)), name="images")

pdfs_dir = Path(__file__).parent / "data" / "pdfs"
pdfs_dir.mkdir(parents=True, exist_ok=True)
app.mount("/pdfs", StaticFiles(directory=str(pdfs_dir)), name="pdfs")

# ── Routers ───────────────────────────────────────────────────────────────────
# Admin router — all write operations under /admin/* (requires ADMIN_TOKEN).
app.include_router(admin_router.router)

# Existing routers — kept for backward compatibility.
app.include_router(chat.router)
app.include_router(ingest.router)
app.include_router(kb.router)
app.include_router(sync_router.router)

logger.debug("[STARTUP] All routers registered — ready to accept connections")