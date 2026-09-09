"""Module loggers for the control plane.

Mirrors the host package's shim and exists for the same reason: the gateway's
``get_logger`` couples module loggers to its JSON formatter and per-request
context, none of which this service has. Duplicated rather than shared because
the two packages are separate deployment units — the control plane runs as a
web service and the host runs on a machine in the pool, and a shared module
would be a dependency between them that the split exists to remove.
"""

from __future__ import annotations

import logging
import os

#: Set the control plane's log level without touching code or flags.
ENV_LOG_LEVEL = "AGENT_LOG_LEVEL"

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a module logger; handler configuration belongs to the entrypoint."""
    return logging.getLogger(name or __name__)


def configure_process_logging(level: str | None = None) -> None:
    """Configure root logging for the control plane's entrypoint.

    Defers to any configuration already in place, so an embedding process — or
    pytest's capture — keeps its own setup.

    Args:
        level: Level name to use; defaults to ``AGENT_LOG_LEVEL`` or INFO.
    """
    logging.basicConfig(
        level=(level or os.environ.get(ENV_LOG_LEVEL) or "INFO").upper(), format=_FORMAT
    )
