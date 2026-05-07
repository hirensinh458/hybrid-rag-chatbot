# utils/logger.py
#
# Centralised structured logger for the RAG backend.
#
# DESIGN:
#   - Every module calls  get_logger(__name__)  to get its own named logger.
#   - One call to configure_logging() at startup sets format, level, and handlers.
#   - Writes to BOTH stderr (coloured) AND a rolling file  data/logs/rag.log.
#   - Request-ID context is stored in a ContextVar so every log line emitted
#     during a single HTTP request automatically carries the same request_id.
#   - Per-module log levels can be overridden via  LOG_LEVEL_<MODULE>=DEBUG  env vars.
#
# USAGE:
#   # in any module:
#   from utils.logger import get_logger
#   logger = get_logger(__name__)
#   logger.info("something happened")
#   logger.debug("detail: %s", value)
#   logger.error("failed: %s", exc, exc_info=True)
#
#   # in FastAPI lifespan or startup:
#   from utils.logger import configure_logging
#   configure_logging()          # call once at boot
#
#   # to attach a request-id to all logs within a request:
#   from utils.logger import set_request_id, clear_request_id
#   set_request_id("abc-123")    # call at start of request
#   clear_request_id()           # call at end of request (or use context manager)
#
# LOG LEVEL:
#   Set via environment variable  LOG_LEVEL  (default INFO).
#   e.g.  LOG_LEVEL=DEBUG  to see every retrieval score.
#
# FILE ROTATION:
#   data/logs/rag.log  — rotates at 10 MB, keeps last 5 backups.

import logging
import logging.handlers
import os
import sys
from contextvars import ContextVar
from pathlib import Path

# ── Request-ID context variable ───────────────────────────────────────────────
# Stores a per-request UUID so log lines from the same HTTP request are
# linkable. Empty string when outside a request context.
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def set_request_id(rid: str) -> None:
    """Set the request-ID for the current async context (call at request start)."""
    _request_id_var.set(rid)


def clear_request_id() -> None:
    """Clear the request-ID (call at request end, or let it expire naturally)."""
    _request_id_var.set("")


def get_request_id() -> str:
    """Return the current request-ID, or empty string if not in a request."""
    return _request_id_var.get()


# ── Custom formatter ──────────────────────────────────────────────────────────

class _RagFormatter(logging.Formatter):
    """
    Custom log formatter that:
      - Includes timestamp, level, logger name, request-ID, and message.
      - Adds ANSI colour codes when writing to a TTY (console output).
      - Does NOT add colour when writing to a file.

    Format:
        2025-01-01 12:00:00.123 | INFO     | rag_chain       | [req=abc123] Retrieve start ...
    """

    # ANSI colour map for log levels (console only)
    _LEVEL_COLOURS = {
        logging.DEBUG   : "\033[36m",    # cyan
        logging.INFO    : "\033[32m",    # green
        logging.WARNING : "\033[33m",    # yellow
        logging.ERROR   : "\033[31m",    # red
        logging.CRITICAL: "\033[35m",    # magenta
    }
    _RESET = "\033[0m"
    _GREY  = "\033[90m"

    def __init__(self, use_colour: bool = False):
        super().__init__()
        self.use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        ms = f"{record.msecs:03.0f}"

        # Level padded to 8 chars
        level = record.levelname.ljust(8)

        # Logger name — strip common prefix "rag_backend." for brevity,
        # right-pad/truncate to 16 chars for alignment.
        name = record.name
        if name.startswith("rag_backend."):
            name = name[len("rag_backend."):]
        name = name[:20].ljust(20)

        # Request-ID (may be empty — omit brackets when empty)
        rid = get_request_id()
        rid_tag = f"[req={rid[:8]}] " if rid else ""

        # Message (with exc_info if present)
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        if self.use_colour:
            colour = self._LEVEL_COLOURS.get(record.levelno, "")
            line = (
                f"{self._GREY}{ts}.{ms}{self._RESET} | "
                f"{colour}{level}{self._RESET} | "
                f"{self._GREY}{name}{self._RESET} | "
                f"{rid_tag}{msg}"
            )
        else:
            line = f"{ts}.{ms} | {level} | {name} | {rid_tag}{msg}"

        return line


# ── Public API ────────────────────────────────────────────────────────────────

_configured = False   # guard against double-initialisation


def configure_logging(
    log_level  : str  = None,
    log_to_file: bool = True,
    log_dir    : str  = None,
) -> None:
    """
    Configure the root logger once at application startup.

    Call this from main.py lifespan before any module creates a logger.

    Args:
        log_level   : "DEBUG" / "INFO" / "WARNING" / "ERROR".
                      Defaults to LOG_LEVEL env var, then "INFO".
        log_to_file : Write to data/logs/rag.log in addition to stderr.
        log_dir     : Override the log directory (default: data/logs/).
    """
    global _configured
    if _configured:
        return
    _configured = True

    # ── Resolve log level ─────────────────────────────────────────────────
    level_str  = (log_level or os.getenv("LOG_LEVEL", "INFO")).upper()
    level      = getattr(logging, level_str, logging.INFO)

    # ── Root logger ───────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any handlers added automatically by uvicorn / other libraries
    root.handlers.clear()

    # ── Console handler (stderr) ──────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    # Use colour if running on a real TTY (not in Docker logs piped to file)
    use_colour = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    console_handler.setFormatter(_RagFormatter(use_colour=use_colour))
    root.addHandler(console_handler)

    # ── Rotating file handler ─────────────────────────────────────────────
    if log_to_file:
        _log_dir = Path(log_dir) if log_dir else (
            Path(__file__).parent.parent / "data" / "logs"
        )
        _log_dir.mkdir(parents=True, exist_ok=True)
        log_path = _log_dir / "rag.log"

        file_handler = logging.handlers.RotatingFileHandler(
            filename    = str(log_path),
            maxBytes    = 10 * 1024 * 1024,   # 10 MB per file
            backupCount = 5,
            encoding    = "utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(_RagFormatter(use_colour=False))
        root.addHandler(file_handler)

    # ── Silence noisy third-party loggers ─────────────────────────────────
    # These produce enormous output at DEBUG that drowns out our logs.
    for noisy_lib in (
        "httpx", "httpcore", "urllib3", "asyncio",
        "sentence_transformers", "transformers",
        "uvicorn.access",  # access logs come through as INFO — keep at WARNING
    ):
        logging.getLogger(noisy_lib).setLevel(logging.WARNING)

    # Log the configuration so we can verify it on startup
    _startup_logger = get_logger("utils.logger")
    _startup_logger.info(
        "Logging configured — level=%s  file=%s  colour=%s",
        level_str,
        str(_log_dir / "rag.log") if log_to_file else "disabled",
        use_colour,
    )


def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger for the given module.

    Usage:
        logger = get_logger(__name__)

    The returned Logger inherits the root level and handlers set by
    configure_logging().  If configure_logging() has not been called yet
    (e.g. in a test), the root logger falls back to Python's default WARNING
    level with no handlers, so calls are silently discarded — no crash.
    """
    return logging.getLogger(name)


__all__ = [
    "configure_logging",
    "get_logger",
    "set_request_id",
    "clear_request_id",
    "get_request_id",
]