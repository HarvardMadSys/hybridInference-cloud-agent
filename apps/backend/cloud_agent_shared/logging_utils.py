"""The JSON log format both deployment units emit.

The control plane and the host each keep their own ``logging_utils`` for what
differs between them. Their ``configure_process_logging`` functions differ:
the control plane keeps its handler so that saved admin settings can change
it later, and the host configures once, from its entrypoint. Each
``get_logger`` also defaults to its own module's name. The rest was the same
code in both places, and it lives here instead: how a record is rendered as
JSON, and how uvicorn's loggers are handed over to the handler a process
installed. A fix to either is made and tested once. Both images already ship
this package, so importing it adds no dependency between the two packages.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

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
#: a process installs on the root logger.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def adopt_uvicorn_loggers() -> None:
    """Let uvicorn's records reach the handler this process installed.

    Without this the stream is half JSON and half text, and ``jq`` fails on
    the first uvicorn line rather than on anything this service wrote — which
    makes "one JSON object per record" false exactly where an operator would
    check it. Ordering works out: uvicorn configures its logging in
    ``Config.__init__`` and imports the application afterwards, so a
    ``--factory`` target runs second and this undoes what it set up.
    """
    for name in UVICORN_LOGGERS:
        adopted = logging.getLogger(name)
        adopted.handlers.clear()
        adopted.propagate = True
