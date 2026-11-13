"""Answer a database the control plane cannot reach as an outage, not as a bug.

A deploy that recreates PostgreSQL takes it away for a moment: 0.4 s on staging
on 2026-11-12. A request in that window meets a pooled connection the server
has just ended, or a server still answering "the database system is starting
up", and asyncpg raises. Nothing caught that, so it reached the catch-all in
``cloud_agent.app`` as an ``unhandled_request_error`` 500 -- a host's claim and
its profiling-policy read among them -- and uvicorn logged each one again as
"Exception in ASGI application": an outage that the same request gets past a
second later, reported as a defect.

:func:`database_unavailable` says which failures are the database being out of
reach rather than something a statement did. :class:`DatabaseUnavailableGuard`
answers those, on every route of a listener, with 503 and ``Retry-After``, and
logs a warning with the traceback. Everything else -- a constraint violation,
a syntax or programming error, a statement timeout, a database name or
password the server refuses -- is re-raised untouched and stays the 500 it was.

A response already under way cannot become a 503. An event stream resumes
from its cursor, so one the database fails mid-way is ended as cleanly as one
that reached its time limit, and its client reconnects; any other response is
re-raised as it was, because half a body ended cleanly would read as a whole.
Mid-stream only the error the stream raised itself counts, not one it was
raised from: a stream that wraps an outage in an error of its own fails as
that error.

A plain ASGI middleware rather than an exception handler. A handler is chosen
by class, and the classes these failures arrive as -- ``OSError``, asyncpg's
``InterfaceError`` -- also carry real bugs, which a handler could only send on
by raising them again from inside itself. Like ``CrossSiteWriteGuard`` it
neither buffers nor re-frames a response: it only watches for where one starts
and ends.
"""

from __future__ import annotations

import errno
import re
import socket
import traceback
from typing import TYPE_CHECKING

import asyncpg
from starlette.responses import JSONResponse

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Under the catch-all's logger, which is where the deployment's log selection
#: reads request failures: the warning that replaces a 500 is found where the
#: 500 was.
logger = get_logger("cloud_agent.app")

#: Seconds a client is told to wait before trying again. A restart is over in
#: about a second; a few more keep the retry out of the same window.
RETRY_AFTER_SECONDS = 5

#: SQLSTATEs with which the server ends a session or will not start one:
#: shutdown (57P01), another backend's crash (57P02), not yet or no longer
#: accepting connections (57P03), an idle-session timeout (57P05), and no
#: connection slot left (53300). Class 08 is added in :func:`_out_of_reach`.
_SESSION_REFUSED = frozenset({"57P01", "57P02", "57P03", "57P05", "53300"})

#: Class 08 is "connection exception" except for this one, a protocol
#: violation, which can be the client's own malformed message.
_PROTOCOL_VIOLATION = "08P01"

#: A server error on an idle connection -- its notice that it is ending the
#: session -- leaves asyncpg's protocol consuming up to a sync point that never
#: comes (state 2). The next statement on that connection fails like this
#: before it is sent, unless asyncpg has seen the connection close first.
_ENDED_WHILE_IDLE = re.compile(
    r"cannot switch to state \d+; another operation \(2\) is in progress"
)

#: How a refused, reset, timed-out or unresolvable connect reaches asyncpg's
#: caller. ``TimeoutError`` is the kernel giving up (``ETIMEDOUT``) and also
#: asyncpg's own connect timeout (60 s unless set): how connecting fails to a
#: database that never answers, hung or behind a route that drops what it is
#: sent. A missing certificate file (``FileNotFoundError``) or a TLS failure
#: (``ssl.SSLError``) is configuration, and not among them.
_NETWORK_ERRORS = (ConnectionError, TimeoutError, socket.gaierror)

#: The errnos of a connect that fails as a plain ``OSError`` (no subclass of
#: its own) because the database's host or network cannot be reached. Any
#: other errno stays a 500: out of file descriptors (``EMFILE``, ``ENFILE``),
#: buffers or memory (``ENOBUFS``, ``ENOMEM``) or local ports
#: (``EADDRNOTAVAIL``), the fault is on this side -- a leak, say -- and must
#: not pass for the database being out of reach. So does an ``OSError`` with
#: no errno: asyncio's one-line summary for a name whose several addresses all
#: failed, in which each errno survives only as text.
_UNREACHABLE = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN})

