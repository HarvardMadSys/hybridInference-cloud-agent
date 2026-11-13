"""The launcher and loopback service a member runs beside its CLI.

The service has two modes. On a translated plan it translates (below); on a
direct plan it forwards the CLI's requests unchanged
(:mod:`cloud_agent_host.model_passthrough`). In both it measures what each
model request sends ahead of the conversation -- its system prompt and tool
schemas -- and writes the record on the CLI's stderr
(:mod:`cloud_agent_shared.request_prefix`), the only thing either mode adds.

A host starts a member on a translated plan as::

    python3 -I model-protocol-translator.pyz <the CLI's own argv ...>

with the upstream and the member's token in the environment
(``AGENT_COMPAT_*``, never argv). The launcher binds ``127.0.0.1`` on a free port, forks the
service, waits until the service reports it is serving, then replaces itself with the CLI: the process the host spawned,
recorded in its pidfile and reaped by UID, *is* the CLI, exactly as on a
direct plan. Before the exec the loopback origin replaces
``ORIGIN_PLACEHOLDER`` in the CLI's argv and environment, the member's token
replaces ``TOKEN_PLACEHOLDER`` in its environment (a token placeholder in its
argv refuses the launch), and every ``AGENT_COMPAT_*`` variable, the upstream
key among them, is removed from its environment.

The service is bound at launch to one client protocol, one upstream URL, one
key, one model and the turn's reasoning level (none at Auto); a request cannot
choose any of them. At a level, every upstream request carries it as
``reasoning_effort``; the CLI's own effort field is never forwarded. It answers only the
client's own endpoint, and only to a caller presenting the member's token
(issued by the control plane for this member alone): other members of the
group share the loopback interface, so the address alone protects nothing,
and a sibling holds a different token it cannot derive from the key they
share. There is no health or configuration
endpoint. When the CLI closes a request -- a cancelled turn -- the upstream
request is closed at once, not at its next byte. It exits with the CLI -- ``PR_SET_PDEATHSIG`` where available, and
its parent's identity checked every second -- so no listener holding the key
outlives the member's process.

Upstream calls go to ``<Base URL>/chat/completions`` over HTTPS with the
system trust store: ``http.client`` reads no proxy from the environment and
follows no redirect. Nothing the upstream sends in an error is passed on, and
the service logs only what kind of thing happened.

The pure conversion it relies on is ``cloud_agent_shared.protocol_compat``,
which the control plane's connection check shares. This module is the I/O
around it and runs only in the sandbox, from the zipapp ``compat_helper``
builds: it imports the standard library, that package and the request meter,
and of the host only the passthrough.

A passthrough launch (``AGENT_COMPAT_CLIENT=passthrough``) carries an origin
and nothing secret. If its service cannot be started -- or never reports that
it is serving -- the launcher still starts the CLI, pointed at that origin
directly: a measurement must never be what stops a turn. For the same reason a
record never waits on stderr: records go through a short queue to a writer
thread of their own, and one that finds the queue full is dropped.
"""

from __future__ import annotations

import contextlib
import ctypes
import hmac
import http.client
import json
import os
import queue
import select
import signal
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlsplit

from cloud_agent_shared.protocol_compat import (
    ANTHROPIC_MESSAGES,
    CLIENTS,
    ENV_CLIENT,
    ENV_MODEL,
    ENV_REASONING_EFFORT,
    ENV_TOKEN,
    ENV_UPSTREAM_KEY,
    ENV_UPSTREAM_URL,
    LAUNCH_ENV,
    ORIGIN_PLACEHOLDER,
    PASSTHROUGH,
    RESPONSES,
    TOKEN_PLACEHOLDER,
)
from cloud_agent_shared.protocol_compat.gate import CompatRejected, prepare
from cloud_agent_shared.protocol_compat.stream import ClientStream, client_error, client_response
from cloud_agent_shared.request_prefix import RECORD_VERSION, measure, record_line

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, MutableMapping

    from cloud_agent_shared.protocol_compat.namespaces import ToolNames

