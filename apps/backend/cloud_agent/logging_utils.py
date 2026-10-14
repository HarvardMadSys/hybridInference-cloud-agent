"""Module loggers for the control plane.

Mirrors the host package's shim and exists for the same reason: the gateway's
``get_logger`` couples module loggers to its JSON formatter and per-request
context, none of which this service has. The JSON formatter and the uvicorn
hand-over are the same in the host, so both come from
:mod:`cloud_agent_shared.logging_utils`. Both images ship that package, so it
adds no dependency between the two deployment units. Configuration stays here
because it is this service's own. The entrypoint installs a handler and keeps
it, and :func:`apply_logging_settings` changes that handler when an
administrator saves new settings.

Call sites throughout this package already pass structured fields —
``extra={"event": ..., "job_id": ..., "duration_ms": ...}`` — but the text
format renders ``%(message)s`` and nothing else, so every one of those fields
was discarded before it reached the log. The admin Settings page's JSON format
renders them instead. Environment values bootstrap logging and seed settings
on first startup. Text stays the default because it is what an operator reading
``docker logs`` by eye already has, and changing that for everyone is not this
switch's job.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from cloud_agent_shared.logging_utils import JsonLogFormatter, adopt_uvicorn_loggers

#: Set the control plane's log level without touching code or flags.
ENV_LOG_LEVEL = "AGENT_LOG_LEVEL"

#: ``json`` renders one JSON object per record; anything else keeps the text
#: format. The entrypoint reads this during bootstrap; the saved admin value
#: controls subsequent changes to the handler the control plane owns.
ENV_LOG_FORMAT = "AGENT_LOG_FORMAT"

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

_process_handler: logging.StreamHandler[Any] | None = None
_process_owns_uvicorn = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger; handler configuration belongs to the entrypoint."""
    return logging.getLogger(name or __name__)


def configure_process_logging(level: str | None = None, log_format: str | None = None) -> None:
    """Configure root logging for the control plane's entrypoint.

    Defers to any configuration already in place, so an embedding process — or
    pytest's capture — keeps its own setup.

    Args:
        level: Level name to use; defaults to ``AGENT_LOG_LEVEL`` or INFO.
        log_format: ``json`` or ``text``; defaults to ``AGENT_LOG_FORMAT`` or
            text. An unrecognised value falls back to text and says so, because
            a misspelled log format is not worth refusing to serve over.
    """
    global _process_handler, _process_owns_uvicorn
    resolved = (level or os.environ.get(ENV_LOG_LEVEL) or "INFO").upper()
    requested = (log_format or os.environ.get(ENV_LOG_FORMAT) or "text").strip().lower()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter() if requested == "json" else logging.Formatter(_FORMAT))
    logging.basicConfig(level=resolved, handlers=[handler])
    if handler in logging.getLogger().handlers:
        _process_handler = handler
        _process_owns_uvicorn = requested == "json"
        if requested == "json":
            # Only the handler this process installed may adopt uvicorn.
            adopt_uvicorn_loggers()
    if requested not in {"text", "json"}:
        get_logger(__name__).warning(
            "unrecognised %s=%r; logging as text", ENV_LOG_FORMAT, requested
        )


def apply_logging_settings(level: str, log_format: str) -> bool:
    """Update logging owned by this service, leaving embedding configuration alone.

    A handler installed before this service, or additional root handlers added
    later by an embedding process, keep their formatter and logging threshold.
    The bootstrap handler is retained across updates instead of accumulating
    handlers whenever settings synchronize.
    """
    global _process_owns_uvicorn
    root = logging.getLogger()
    handler = _process_handler
    if handler is None or root.handlers != [handler]:
        return False
    handler.setFormatter(JsonLogFormatter() if log_format == "json" else logging.Formatter(_FORMAT))
    handler.setLevel(level)
    root.setLevel(level)
    if log_format == "json" and not _process_owns_uvicorn:
        adopt_uvicorn_loggers()
        _process_owns_uvicorn = True
    return True
