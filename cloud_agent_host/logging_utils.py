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
"""

from __future__ import annotations

import logging
import os

#: Set the host process log level without touching code or flags.
ENV_LOG_LEVEL = "AGENT_LOG_LEVEL"

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger; handler configuration belongs to the entrypoint."""
    return logging.getLogger(name or __name__)


def configure_process_logging(level: str | None = None) -> None:
    """Configure root logging for a host process entrypoint.

    The one sanctioned place logging gets set up — module import must stay
    side-effect free. Defers to any configuration already in place: if the
    root logger has handlers (an embedding app, or pytest's capture), this
    changes nothing, which is what makes calling it from ``main()`` safe
    everywhere.

    Args:
        level: Level name to use; defaults to ``AGENT_LOG_LEVEL`` or INFO.
    """
    resolved = (level or os.environ.get(ENV_LOG_LEVEL) or "INFO").upper()
    logging.basicConfig(level=resolved, format=_FORMAT)