#: A token shorter than this is not one the control plane issued.
MIN_TOKEN_CHARS = 32
#: A reasoning level is a short word (``low``, ``xhigh``…).
MAX_EFFORT_CHARS = 16

#: The one path each client protocol is served at.
CLIENT_PATHS = {RESPONSES: "/v1/responses", ANTHROPIC_MESSAGES: "/v1/messages"}

MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
CONNECT_TIMEOUT_S = 30.0
#: How long the upstream may stay silent mid-answer (a long thinking pause).
IDLE_TIMEOUT_S = 900.0
_PR_SET_PDEATHSIG = 1
#: What CPython ignores from its start. An ignored signal stays ignored across
#: exec and ``sh`` cannot take one back, so without the defaults restored a CLI
#: started from here would begin with both ignored, unlike a direct launch.
#: Codex then passes SIGXFSZ on to every command it runs, where a write past
#: ``ulimit -f`` fails instead of killing the writer. ``subprocess`` restores
#: the same two for every command it starts.
_PYTHON_IGNORED = (signal.SIGPIPE, signal.SIGXFSZ)

#: How the service opens its one upstream connection. Production always uses
#: ``HTTPSConnection`` with the system trust store; only in-process test code
#: replaces this, never a launch variable or a request.
UPSTREAM_CONNECTION: Callable[..., http.client.HTTPConnection] = http.client.HTTPSConnection


class LaunchError(Exception):
    """The trusted launch is missing or malformed; nothing is started."""


@dataclass(frozen=True)
class Binding:
    """The one upstream, key and model this member's service may use."""

    client: str
    host: str
    port: int
    path: str
    model: str
    api_key: str = field(repr=False)
    token: str = field(repr=False)
    #: The turn's level, checked by the control plane; empty is Auto.
    reasoning_effort: str = ""

    @classmethod
    def from_launch(cls, env: MutableMapping[str, str], argv: list[str]) -> Binding:
        """Take the launch variables out of ``env``; the model from them or the CLI's ``--model``."""
        values = {name: env.pop(name, "") for name in LAUNCH_ENV}
        client, url, key = values[ENV_CLIENT], values[ENV_UPSTREAM_URL], values[ENV_UPSTREAM_KEY]
        model, token = values[ENV_MODEL] or _argv_model(argv), values[ENV_TOKEN]
        if client not in CLIENTS or not key or not model or len(token) < MIN_TOKEN_CHARS:
            raise LaunchError("incomplete model protocol compatibility launch")
        if any(TOKEN_PLACEHOLDER in arg for arg in argv):
            raise LaunchError("a token never goes in a process argument")
        effort = values[ENV_REASONING_EFFORT]
        if effort and not (
            len(effort) <= MAX_EFFORT_CHARS and effort.isascii() and effort.isalpha()
        ):
            raise LaunchError("a reasoning level is one plain word")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise LaunchError("the upstream must be a plain https URL")
        return cls(
            client=client,
            host=parts.hostname,
            port=parts.port or 443,
            path=parts.path.rstrip("/") + "/chat/completions",
            model=model,
            api_key=key,
            token=token,
            reasoning_effort=effort,
        )


def _argv_model(argv: list[str]) -> str:
    if "--model" in argv[:-1]:
        return argv[argv.index("--model") + 1]
    return ""


def _log(category: str) -> None:
    """One line on stderr naming what happened; never a body, key or token."""
    print(f"model-protocol-translator: {category}", file=sys.stderr, flush=True)


#: Prefixes one meter remembers before it starts over (and repeats a record).
MAX_REMEMBERED_PREFIXES = 256


