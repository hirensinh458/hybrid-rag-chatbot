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

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import asyncio
from contextlib import asynccontextmanager
from pathlib    import Path
from fastapi    import FastAPI
from fastapi.middleware.cors    import CORSMiddleware
from fastapi.staticfiles        import StaticFiles
from services.sync_service import SyncService
from services.rag_service  import startup
from services              import rag_service
from routers               import chat, ingest, kb
from routers               import sync  as sync_router
from routers               import admin as admin_router

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
    await asyncio.sleep(30)  # wait for startup to settle

    while True:
        try:
            cloud_store = rag_service.get_cloud_store() if hasattr(rag_service, 'get_cloud_store') else None
            is_online   = rag_service.is_online()       if hasattr(rag_service, 'is_online')       else False

            if cloud_store is not None and is_online:
                print('[PERIODIC SYNC] Running Cloud → Local vector sync')
                loop = asyncio.get_event_loop()
                sync = SyncService()
                await loop.run_in_executor(None, sync.run)
                print('[PERIODIC SYNC] Complete')
            else:
                print('[PERIODIC SYNC] Skipped — cloud store not configured or server offline')

        except Exception as e:
            # Never let a sync error crash the background loop
            print(f'[PERIODIC SYNC] Error (will retry in {BACKEND_SYNC_INTERVAL_S // 60} min): {e}')

        await asyncio.sleep(BACKEND_SYNC_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run existing startup (loads RAG service, BM25, Qdrant connection, etc.)
    await startup()

    # P5: Start the periodic Cloud→Local background sync loop
    task = asyncio.create_task(_periodic_cloud_sync())
    print(f'[STARTUP] Cloud sync task scheduled every {BACKEND_SYNC_INTERVAL_S // 60} min')

    yield

    # Clean shutdown — cancel the background task so it doesn't linger
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    print('[SHUTDOWN] Periodic sync task stopped')


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