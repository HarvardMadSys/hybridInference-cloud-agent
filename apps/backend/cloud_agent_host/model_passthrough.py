"""The member's request meter on a direct plan: its CLI's requests, forwarded unchanged.

On a direct plan the CLI speaks its provider's own protocol with its owner's
own key. The member's launcher (:mod:`cloud_agent_host.protocol_translator`)
still starts the member's loopback service in front of it, in *passthrough*,
only so that each model request can be measured
(:mod:`cloud_agent_shared.request_prefix`): what it sends ahead of the
conversation, its system prompt and tool schemas, which no CLI reports.

The service is bound at launch to one upstream origin, ``https://<authority>``
(or, for a member on a connection the deployment provides, the host's model
relay at ``http://<authority>`` on the group's own bridge), and a request
cannot choose another. Its CLI is told
``http://127.0.0.1:<port>/<authority><path>``: the provider's authority stays in
the URL the CLI reads, so a CLI that recognizes its provider from the URL's
text still does, and the service answers only paths under ``/<authority>``,
forwarding the rest of each path to the origin.

It holds no key and issues no token. The CLI's own credentials pass through in
its own headers, so a sibling member that reaches the port can do nothing
through it that it cannot already do over the public egress; what it could add
is a record on this member's stderr, and records are sizes the owner reads,
never anything the platform acts on.

Requests and answers pass unchanged but for what a hop owns: hop-by-hop
headers, ``Host``, and the body's framing (a chunked request is forwarded with
its length; an answer without one is relayed chunked). An answer streams as it
arrives, the provider's errors and their bodies included, since a CLI decides
its retries and its compaction from them. A model request's body is measured
after it has been sent, while the provider works on it, and a body the meter
cannot read is forwarded all the same. Each CLI connection keeps its own
upstream connection between requests, as the CLI's direct connection would;
one the provider closed while idle is replaced before it is used.

An answer that ends before the length it declared closes the CLI's connection
at once, as the provider's own short answer would have, so the CLI sees the
error and retries rather than waiting for bytes that will never come.

As for the translator: HTTPS with the system trust store, no proxy read from
the environment, no redirect followed (a 3xx is relayed for the CLI to handle;
a ``Location`` on the origin's own path is pointed back under ``/<authority>``,
where the CLI's URL resolves it), a request the CLI closes is closed upstream
at once, and the service exits with its CLI.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import select
import ssl
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cloud_agent_host.protocol_translator import (
    CONNECT_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    LaunchError,
    PrefixMeter,
    emit_record,
    watch_client,
)
from cloud_agent_shared.protocol_compat import (
    ENV_CLIENT,
    ENV_MODEL,
    ENV_TOKEN,
    ENV_UPSTREAM_KEY,
    ENV_UPSTREAM_RELAY,
    ENV_UPSTREAM_URL,
    LAUNCH_ENV,
    PASSTHROUGH,
    passthrough_authority,
    upstream_port,
    upstream_scheme,
)
from cloud_agent_shared.request_prefix import MAX_MEASURED_BODY_BYTES, protocol_for_path

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable, MutableMapping

#: The largest request body forwarded; a provider refuses far smaller ones.
MAX_FORWARD_BODY_BYTES = 128 * 1024 * 1024
#: What one read of an answer relays at most; an event stream relays sooner.
RELAY_CHUNK_BYTES = 64 * 1024
#: A chunked request's size line, extensions included.
MAX_CHUNK_LINE_BYTES = 4096
#: Headers that belong to one hop (RFC 9110 section 7.6.1), and those this hop
#: writes itself.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "expect",
    }
)
#: Methods whose request always carries a length, even an empty one.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

#: How the service opens an upstream connection. Production always uses
#: ``HTTPSConnection`` with the system trust store; only in-process test code
#: replaces this, never a launch variable or a request.
UPSTREAM_CONNECTION: Callable[..., http.client.HTTPConnection] = http.client.HTTPSConnection
#: How the service reaches the host's model relay, the one upstream it speaks
#: to in the clear. Replaced only by in-process test code, like the above.
RELAY_CONNECTION: Callable[..., http.client.HTTPConnection] = http.client.HTTPConnection


def _log(category: str) -> None:
    """One line on stderr naming what happened; never a URL, header or body."""
    print(f"model-request-meter: {category}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class PassthroughBinding:
    """The one upstream origin this member's service forwards to."""

    host: str
    port: int
    #: False only for the host's model relay, reached in the clear.
    tls: bool = True

    @property
    def authority(self) -> str:
        """``host[:port]``, as the CLI's URL names it."""
        return passthrough_authority(self.host, self.port)

    @property
    def origin(self) -> str:
        """The upstream's origin, for a CLI that must reach it without the meter."""
        return f"{'https' if self.tls else 'http'}://{self.authority}"

    @classmethod
    def from_launch(cls, env: MutableMapping[str, str]) -> PassthroughBinding:
        """Take the launch variables out of ``env``: an origin, and nothing secret."""
        values = {name: env.pop(name, "") for name in LAUNCH_ENV}
        if values[ENV_CLIENT] != PASSTHROUGH:
            raise LaunchError("not a passthrough launch")
        if values[ENV_UPSTREAM_KEY] or values[ENV_TOKEN] or values[ENV_MODEL]:
            raise LaunchError("a passthrough holds no key, token or model")
        parts = urlsplit(values[ENV_UPSTREAM_URL])
        scheme = upstream_scheme(values[ENV_UPSTREAM_RELAY], parts.scheme)
        if (
            not scheme
            or not parts.hostname
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
        ):
            raise LaunchError(
                "the upstream must be a plain https origin, or the host's model relay"
            )
        return cls(
            host=parts.hostname, port=upstream_port(scheme, parts.port), tls=scheme == "https"
        )

    def upstream_path(self, path: str) -> str | None:
        """The origin's path for a request to ``path``; None outside this binding."""
        prefix = "/" + self.authority
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix) :]
        if rest and rest[0] not in "/?":
            return None
        return rest if rest.startswith("/") else "/" + rest

    def client_location(self, location: str) -> str:
        """A redirect's ``Location`` as the CLI must read it to reach the same place.

        The CLI resolves a path against the loopback origin, so a path on the
        origin (``/v2/messages``) gets the binding's prefix back. A full URL
        and a relative reference already name the same place for the CLI.
        """
        if location.startswith("/") and not location.startswith("//"):
            return "/" + self.authority + location
        return location