class PrefixMeter:
    """Measure each request; a whole record for a prefix seen first, a repeat after.

    Thread-safe: the loopback service answers each connection on a thread of
    its own.
    """

    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def line(self, protocol: str, body: Any) -> bytes | None:
        """The line to write for one request ``body``, or None when it is not measured."""
        record = measure(protocol, body, mode=self.mode)
        return None if record is None else self._line(record)

    def report(self, protocol: str, body: Any, emit: Callable[[bytes], bool]) -> None:
        """Measure ``body`` and ``emit`` its line.

        A line ``emit`` drops leaves its prefix unreported, so the next request
        with that prefix writes the whole record again rather than a repeat the
        host could not resolve.
        """
        record = measure(protocol, body, mode=self.mode)
        if record is not None and not emit(self._line(record)):
            with self._lock:
                self._seen.discard(record["fp"])

    def _line(self, record: dict[str, Any]) -> bytes:
        with self._lock:
            if record["fp"] in self._seen:
                return record_line({"v": RECORD_VERSION, "fp": record["fp"], "repeat": True})
            if len(self._seen) >= MAX_REMEMBERED_PREFIXES:
                self._seen.clear()
            self._seen.add(record["fp"])
        return record_line(record)


#: Records waiting for stderr. A CLI whose stderr is not being read fills the
#: pipe; past this many, a record is dropped rather than waited for.
MAX_PENDING_RECORDS = 64


class RecordWriter:
    """Records written on one descriptor from a thread of their own.

    A request's thread only queues its record: a blocked descriptor -- a full
    pipe nobody is reading -- stalls this writer, never a model request. The
    thread starts with the first record, in the service process, after the fork.
    """

    def __init__(self, fd: int = 2) -> None:
        self._fd = fd
        self._queue: queue.Queue[bytes] = queue.Queue(MAX_PENDING_RECORDS)
        self._start = threading.Lock()
        self._started = False

    def put(self, line: bytes) -> bool:
        """Queue ``line``; False when it was dropped instead."""
        with self._start:
            if not self._started:
                threading.Thread(target=self._run, name="prefix-records", daemon=True).start()
                self._started = True
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            view = memoryview(self._queue.get())
            try:
                while view:
                    view = view[os.write(self._fd, view) :]
            except OSError:
                continue  # A closed stderr: the record is lost, never a request.


_RECORDS = RecordWriter()


def emit_record(line: bytes) -> bool:
    """Queue one request-prefix record for stderr; False when it was dropped."""
    return _RECORDS.put(line)


class _ClientGone(Exception):
    """The CLI closed its connection: a cancelled request."""


