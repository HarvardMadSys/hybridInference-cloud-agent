"""Module loggers for the host package.

The gateway's ``serving.utils.logging`` couples ``get_logger`` to its JSON
formatter and per-request context machinery — none of which exists on a host.
The moved modules only ever call ``get_logger(__name__)``, so this shim keeps
exactly that surface. Copied-not-imported per the migration rule that the
gateway is reached over HTTP only.

The source's ``get_logger`` also *initialised* logging as a side effect (root
handler, level from env); this shim does not, and
``tests/unit/test_logging_utils.py`` pins that no-side-effects contract.
Configuration happens exactly once instead, when a process entrypoint calls
:func:`configure_process_logging` — the runner's ``main()`` does. An embedding
process that configured logging first keeps its own setup: ``basicConfig`` is
a no-op once the root logger has handlers.

:class:`JsonLogFormatter` comes from :mod:`cloud_agent_shared.logging_utils`,
and the control plane renders with the same class. The runner image ships
that package but not the web service, so the shared package is the one place
both can import it from. Host modules pass structured fields the text format
discards — ``extra={"event": ..., "job_id": ...}`` — so a host that sets
``AGENT_LOG_FORMAT=json`` gets them, and one that does not is unchanged.
"""

from __future__ import annotations

import logging
import os

from cloud_agent_shared.logging_utils import JsonLogFormatter, adopt_uvicorn_loggers

#: Set the host process log level without touching code or flags.
ENV_LOG_LEVEL = "AGENT_LOG_LEVEL"

#: ``json`` renders one JSON object per record; anything else keeps the text
#: format. Set by the operator on the machine, like the level beside it.
ENV_LOG_FORMAT = "AGENT_LOG_FORMAT"

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger; handler configuration belongs to the entrypoint."""
    return logging.getLogger(name or __name__)


def configure_process_logging(level: str | None = None, log_format: str | None = None) -> None:
    """Configure root logging for a host process entrypoint.

    The one sanctioned place logging gets set up — module import must stay
    side-effect free. Defers to any configuration already in place: if the
    root logger has handlers (an embedding app, or pytest's capture), this
    changes nothing, which is what makes calling it from ``main()`` safe
    everywhere.

    Args:
        level: Level name to use; defaults to ``AGENT_LOG_LEVEL`` or INFO.
        log_format: ``json`` or ``text``; defaults to ``AGENT_LOG_FORMAT`` or
            text. An unrecognised value falls back to text and says so, because
            a misspelled log format is not worth refusing to start over.
    """
    resolved = (level or os.environ.get(ENV_LOG_LEVEL) or "INFO").upper()
    requested = (log_format or os.environ.get(ENV_LOG_FORMAT) or "text").strip().lower()
    if requested == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        logging.basicConfig(level=resolved, handlers=[handler])
        if handler in logging.getLogger().handlers:
            # Only when this call is the one that configured logging. An
            # embedding process that got there first keeps its own setup, and
            # reshaping uvicorn's loggers around a handler we did not install
            # is not ours to do.
            adopt_uvicorn_loggers()
        return
    logging.basicConfig(level=resolved, format=_FORMAT)
    if requested != "text":
        get_logger(__name__).warning(
            "unrecognised %s=%r; logging as text", ENV_LOG_FORMAT, requested
        )