class _Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _ClientGone(Exception):
    """The CLI closed its connection: a cancelled request."""


def _connection_tokens(value: str | None) -> set[str]:
    """Header names a ``Connection`` header marks as this hop's."""
    return {token.strip().lower() for token in (value or "").split(",") if token.strip()}


def _forwarded(headers: Any) -> list[tuple[str, str]]:
    """Every end-to-end header, in order and with repeats."""
    own = _HOP_HEADERS | _connection_tokens(headers.get("connection"))
    return [(name, value) for name, value in headers.items() if name.lower() not in own]


def _closed(sock: socket.socket | None) -> bool:
    """Whether an idle upstream connection was closed (or spoke out of turn).

    Nothing is owed on an idle connection, so anything readable is its end --
    except, over TLS, records that carry no data (a TLS 1.3 server's later
    session ticket), which a non-blocking read consumes and reports as such.
    """
    if sock is None:
        return True
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    if not isinstance(sock, ssl.SSLSocket):
        return True
    timeout = sock.gettimeout()
    try:
        sock.setblocking(False)
        sock.recv(1)
    except ssl.SSLWantReadError:
        return False
    except OSError:
        return True
    finally:
        with contextlib.suppress(OSError):
            sock.settimeout(timeout)
    # Its end, or data nothing asked for: either way not a connection to reuse.
    return True


