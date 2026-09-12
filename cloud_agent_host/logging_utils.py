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

:class:`JsonLogFormatter` is duplicated from the control plane's shim rather
than imported from it, under the same rule: the two packages are separate
deployment units, and the runner must not grow an import into the web service
to render its own logs. Host modules pass structured fields the text format
discards — ``extra={"event": ..., "job_id": ...}`` — so a host that sets
``AGENT_LOG_FORMAT=json`` gets them, and one that does not is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

#: Set the host process log level without touching code or flags.
ENV_LOG_LEVEL = "AGENT_LOG_LEVEL"

#: ``json`` renders one JSON object per record; anything else keeps the text
#: format. Set by the operator on the machine, like the level beside it.
ENV_LOG_FORMAT = "AGENT_LOG_FORMAT"

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

#: Attributes the stdlib puts on every record — the envelope, not a caller's
#: ``extra``. Read off a real record rather than hand-listed so an interpreter
#: that adds one (3.12 added ``taskName``) does not start reporting it as
#: caller data; ``message`` and ``asctime`` are set while formatting rather
#: than in ``__init__``, so they are named here.
_RECORD_ATTRS = frozenset(logging.LogRecord("", logging.NOTSET, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
}


class JsonLogFormatter(logging.Formatter):
    """Render a record as one JSON object, with ``extra`` fields at the top level.

    Top level rather than nested under a key, so a field can be selected
    directly — ``jq 'select(.event == "…") | .duration_ms'`` — which is the
    only query a process with no log pipeline has. The envelope wins a name
    collision: reshaping ``level`` or ``logger`` out from under whoever reads
    the stream is worse than dropping one caller field, and no field in use
    collides.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the record and its ``extra`` fields as a single-line object."""
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # `default=str` rather than letting a value raise: a formatter that
        # raises loses the record and prints a traceback from inside logging,
        # which is a worse outcome than one field rendered as its repr.
        return json.dumps(payload, default=str, ensure_ascii=False)


#: uvicorn's own loggers. Its default config gives ``uvicorn`` and
#: ``uvicorn.access`` their own text handlers with ``propagate: False``, and
#: declares no ``root`` entry at all — so nothing it writes reaches a handler
#: installed here.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _adopt_uvicorn_loggers() -> None:
    """Let uvicorn's records reach the handler this process installed.

    Without this the stream is half JSON and half text, and ``jq`` fails on
    the first uvicorn line rather than on anything this service wrote — which
    makes "one JSON object per record" false exactly where an operator would
    check it. Ordering works out: uvicorn configures its logging in
    ``Config.__init__`` and imports the application afterwards, so a
    ``--factory`` target runs second and this undoes what it set up.
    """
    for name in _UVICORN_LOGGERS:
        adopted = logging.getLogger(name)
        adopted.handlers.clear()
        adopted.propagate = True


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
            _adopt_uvicorn_loggers()
        return
    logging.basicConfig(level=resolved, format=_FORMAT)
    if requested != "text":
        get_logger(__name__).warning(
            "unrecognised %s=%r; logging as text", ENV_LOG_FORMAT, requested
        )