def _sse_payloads(response: http.client.HTTPResponse) -> Iterator[dict[str, Any]]:
    """Each JSON ``data:`` payload of the upstream stream, up to ``[DONE]`` and no further.

    Raises ``ValueError`` for a line that is not JSON, not an object or too
    long: the answer is then unreadable, never merely shorter.
    """
    while True:
        line = response.readline(MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("upstream line too long")
        text = line.decode("utf-8").strip()
        if not text.startswith("data:"):
            continue
        data = text[5:].strip()
        if data == "[DONE]":
            # The answer is over; an upstream holding the connection open
            # after it must not keep the client waiting.
            return
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("upstream payload is not an object")
        yield payload


def _upstream_status(status: int) -> int:
    # A redirect is not followed, and is the provider's failure to answer here.
    return 502 if 300 <= status < 400 else status


class _Handler(BaseHTTPRequestHandler):
    """One request from the CLI: authenticate, gate, translate, relay."""

    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        """Silence the default access log: request lines may carry query strings."""
        del args

    # -- answers ---------------------------------------------------------------

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, client_error(self.server.binding.client, status, message))

    def _write(self, frames: list[str]) -> None:
        try:
            for frame in frames:
                self.wfile.write(frame.encode())
            self.wfile.flush()
        except OSError as exc:
            raise _ClientGone from exc

    # -- request ---------------------------------------------------------------

    def _authorized(self) -> bool:
        presented = self.headers.get("x-api-key") or ""
        bearer = self.headers.get("authorization") or ""
        if bearer[:7].lower() == "bearer ":
            presented = presented or bearer[7:].strip()
        return hmac.compare_digest(presented.encode(), self.server.binding.token.encode())

    def _read_body(self) -> Any:
        if self.headers.get("transfer-encoding"):
            raise CompatRejected("Send the request with a Content-Length.", status=411)
        try:
            length = int(self.headers.get("content-length") or "")
        except ValueError:
            raise CompatRejected("Send the request with a Content-Length.", status=411) from None
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise CompatRejected("The request is too large to translate.", status=413)
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            raise CompatRejected("The request body is not JSON.") from None

    def do_GET(self) -> None:
        """No resource is served except the client's own POST endpoint."""
        self._refuse_other()

    def do_HEAD(self) -> None:
        """The CLI's connection warm-up; nothing is served here."""
        self._refuse_other()

    def _refuse_other(self) -> None:
        if not self._authorized():
            self._error(401, "This endpoint needs the member's token.")
            return
        self._error(404, "Not found.")

    def do_POST(self) -> None:
        """Translate one request onto the bound upstream and relay its answer."""
        binding = self.server.binding
        if not self._authorized():
            _log("refused an unauthenticated request")
            self._error(401, "This endpoint needs the member's token.")
            return
        if self.path.split("?", 1)[0] != CLIENT_PATHS[binding.client]:
            self._error(404, "Not found.")
            return
        try:
            body = self._read_body()
            prepared = prepare(binding.client, body, binding.model, binding.reasoning_effort)
        except CompatRejected as rejected:
            _log(f"refused a request ({rejected.status})")
            self._error(rejected.status, rejected.message)
            return
        self._measure(body)
        try:
            self._relay(prepared.chat, body, prepared.names)
        except _ClientGone:
            _log("the client closed the request")

    def _measure(self, body: Any) -> None:
        """Record what the CLI's request sends ahead of the conversation; never fail it."""
        try:
            self.server.meter.report(self.server.binding.client, body, emit_record)
        except Exception:
            # A measurement is diagnostic: the request is translated regardless.
            return

    # -- upstream --------------------------------------------------------------

    def _relay(self, chat: dict[str, Any], body: dict[str, Any], names: ToolNames | None) -> None:
        binding = self.server.binding
        streaming = bool(chat.get("stream"))
        connection = UPSTREAM_CONNECTION(
            binding.host, binding.port, timeout=CONNECT_TIMEOUT_S, context=self.server.tls
        )
        done = threading.Event()
        upstream_socket: socket.socket | None = None
        response: http.client.HTTPResponse | None = None
        try:
            try:
                connection.request(
                    "POST",
                    binding.path,
                    body=json.dumps(chat).encode(),
                    headers={
                        "authorization": f"Bearer {binding.api_key}",
                        "content-type": "application/json",
                        "accept": "text/event-stream" if streaming else "application/json",
                        "user-agent": "cloud-agent-model-protocol-translator/1",
                    },
                )
                # Held apart from the connection: ``http.client`` hands a
                # close-delimited answer's socket to the response and forgets it.
                upstream_socket = connection.sock
                watch_client(self.connection, upstream_socket, done)
                if upstream_socket is not None:
                    upstream_socket.settimeout(IDLE_TIMEOUT_S)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                _log("the provider could not be reached")
                self._error(502, "The model provider could not be reached.")
                return
            self._answer(response, body, names, streaming=streaming)
        finally:
            # The provider sees this request end now, whether or not it would
            # have kept the connection.
            done.set()
            if response is not None:
                response.close()
            shutdown_socket(upstream_socket)
            connection.close()

    def _answer(
        self,
        response: http.client.HTTPResponse,
        body: dict[str, Any],
        names: ToolNames | None,
        *,
        streaming: bool,
    ) -> None:
        if response.status >= 300:
            _log(f"the provider answered HTTP {response.status}")
            status = _upstream_status(response.status)
            self._error(status, f"The model provider answered HTTP {response.status}.")
        elif streaming:
            self._relay_stream(response, body, names)
        else:
            self._relay_json(response, body, names)

    def _relay_stream(
        self,
        response: http.client.HTTPResponse,
        body: dict[str, Any],
        names: ToolNames | None,
    ) -> None:
        binding = self.server.binding
        stream = ClientStream(binding.client, model=binding.model, request_body=body, names=names)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for payload in _sse_payloads(response):
                self._write(stream.feed(payload))
        except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError):
            stream.fail()
            _log("the provider's stream broke off or could not be read")
        self._write(stream.finish())
        if not stream.succeeded:
            _log("the provider's stream did not finish")

    def _relay_json(
        self, response: http.client.HTTPResponse, body: dict[str, Any], names: ToolNames | None
    ) -> None:
        binding = self.server.binding
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            chat = json.loads(raw) if len(raw) <= MAX_RESPONSE_BYTES else None
        except (OSError, http.client.HTTPException, ValueError):
            chat = None
        answer = (
            client_response(
                binding.client, chat, model=binding.model, request_body=body, names=names
            )
            if isinstance(chat, dict)
            else None
        )
        if answer is None:
            _log("the provider's answer was unreadable or unfinished")
            self._error(502, "The model provider's answer could not be read.")
            return
        self._send_json(200, answer)


