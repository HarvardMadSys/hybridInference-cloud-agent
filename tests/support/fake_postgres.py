"""A PostgreSQL that says only what a test scripts: enough of the wire protocol for asyncpg.

asyncpg connects to it and runs simple queries on it, and the test decides what
a real server would have said instead: nothing at all to a new connection,
"the database system is starting up" to it, a close in the middle of a
statement, a statement that is never answered, or the notice a server shutting
down sends every idle client before it closes the socket. A real server cannot be stopped between that
notice and the close, and that gap is where asyncpg's pool used to lose a
connection for good; here the test holds it open for as long as it likes.
"""

from __future__ import annotations

import asyncio
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import TracebackType

_SSL_REQUEST = 80877103
_CANCEL_REQUEST = 80877102

#: What a server shutting down (``pg_ctl stop -m fast``, a container being
#: replaced) tells each client it is about to disconnect.
ADMIN_SHUTDOWN = ("57P01", "terminating connection due to administrator command")
STARTING_UP = ("57P03", "the database system is starting up")


def _message(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("!i", len(body) + 4) + body


def fatal(sqlstate: str, text: str) -> bytes:
    """An ``ErrorResponse`` of severity FATAL."""
    fields = (("S", "FATAL"), ("V", "FATAL"), ("C", sqlstate), ("M", text))
    body = b"".join(code.encode() + value.encode() + b"\0" for code, value in fields)
    return _message(b"E", body + b"\0")


_PARAMETERS = (
    ("server_version", "16.0"),
    ("server_encoding", "UTF8"),
    ("client_encoding", "UTF8"),
    ("DateStyle", "ISO, MDY"),
    ("integer_datetimes", "on"),
    ("TimeZone", "UTC"),
    ("standard_conforming_strings", "on"),
)

_HANDSHAKE = b"".join(
    [
        _message(b"R", struct.pack("!i", 0)),  # AuthenticationOk
        *(_message(b"S", f"{name}\0{value}\0".encode()) for name, value in _PARAMETERS),
        _message(b"K", struct.pack("!ii", 4242, 99)),  # BackendKeyData
        _message(b"Z", b"I"),  # ReadyForQuery, idle
    ]
)


class FakePostgres:
    """A server on a loopback port, as an async context manager.

    Attributes set by the test take effect on the next connection or statement:

    - ``stall_connections``: accept a new connection and never answer it, as a
      server that hangs would, or one behind a route that drops what it is sent.
    - ``refusal``: ``(sqlstate, message)`` answered to every new connection.
    - ``close_on_query``: close the connection when a statement arrives.
    - ``stall_queries``: never answer a statement.
    """

    def __init__(self) -> None:
        """Serve nothing until entered."""
        self.stall_connections = False
        self.refusal: tuple[str, str] | None = None
        self.close_on_query = False
        self.stall_queries = False
        self.connections = 0
        self._sessions: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> FakePostgres:
        """Start listening on a free loopback port."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close every session and stop listening."""
        self.close_sessions()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def dsn(self) -> str:
        """Where asyncpg reaches this server."""
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"postgresql://agent@127.0.0.1:{port}/agent"

    def end_sessions_while_idle(self, sqlstate: str, text: str) -> None:
        """Send every open session a FATAL notice, and leave the socket open."""
        for writer in self._sessions:
            writer.write(fatal(sqlstate, text))

    def close_sessions(self) -> None:
        """Close every open session's socket."""
        for writer in list(self._sessions):
            writer.close()
        self._sessions.clear()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            if self.stall_connections:
                # Held until the client gives up, or the sessions are closed.
                self._sessions.add(writer)
                await reader.read()
                return
            length, code = struct.unpack("!ii", await reader.readexactly(8))
            if code == _SSL_REQUEST:
                writer.write(b"N")
                length, code = struct.unpack("!ii", await reader.readexactly(8))
            await reader.readexactly(length - 8)
            if code == _CANCEL_REQUEST:
                return
            if self.refusal is not None:
                writer.write(fatal(*self.refusal))
                return
            writer.write(_HANDSHAKE)
            self._sessions.add(writer)
            await self._statements(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            self._sessions.discard(writer)
            writer.close()

    async def _statements(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            kind = await reader.readexactly(1)
            (length,) = struct.unpack("!i", await reader.readexactly(4))
            await reader.readexactly(length - 4)
            if kind == b"X":  # Terminate
                return
            if kind != b"Q":
                continue
            if self.close_on_query:
                return
            if not self.stall_queries:
                writer.write(_message(b"C", b"SELECT 1\0") + _message(b"Z", b"I"))