#: Where asyncpg opens connections: :func:`asyncpg.connect`, which a pool calls
#: as well and whose own frame raises its connect timeout, and the module it
#: connects through. Only an ``OSError`` raised through one of them can be the
#: database out of reach; one raised through the pool or a query (an acquire
#: or statement timeout) is not.
_CONNECT = ("asyncpg.connection", "connect")
_CONNECTING = "asyncpg.connect_utils"


def database_unavailable(exc: BaseException) -> bool:
    """Whether ``exc``, or an error it was raised from, is the database out of reach.

    Only explicit causes (``raise ... from``) are followed. An error raised
    while handling an outage belongs to the handler, and may be a bug. An event
    stream under way is judged by its own error alone
    (:class:`DatabaseUnavailableGuard`).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _out_of_reach(current):
            return True
        current = current.__cause__
    return False


def _out_of_reach(exc: BaseException) -> bool:
    if isinstance(exc, asyncpg.PostgresError):
        state = exc.sqlstate or ""
        return (state.startswith("08") and state != _PROTOCOL_VIOLATION) or (
            state in _SESSION_REFUSED
        )
    if isinstance(exc, asyncpg.InterfaceError):
        # "connection is closed", from the connection, its protocol, or a
        # transaction or statement whose connection it was. Not "released back
        # to the pool": that is also how a caller fails that uses a connection
        # after giving it back, and that bug must stay a 500.
        return str(exc).endswith("connection is closed")
    if isinstance(exc, asyncpg.InternalClientError):
        return _ENDED_WHILE_IDLE.fullmatch(str(exc)) is not None
    if isinstance(exc, OSError):
        return _while_connecting(exc) and (
            isinstance(exc, _NETWORK_ERRORS) or (type(exc) is OSError and exc.errno in _UNREACHABLE)
        )
    return False


def _while_connecting(exc: BaseException) -> bool:
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        module = frame.f_globals.get("__name__")
        if module == _CONNECTING or (module, frame.f_code.co_name) == _CONNECT:
            return True
    return False


def _event_stream(start: Message) -> bool:
    return any(
        name.lower() == b"content-type" and value.startswith(b"text/event-stream")
        for name, value in start.get("headers", ())
    )


def database_unavailable_response(scope: Scope) -> Response:
    """The answer of the main and MCP listeners: the envelope their refusals use."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "error": {
                    "type": "database_unavailable",
                    "message": "The service is briefly unavailable. Please try again shortly.",
                }
            }
        },
        headers={"Cache-Control": "no-store", "Retry-After": str(RETRY_AFTER_SECONDS)},
    )


class DatabaseUnavailableGuard:
    """Answer a request the database failed with 503, and let every other failure through."""

    def __init__(
        self,
        app: ASGIApp,
        answer: Callable[[Scope], Response] = database_unavailable_response,
    ) -> None:
        """Wrap ``app``; ``answer`` renders the 503 in the listener's own shape."""
        self.app = app
        self.answer = answer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on; answer it, or end its event stream, if the database failed it."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = finished = resumable = False

        async def watched(message: Message) -> None:
            nonlocal started, finished, resumable
            if message["type"] == "http.response.start":
                started, resumable = True, _event_stream(message)
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                finished = True
            await send(message)

        try:
            await self.app(scope, receive, watched)
        except Exception as exc:
            if finished or (started and not resumable):
                raise
            # A stream under way is judged by the error it raised alone. Under
            # uvicorn (ASGI 2.3) Starlette runs it in a task group that raises
            # its error again ``from`` whatever it was handling, so a bug in a
            # handler of an outage would arrive with the outage as its cause.
            if not (_out_of_reach(exc) if started else database_unavailable(exc)):
                raise
            logger.warning(
                "agent_database_unavailable",
                exc_info=exc,
                extra={"event": "agent_database_unavailable", "path": scope.get("path", "")},
            )
            if started:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await self.answer(scope)(scope, receive, send)


__all__ = [
    "RETRY_AFTER_SECONDS",
    "DatabaseUnavailableGuard",
    "database_unavailable",
    "database_unavailable_response",
]