def shutdown_socket(sock: socket.socket | None) -> None:
    """End both directions of ``sock`` now, if there is one."""
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def watch_client(
    client: socket.socket, upstream: socket.socket | None, done: threading.Event
) -> None:
    """From the moment the request is sent until ``done``: the CLI's close cancels it."""
    threading.Thread(
        target=_close_upstream_when_client_leaves,
        args=(client, upstream, done),
        name="client-watch",
        daemon=True,
    ).start()


def _close_upstream_when_client_leaves(
    client: socket.socket, upstream: socket.socket | None, done: threading.Event
) -> None:
    """Shut the upstream request down the moment the CLI closes its own.

    The request has been read whole, so the CLI sends nothing more: the socket
    turning readable with no data is its close (a cancelled turn). Without
    this the service would notice only on its next write -- after the
    upstream's next byte, which a silent provider may not send for minutes.
    """
    while not done.is_set():
        try:
            readable, _, _ = select.select([client], [], [], 0.25)
            if readable and not client.recv(1, socket.MSG_PEEK):
                break
        except (OSError, ValueError):
            break
        if readable:
            # The CLI sent more before this answer ended (a pipelined request
            # on a kept connection): not a close, and not a reason to spin.
            time.sleep(0.25)
    else:
        return
    if not done.is_set():
        shutdown_socket(upstream)


