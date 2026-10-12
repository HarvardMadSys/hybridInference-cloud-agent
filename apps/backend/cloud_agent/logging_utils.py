"""Module loggers for the control plane.

Mirrors the host package's shim and exists for the same reason: the gateway's
``get_logger`` couples module loggers to its JSON formatter and per-request
context, none of which this service has. Duplicated rather than shared because
the two packages are separate deployment units — the control plane runs as a
web service and the host runs on a machine in the pool, and a shared module
would be a dependency between them that the split exists to remove. The JSON
formatter below is duplicated into the host shim under that same rule.

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

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

#: Set the control plane's log level without touching code or flags.
ENV_LOG_LEVEL = "AGENT_LOG_LEVEL"

#: ``json`` renders one JSON object per record; anything else keeps the text
#: format. The entrypoint reads this during bootstrap; the saved admin value
#: controls subsequent changes to the handler the control plane owns.
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
_process_handler: logging.StreamHandler[Any] | None = None
_process_owns_uvicorn = False


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
            _adopt_uvicorn_loggers()
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
        _adopt_uvicorn_loggers()
        _process_owns_uvicorn = True
    return True
