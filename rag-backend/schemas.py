# schemas.py
# All Pydantic request and response models for the RAG API.
#
# CHANGES vs previous version:
#   - OfflineChunk: added bbox, page_width, page_height, chunk_type fields.
#   - IngestResponse: added quota_hit (bool) and remaining_files (list[str]).
#
#   WHY (IngestResponse changes):
#     When a batch upload hits the vector quota mid-flight, the backend now
#     performs partial ingestion — indexing files until the cap is reached,
#     then stopping. The two new fields communicate this outcome to callers:
#
#       quota_hit      : True when one or more files were skipped because the
#                        tenant's vector limit was reached during this batch.
#       remaining_files: Names of files that were NOT indexed due to quota.
#                        Empty list when quota_hit is False.
#
#     The frontend (IngestPanel.jsx) uses these to display a warning banner
#     identifying exactly which files were skipped, rather than marking the
#     whole upload as failed.

from pydantic import BaseModel, Field


# ── Requests ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    question   : str = Field(..., min_length=1, max_length=2000)
    session_id : str = Field(default="default", max_length=64)


class ClearRequest(BaseModel):
    session_id : str = Field(default="default")


# ── Responses ─────────────────────────────────────────────

class Citation(BaseModel):
    source      : str
    page        : int | None = None
    heading     : str        = ""
    section_path: str        = ""
    chunk_type  : str        = "text"
    # ── NEW fields (Issue 1 — clickable citations) ──────
    bbox        : list[float] | None  = None   # [x0, y0, x1, y1] PDF points, null if unavailable
    page_width  : float | None        = None   # PDF page dimensions for coordinate normalisation
    page_height : float | None        = None
    source_url  : str                 = ""     # Supabase public URL or ""


class ChatResponse(BaseModel):
    answer     : str
    query_type : str
    citations  : list[Citation] = []
    usage      : dict           = {}
    session_id : str


class OfflineChunk(BaseModel):
    """
    A single retrieved chunk returned in offline mode (no LLM generation).

    Fields:
        source       : filename of the source PDF
        page         : 1-indexed page number within the PDF
        heading      : nearest heading above this chunk (may be empty)
        section_path : full breadcrumb e.g. "Ch4 > 4.2 Cooling System > Procedure"
        content      : raw text of this chunk as extracted from the PDF
        score        : RRF relevance score from hybrid retrieval (0.0–1.0 range)
        chunk_type   : "text" | "heading" | "bullet" | "table" | "image"
                       Used by frontend to show the right icon on the card.

        bbox         : [x0, y0, x1, y1] in PDF points (1pt = 1/72 inch).
                       Coordinate origin: top-left of the page.
                       None if position could not be determined (some images/tables).
                       Frontend uses this to draw a highlight rectangle over the
                       exact source text in the PDF viewer.

        page_width   : width of the PDF page in the same PDF points as bbox.
        page_height  : height of the PDF page in the same PDF points as bbox.
                       Required for coordinate normalization:
                         canvas_x = (bbox[0] / page_width)  * canvas_width
                         canvas_y = (bbox[1] / page_height) * canvas_height
                       None when bbox is None.
    """
    source      : str
    page        : int | None          = None
    heading     : str                 = ""
    section_path: str                 = ""
    content     : str                 = ""
    score       : float               = 0.0
    # ── NEW fields ────────────────────────────────────────
    chunk_type  : str                 = "text"
    bbox        : list[float] | None  = None
    page_width  : float | None        = None
    page_height : float | None        = None


class OfflineQueryResponse(BaseModel):
    """
    Returned instead of a streamed LLM answer when the device is offline.
    The frontend renders each chunk as a manual-excerpt card with a
    "Open in manual" button that deep-links into the PDF viewer.
    """
    query      : str
    chunks     : list[OfflineChunk] = []
    total      : int                = 0
    is_offline : bool               = True


class SyncStatusResponse(BaseModel):
    """Current state of the document sync service."""
    last_synced   : str | None = None   # ISO timestamp or None
    is_syncing    : bool       = False
    pending_count : int        = 0      # docs awaiting download
    message       : str        = ""


class IngestResponse(BaseModel):
    status          : str                # "ok" | "partial"
    files_indexed   : list[str]
    total_chunks    : int
    total_parents   : int
    message         : str
    # ── Phase 4 additions — partial quota ingestion ───────────────────────────
    quota_hit       : bool       = False  # True when vector cap was reached mid-batch
    remaining_files : list[str]  = []     # Files NOT indexed because quota was hit


class StatsResponse(BaseModel):
    total_vectors  : int
    bm25_docs      : int
    parent_count   : int
    indexed_files  : list[str]
    embedding_model: str
    llm_model      : str
    collection     : str


class HealthResponse(BaseModel):
    status          : str
    version         : str        = "3.0.0"
    groq_available  : bool                    # renamed from groq_configured
    is_online       : bool       = True
    timestamp       : str        = ""         # ISO UTC, filled by the endpoint


class DocumentsResponse(BaseModel):
    files       : list[str]
    total_files : int


class WipeResponse(BaseModel):
    status  : str
    message : str


class DeleteFileResponse(BaseModel):
    status         : str
    filename       : str
    vectors_deleted: int
    message        : str


class IngestStatusResponse(BaseModel):
    status  : str
    progress: int  = 0
    message : str  = ""
    result  : dict = {}


__all__ = [
    "ChatRequest", "ClearRequest",
    "Citation", "ChatResponse",
    "OfflineChunk", "OfflineQueryResponse",
    "SyncStatusResponse",
    "IngestResponse",
    "StatsResponse", "HealthResponse", "DocumentsResponse",
    "WipeResponse", "DeleteFileResponse", "IngestStatusResponse",
]