class _Server(ThreadingHTTPServer):
    """The member's loopback service, on the socket the launcher bound."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A request that failed past its own handling -- usually a closed client: one line."""
        del request, client_address
        _log("a request ended early")

    def __init__(self, sock: socket.socket, binding: Binding) -> None:
        """Serve on ``sock`` for ``binding``; nothing is bound here."""
        super().__init__(("127.0.0.1", 0), _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
        self.binding = binding
        self.meter = PrefixMeter(mode="translated")
        self.tls = ssl.create_default_context()


def _die_with(parent: int) -> None:
    """Exit when ``parent`` -- the CLI after its exec -- is gone."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass  # The poll below still applies.
    if os.getppid() != parent:
        os._exit(0)

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(1.0)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


#: How long the launcher waits for its service to report that it is serving.
READY_TIMEOUT_S = 10.0
_READY = b"r"


def _serve_child(
    sock: socket.socket,
    make: Callable[[socket.socket], ThreadingHTTPServer],
    parent: int,
    ready: int,
) -> NoReturn:
    """The forked service: detached from the CLI's stdio, gone when it is.

    It reports on ``ready`` once its server exists; a service that fails
    before then just exits, and the launcher sees the pipe close unreported.
    """
    try:
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 0)
        os.dup2(null, 1)
        _die_with(parent)
        server = make(sock)
        os.write(ready, _READY)
        os.close(ready)
        server.serve_forever()
    except BaseException:
        _log("the loopback service stopped")
    finally:
        os._exit(0)


def _start_service(make: Callable[[socket.socket], ThreadingHTTPServer]) -> str:
    """Bind the loopback socket and fork the server ``make`` builds onto it.

    Returns the origin the CLI is told, once the service has reported that it
    is serving; raises OSError when it cannot start or does not report in time.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    read_end, write_end = os.pipe()
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        parent = os.getpid()
        child = os.fork()
        if child == 0:
            os.close(read_end)
            _serve_child(sock, make, parent, write_end)
        os.close(write_end)
        write_end = -1
        readable, _, _ = select.select([read_end], [], [], READY_TIMEOUT_S)
        if not readable or os.read(read_end, 1) != _READY:
            with contextlib.suppress(OSError):
                os.kill(child, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.waitpid(child, 0)
            raise OSError("the loopback service did not start")
    finally:
        sock.close()
        for end in (read_end, write_end):
            if end >= 0:
                os.close(end)
    return origin


def _exec_cli(argv: list[str], env: dict[str, str], replacements: dict[str, str]) -> NoReturn:
    cli = [_replaced(arg, replacements) for arg in argv]
    cli_env = {name: _replaced(value, replacements) for name, value in env.items()}
    # Only the CLI's side of the fork: the service is Python, where a closed
    # client must stay an error rather than a SIGPIPE.
    for number in _PYTHON_IGNORED:
        signal.signal(number, signal.SIG_DFL)
    os.execvpe(cli[0], cli, cli_env)


def _replaced(value: str, replacements: dict[str, str]) -> str:
    for placeholder, actual in replacements.items():
        value = value.replace(placeholder, actual)
    return value


def _launch_passthrough(argv: list[str], env: dict[str, str]) -> NoReturn:
    """A direct plan's member: its CLI behind the meter, or straight at its origin."""
    from cloud_agent_host import model_passthrough

    if any(TOKEN_PLACEHOLDER in value for value in (*argv, *env.values())):
        raise LaunchError("a passthrough has no token to write")
    binding = model_passthrough.PassthroughBinding.from_launch(env)
    marked = f"{ORIGIN_PLACEHOLDER}/{binding.authority}"
    try:
        origin = _start_service(lambda sock: model_passthrough.server(sock, binding))
    except OSError:
        _log("the request meter could not start; the CLI reaches its provider directly")
        _exec_cli(argv, env, {marked: f"https://{binding.authority}"})
    _exec_cli(argv, env, {ORIGIN_PLACEHOLDER: origin})


def launch(argv: list[str]) -> NoReturn:
    """Start the service for this member and become its CLI."""
    if not argv:
        raise LaunchError("no CLI to launch")
    env = dict(os.environ)
    if env.get(ENV_CLIENT) == PASSTHROUGH:
        _launch_passthrough(argv, env)
    binding = Binding.from_launch(env, argv)
    try:
        origin = _start_service(lambda sock: _Server(sock, binding))
    except OSError:
        raise LaunchError("the member's loopback service could not start") from None
    _exec_cli(argv, env, {ORIGIN_PLACEHOLDER: origin, TOKEN_PLACEHOLDER: binding.token})


def main(argv: list[str]) -> int:
    """``python3 -I model-protocol-translator.pyz <CLI argv>``."""
    try:
        launch(argv)
    except (LaunchError, OSError) as exc:
        message = str(exc) if isinstance(exc, LaunchError) else "could not start the CLI"
        _log(message)
        return 70


__all__ = [
    "CLIENT_PATHS",
    "CONNECT_TIMEOUT_S",
    "IDLE_TIMEOUT_S",
    "MIN_TOKEN_CHARS",
    "Binding",
    "LaunchError",
    "PrefixMeter",
    "RecordWriter",
    "emit_record",
    "launch",
    "main",
    "shutdown_socket",
    "watch_client",
]