class _PassthroughHandler(BaseHTTPRequestHandler):
    """One CLI connection: each request forwarded to the origin, its answer relayed."""

    server: _PassthroughServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        """Start the connection with no upstream yet."""
        super().setup()
        self._upstream: http.client.HTTPConnection | None = None

    def finish(self) -> None:
        """The CLI's connection is over: so is its upstream one."""
        self._drop_upstream()
        super().finish()

    def log_message(self, *args: Any) -> None:
        """Silence the default access log: request lines carry paths."""
        del args

    def do_GET(self) -> None:
        """Forward the request; every method is forwarded the same way."""
        self._forward()

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    # -- answers of its own ----------------------------------------------------

    def _refuse(self, status: int, message: str) -> None:
        data = json.dumps({"error": {"type": "api_error", "message": message}}).encode()
        self.send_response_only(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(data)
        self.close_connection = True

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError as exc:
            raise _ClientGone from exc

    # -- request ---------------------------------------------------------------

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            return self._read_chunked()
        declared = self.headers.get("content-length")
        if declared is None:
            return b""
        try:
            length = int(declared)
        except ValueError:
            raise _Refused(400, "The request's Content-Length is not a number.") from None
        if length < 0:
            raise _Refused(400, "The request's Content-Length is not a number.")
        if length > MAX_FORWARD_BODY_BYTES:
            raise _Refused(413, "The request is too large to forward.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise _ClientGone
        return body

    def _read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
            if not line or len(line) > MAX_CHUNK_LINE_BYTES:
                raise _Refused(400, "The request's chunked body could not be read.")
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise _Refused(400, "The request's chunked body could not be read.") from None
            if size == 0:
                break
            if len(body) + size > MAX_FORWARD_BODY_BYTES:
                raise _Refused(413, "The request is too large to forward.")
            data = self.rfile.read(size + 2)
            if len(data) != size + 2:
                raise _ClientGone
            body += data[:size]
        # Trailers end at an empty line; none is forwarded.
        while (line := self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)) not in (b"\r\n", b"\n"):
            if not line or len(line) > MAX_CHUNK_LINE_BYTES:
                raise _Refused(400, "The request's chunked body could not be read.")
        return bytes(body)

    def _forward(self) -> None:
        path = self.server.binding.upstream_path(self.path)
        if path is None:
            self._refuse(404, "Not found.")
            return
        try:
            body = self._read_body()
            connection = self._send(path, body)
        except _Refused as refused:
            self._refuse(refused.status, refused.message)
            return
        except _ClientGone:
            self.close_connection = True
            return
        self._measure(path, body)
        try:
            self._answer(connection)
        except _ClientGone:
            _log("the client closed the request")
            self.close_connection = True
            self._drop_upstream()

    def _send(self, path: str, body: bytes) -> http.client.HTTPConnection:
        """The request, sent upstream; a stale kept connection is replaced once."""
        headers = _forwarded(self.headers)
        for _ in range(2):
            reused = self._upstream is not None
            connection = self._connection()
            try:
                connection.putrequest(self.command, path, skip_host=True, skip_accept_encoding=True)
                connection.putheader("Host", self.server.binding.authority)
                for name, value in headers:
                    connection.putheader(name, value)
                if body or self.command in _BODY_METHODS:
                    connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body or None)
                return connection
            except (OSError, http.client.HTTPException):
                self._drop_upstream()
                if not reused:
                    break
        _log("the provider could not be reached")
        raise _Refused(502, "The model provider could not be reached.")

    def _measure(self, path: str, body: bytes) -> None:
        """Record what a model request sends ahead of the conversation; never fail it."""
        protocol = protocol_for_path(path) if self.command == "POST" else None
        encoding = (self.headers.get("content-encoding") or "identity").strip().lower()
        if protocol is None or encoding != "identity" or not body:
            return
        if len(body) > MAX_MEASURED_BODY_BYTES:
            return
        try:
            self.server.meter.report(protocol, json.loads(body), emit_record)
        except Exception:
            # A measurement is diagnostic: whatever a body holds, the request
            # it describes was already sent, and its answer is still relayed.
            return

    # -- upstream --------------------------------------------------------------

    def _connection(self) -> http.client.HTTPConnection:
        if self._upstream is not None and _closed(self._upstream.sock):
            self._drop_upstream()
        if self._upstream is None:
            binding = self.server.binding
            self._upstream = (
                UPSTREAM_CONNECTION(
                    binding.host, binding.port, timeout=CONNECT_TIMEOUT_S, context=self.server.tls
                )
                if binding.tls
                else RELAY_CONNECTION(binding.host, binding.port, timeout=CONNECT_TIMEOUT_S)
            )
        return self._upstream

    def _drop_upstream(self) -> None:
        if self._upstream is not None:
            with contextlib.suppress(OSError):
                self._upstream.close()
            self._upstream = None

    def _answer(self, connection: http.client.HTTPConnection) -> None:
        upstream_socket = connection.sock
        done = threading.Event()
        watch_client(self.connection, upstream_socket, done)
        try:
            if upstream_socket is not None:
                upstream_socket.settimeout(IDLE_TIMEOUT_S)
            try:
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                _log("the provider did not answer")
                self._drop_upstream()
                self._refuse(502, "The model provider did not answer.")
                return
            if not self._relay(response) or response.will_close:
                self._drop_upstream()
        finally:
            done.set()

    def _relay(self, response: http.client.HTTPResponse) -> bool:
        """Relay ``response`` whole; whether its upstream connection can be used again."""
        bodiless = self.command == "HEAD" or response.status in {204, 304} or response.status < 200
        unframed = not bodiless and response.length is None
        # An HTTP/1.0 client cannot read a chunked answer: it gets one that
        # ends when the connection does.
        chunked = unframed and self.request_version != "HTTP/1.0"
        own = _HOP_HEADERS | _connection_tokens(response.getheader("connection"))
        self.send_response_only(response.status, response.reason)
        for name, value in response.getheaders():
            if name.lower() == "location":
                self.send_header(name, self.server.binding.client_location(value))
            elif name.lower() not in own:
                self.send_header(name, value)
        declared = response.getheader("content-length")
        if bodiless and declared is not None:
            self.send_header("Content-Length", declared)
        elif chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif unframed:
            self.send_header("Connection", "close")
        elif not bodiless:
            self.send_header("Content-Length", str(response.length))
        try:
            self.end_headers()
        except OSError as exc:
            raise _ClientGone from exc
        if bodiless:
            return True
        return self._relay_body(response, chunked=chunked)

    def _relay_body(self, response: http.client.HTTPResponse, *, chunked: bool) -> bool:
        while True:
            try:
                data = response.read1(RELAY_CHUNK_BYTES)
            except (OSError, http.client.HTTPException, ValueError):
                # The answer broke off: the CLI sees it end unfinished, as it
                # would have from the provider itself.
                _log("the provider's answer broke off")
                self.close_connection = True
                return False
            if not data:
                break
            self._write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)
        if response.length:
            # The provider closed before the length it declared (``read1``
            # reports that as an end, not an error): end the CLI's answer short
            # too, now, rather than leave it waiting for the rest.
            _log("the provider's answer broke off")
            self.close_connection = True
            return False
        if chunked:
            self._write(b"0\r\n\r\n")
        return True


class _PassthroughServer(ThreadingHTTPServer):
    """The member's loopback service in passthrough, on the socket the launcher bound."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A request that failed past its own handling -- usually a closed client: one line."""
        del request, client_address
        _log("a request ended early")

    def __init__(self, sock: socket.socket, binding: PassthroughBinding) -> None:
        """Serve on ``sock`` for ``binding``; nothing is bound here."""
        super().__init__(("127.0.0.1", 0), _PassthroughHandler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
        self.binding = binding
        self.meter = PrefixMeter(mode="passthrough")
        self.tls = ssl.create_default_context()


def server(sock: socket.socket, binding: PassthroughBinding) -> ThreadingHTTPServer:
    """The service that answers the CLI on ``sock``, ready to serve."""
    return _PassthroughServer(sock, binding)


__all__ = [
    "MAX_FORWARD_BODY_BYTES",
    "PassthroughBinding",
    "server",
]
