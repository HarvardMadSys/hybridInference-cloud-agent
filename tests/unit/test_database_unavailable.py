"""A database restart is answered as an outage to retry, never as a bug.

On 2026-11-12 a deploy recreated the control plane's PostgreSQL, gone for 0.4 s.
A host's ``POST /v1/agent/worker/claim`` and ``POST /v1/agent/hosts/profiling-policy``
met it and answered 500, logged as ``unhandled_request_error`` and once more by
uvicorn as "Exception in ASGI application". What is pinned here: 503 with
``Retry-After`` and a warning, on every listener, for what asyncpg really raises
when the database is out of reach -- most of it raised by asyncpg itself against
:mod:`tests.support.fake_postgres` -- and for nothing else, so a real bug stays
the 500 it was.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import logging
import os
import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from tests.support.fake_postgres import ADMIN_SHUTDOWN, STARTING_UP, FakePostgres
from tests.support.owners import use_session_secret

from cloud_agent.api._database_unavailable import (
    RETRY_AFTER_SECONDS,
    DatabaseUnavailableGuard,
    database_unavailable,
)
from cloud_agent.api._local_auth import enforce_rate_limits
from cloud_agent.api.deps import get_agent_app_credentials, get_http_client, get_storage
from cloud_agent.app import (
    create_app,
    create_github_relay_app,
    create_mcp_app,
    install_admin_no_store,
)
from cloud_agent.tokens import mint_host_token, mint_residency_github_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable


async def _raised(awaitable: Awaitable[Any]) -> BaseException:
    try:
        await awaitable
    except Exception as exc:
        return exc
    raise AssertionError("expected a failure")


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ── What asyncpg raises, and which of it is the database out of reach ──────


async def test_a_server_still_starting_up_is_out_of_reach() -> None:
    async with FakePostgres() as server:
        server.refusal = STARTING_UP
        exc = await _raised(asyncpg.connect(server.dsn))
    assert isinstance(exc, asyncpg.CannotConnectNowError)
    assert database_unavailable(exc)


async def test_a_server_not_listening_is_out_of_reach() -> None:
    exc = await _raised(asyncpg.connect(f"postgresql://agent@127.0.0.1:{_closed_port()}/agent"))
    assert isinstance(exc, ConnectionRefusedError)
    assert database_unavailable(exc)


async def test_a_server_that_never_answers_is_out_of_reach_once_asyncpg_gives_up() -> None:
    """Hung, or behind a route that drops what it is sent: asyncpg's connect timeout.

    asyncpg raises it in ``asyncpg.connect`` itself, around the whole handshake,
    whether a pool asked for the connection or not.
    """
    async with FakePostgres() as server:
        server.stall_connections = True
        direct = await _raised(asyncpg.connect(server.dsn, timeout=0.1))
        pool = await asyncpg.create_pool(server.dsn, min_size=0, max_size=1, timeout=0.1)
        try:
            pooled = await _raised(pool.acquire())
        finally:
            pool.terminate()
    assert isinstance(direct, TimeoutError)
    assert isinstance(pooled, TimeoutError)
    assert database_unavailable(direct)
    assert database_unavailable(pooled)


async def _connect_failing_with(monkeypatch: pytest.MonkeyPatch, code: int) -> BaseException:
    """What ``asyncpg.connect`` raises when opening its socket fails with ``code``."""

    async def create_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", create_connection)
    exc = await _raised(asyncpg.connect(f"postgresql://agent@127.0.0.1:{_closed_port()}/agent"))
    assert type(exc) is OSError
    assert exc.errno == code
    return exc


@pytest.mark.parametrize(
    "code",
    [errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN],
    ids=errno.errorcode.get,
)
async def test_no_route_to_the_database_is_out_of_reach(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Its host or network gone: a plain ``OSError``, these errnos having no class of their own."""
    assert database_unavailable(await _connect_failing_with(monkeypatch, code))


@pytest.mark.parametrize(
    "code",
    [errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, errno.EADDRNOTAVAIL],
    ids=errno.errorcode.get,
)
async def test_this_process_out_of_descriptors_memory_or_ports_is_not_an_outage(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """``[Errno 24] Too many open files`` is a leak here, a bug, and stays a 500."""
    assert not database_unavailable(await _connect_failing_with(monkeypatch, code))


async def test_a_connection_closed_during_a_statement_and_used_after_is_out_of_reach() -> None:
    async with FakePostgres() as server:
        conn = await asyncpg.connect(server.dsn)
        server.close_on_query = True
        during = await _raised(conn.execute("SELECT 1"))
        after = await _raised(conn.execute("SELECT 1"))
    assert isinstance(during, asyncpg.ConnectionDoesNotExistError)
    assert isinstance(after, asyncpg.InterfaceError)
    assert database_unavailable(during)
    assert database_unavailable(after)


async def test_a_statement_on_a_connection_the_server_ended_while_idle_is_out_of_reach() -> None:
    """The race of a restart: the shutdown notice has arrived, the close has not."""
    async with FakePostgres() as server:
        conn = await asyncpg.connect(server.dsn)
        server.end_sessions_while_idle(*ADMIN_SHUTDOWN)
        await asyncio.sleep(0.05)
        exc = await _raised(conn.execute("SELECT 1"))
        conn.terminate()
    assert isinstance(exc, asyncpg.InternalClientError)
    assert database_unavailable(exc)


@pytest.mark.parametrize(
    "refusal",
    [
        ("3D000", 'database "agent" does not exist'),
        ("28P01", 'password authentication failed for user "agent"'),
    ],
    ids=["no such database", "wrong password"],
)
async def test_a_server_refusing_this_configuration_is_not_an_outage(
    refusal: tuple[str, str],
) -> None:
    """A retry gets the same answer: an operator has something to fix."""
    async with FakePostgres() as server:
        server.refusal = refusal
        exc = await _raised(asyncpg.connect(server.dsn))
    assert isinstance(exc, asyncpg.PostgresError)
    assert not database_unavailable(exc)


async def test_a_statement_timeout_is_not_an_outage() -> None:
    async with FakePostgres() as server:
        conn = await asyncpg.connect(server.dsn)
        server.stall_queries = True
        exc = await _raised(conn.execute("SELECT 1", timeout=0.1))
        conn.terminate()
    assert isinstance(exc, TimeoutError)
    assert not database_unavailable(exc)


async def test_waiting_for_a_busy_pool_is_not_an_outage() -> None:
    async with FakePostgres() as server:
        pool = await asyncpg.create_pool(server.dsn, min_size=1, max_size=1)
        try:
            async with pool.acquire():
                exc = await _raised(pool.acquire(timeout=0.1))
        finally:
            pool.terminate()
    assert isinstance(exc, TimeoutError)
    assert not database_unavailable(exc)


async def test_a_refused_connection_to_something_else_is_not_the_database() -> None:
    exc = await _raised(asyncio.open_connection("127.0.0.1", _closed_port()))
    assert isinstance(exc, ConnectionRefusedError)
    assert not database_unavailable(exc)


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.AdminShutdownError("terminating connection due to administrator command"),
        asyncpg.CrashShutdownError(
            "terminating connection because of crash of another server process"
        ),
        asyncpg.CannotConnectNowError("the database system is shutting down"),
        asyncpg.IdleSessionTimeoutError("terminating connection due to idle-session timeout"),
        asyncpg.TooManyConnectionsError("sorry, too many clients already"),
        asyncpg.ConnectionFailureError("could not receive data from client"),
        asyncpg.TransactionResolutionUnknownError("connection lost during COMMIT"),
        asyncpg.InterfaceError("cannot perform operation: connection is closed"),
        asyncpg.InterfaceError(
            "cannot call Transaction.__aexit__(): the underlying connection is closed"
        ),
    ],
    ids=repr,
)
def test_these_are_the_database_out_of_reach(exc: BaseException) -> None:
    assert database_unavailable(exc)


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.UniqueViolationError("duplicate key value violates unique constraint"),
        asyncpg.ForeignKeyViolationError("insert or update violates foreign key constraint"),
        asyncpg.CheckViolationError("new row violates check constraint"),
        asyncpg.DeadlockDetectedError("deadlock detected"),
        asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
        asyncpg.IdleInTransactionSessionTimeoutError(
            "terminating connection due to idle-in-transaction timeout"
        ),
        asyncpg.DatabaseDroppedError("terminating connection because database was dropped"),
        asyncpg.ProtocolViolationError("bind message supplies 1 parameters, but requires 2"),
        asyncpg.UndefinedTableError('relation "agent_jobs" does not exist'),
        asyncpg.PostgresSyntaxError('syntax error at or near "SELEC"'),
        asyncpg.DataError("invalid input for query argument $1: 'x' (an integer is required)"),
        asyncpg.InterfaceError("cannot perform operation: another operation is in progress"),
        asyncpg.InterfaceError(
            "cannot call Connection.fetch(): connection has been released back to the pool"
        ),
        asyncpg.InterfaceError("pool is closing"),
        asyncpg.InternalClientError("unknown error in protocol implementation"),
        ConnectionRefusedError(111, "Connection refused"),
        FileNotFoundError(2, "No such file or directory"),
        TimeoutError(),
        KeyError("host"),
        ValueError("bad input"),
    ],
    ids=repr,
)
def test_these_are_not(exc: BaseException) -> None:
    assert not database_unavailable(exc)


def test_an_error_raised_from_an_outage_is_one_and_one_raised_while_handling_it_is_not() -> None:
    """``raise ... from`` says why; an exception during the handling may be the handler's bug."""
    outage = asyncpg.CannotConnectNowError("the database system is starting up")
    caused = RuntimeError("could not read the host")
    caused.__cause__ = outage
    during = KeyError("host")
    during.__context__ = outage
    assert database_unavailable(caused)
    assert not database_unavailable(during)


# ── The guard: what a route the database failed answers ─────────────────────


def _failing_app(failure: BaseException) -> FastAPI:
    """A route and a dependency that fail, under the main listener's own handlers."""
    app = FastAPI()

    async def fail() -> None:
        raise failure

    async def failing_dependency() -> None:
        raise failure

    async def behind_a_dependency(_: None = Depends(failing_dependency)) -> dict[str, str]:
        return {"ok": "never"}

    async def body(first: bytes) -> AsyncIterator[bytes]:
        yield first
        raise failure

    async def streams() -> StreamingResponse:
        return StreamingResponse(body(b"first chunk"))

    async def events() -> StreamingResponse:
        return StreamingResponse(body(b"id: 7\ndata: {}\n\n"), media_type="text/event-stream")

    app.add_api_route("/v1/agent/fail", fail, methods=["POST"])
    app.add_api_route("/admin/agent/fail", fail, methods=["GET"])
    app.add_api_route("/v1/agent/dependency", behind_a_dependency, methods=["GET"])
    app.add_api_route("/v1/agent/stream", streams, methods=["GET"])
    app.add_api_route("/v1/agent/events", events, methods=["GET"])
    install_admin_no_store(app)
    app.add_middleware(DatabaseUnavailableGuard)
    return app


async def _call(app: FastAPI, method: str, path: str, **kwargs: Any) -> Any:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def _assert_retry(response: Any) -> None:
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/v1/agent/fail"), ("GET", "/admin/agent/fail"), ("GET", "/v1/agent/dependency")],
)
async def test_the_database_out_of_reach_is_a_503_to_retry_and_a_warning(
    caplog: pytest.LogCaptureFixture, method: str, path: str
) -> None:
    failure = asyncpg.CannotConnectNowError("the database system is starting up")
    caplog.set_level(logging.WARNING, logger="cloud_agent.app")

    response = await _call(_failing_app(failure), method, path)

    _assert_retry(response)
    assert response.json() == {
        "detail": {
            "error": {
                "type": "database_unavailable",
                "message": "The service is briefly unavailable. Please try again shortly.",
            }
        }
    }
    [record] = [r for r in caplog.records if r.name == "cloud_agent.app"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "agent_database_unavailable"
    assert (record.__dict__["event"], record.__dict__["path"]) == (
        "agent_database_unavailable",
        path,
    )
    assert record.exc_info is not None and record.exc_info[1] is failure


async def test_any_other_failure_is_still_the_500_it_was(caplog: pytest.LogCaptureFixture) -> None:
    failure = asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
    caplog.set_level(logging.WARNING, logger="cloud_agent.app")

    response = await _call(_failing_app(failure), "POST", "/v1/agent/fail")

    assert response.status_code == 500
    assert "retry-after" not in response.headers
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "cloud_agent.app"] == [
        (logging.ERROR, "unhandled_request_error")
    ]
    # And it leaves the guard as it entered it, for the server to log.
    transport = ASGITransport(app=_failing_app(failure))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(asyncpg.UniqueViolationError) as raised:
            await client.post("/v1/agent/fail")
    assert raised.value is failure


async def test_a_response_already_under_way_is_not_rewritten() -> None:
    """Half a body cannot become a 503, nor be ended as if it were whole: it fails as it was."""
    failure = asyncpg.CannotConnectNowError("the database system is starting up")
    transport = ASGITransport(app=_failing_app(failure))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(asyncpg.CannotConnectNowError):
            await client.get("/v1/agent/stream")


async def test_an_event_stream_the_database_fails_ends_for_its_client_to_resume(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """As one that reached its time limit does: the client reconnects from its cursor."""
    failure = asyncpg.ConnectionDoesNotExistError(
        "connection was closed in the middle of operation"
    )
    caplog.set_level(logging.WARNING, logger="cloud_agent.app")
    transport = ASGITransport(app=_failing_app(failure))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/agent/events")

    assert response.status_code == 200
    assert response.text == "id: 7\ndata: {}\n\n"
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "cloud_agent.app"] == [
        (logging.WARNING, "agent_database_unavailable")
    ]


async def test_an_event_stream_that_fails_for_any_other_reason_still_fails() -> None:
    failure = asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
    transport = ASGITransport(app=_failing_app(failure))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(asyncpg.UniqueViolationError):
            await client.get("/v1/agent/events")


async def test_a_bug_while_an_event_stream_handles_an_outage_still_fails() -> None:
    """Mid-stream, the guard judges the error the stream raised, not the one it handled.

    uvicorn announces ASGI 2.3, and under it Starlette runs a stream in a task
    group that raises the stream's error again ``from`` whatever it was
    handling -- the outage here, which would otherwise end the stream as one.
    """

    async def body() -> AsyncIterator[bytes]:
        yield b"id: 7\ndata: {}\n\n"
        try:
            raise asyncpg.CannotConnectNowError("the database system is starting up")
        except asyncpg.CannotConnectNowError:
            hosts: dict[str, bytes] = {}
            yield hosts["runner-1"]  # the bug: a KeyError raised while handling it

    async def events() -> StreamingResponse:
        return StreamingResponse(body(), media_type="text/event-stream")

    app = FastAPI()
    app.add_api_route("/v1/agent/events", events, methods=["GET"])
    app.add_middleware(DatabaseUnavailableGuard)

    async def as_uvicorn_serves_it(scope: Any, receive: Any, send: Any) -> None:
        scope["asgi"] = {**scope.get("asgi", {}), "spec_version": "2.3"}
        await app(scope, receive, send)

    transport = ASGITransport(app=as_uvicorn_serves_it)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(KeyError) as raised:
            await client.get("/v1/agent/events")
    # It went past the guard with the outage as its cause, as the task group
    # made it: following that cause would have ended the stream cleanly.
    assert isinstance(raised.value.__cause__, asyncpg.CannotConnectNowError)


async def test_a_websocket_passes_through_untouched() -> None:
    failure = asyncpg.CannotConnectNowError("the database system is starting up")
    sent: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        raise failure

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:
        return {"type": "websocket.connect"}

    guard = DatabaseUnavailableGuard(app)
    with pytest.raises(asyncpg.CannotConnectNowError):
        await guard({"type": "websocket", "path": "/ws"}, receive, send)
    assert sent == []


# ── Every listener, and the routes that answered 500 on 2026-11-12 ────────


@pytest.mark.parametrize("build", [create_app, create_mcp_app, create_github_relay_app])
def test_every_listener_is_guarded(build: Any) -> None:
    assert DatabaseUnavailableGuard in [middleware.cls for middleware in build().user_middleware]


class _RestartingHostPool:
    """The host lookup every host request starts with, while the database restarts."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    async def authenticate_host(self, *, host: str, credential_hash: str) -> None:
        raise self.failure


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/agent/worker/claim", {"worker_id": "worker-1", "lease_ttl_seconds": 60}),
        ("/v1/agent/hosts/profiling-policy", {}),
    ],
)
@pytest.mark.parametrize(
    "failure",
    [
        asyncpg.CannotConnectNowError("the database system is starting up"),
        asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
    ],
    ids=["starting up", "dead pooled connection"],
)
async def test_a_host_that_meets_a_restart_is_told_to_retry(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any], failure: BaseException
) -> None:
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "database-unavailable-test-secret")
    app = create_app()
    app.state.storage = SimpleNamespace(host_pool=_RestartingHostPool(failure))
    token = mint_host_token(host="runner-1")

    response = await _call(
        app, "POST", path, json=body, headers={"Authorization": f"Bearer {token}"}
    )

    _assert_retry(response)
    assert response.json()["detail"]["error"]["type"] == "database_unavailable"


class _RestartingRelay:
    async def resolve_github_caller(self, *, thread_id: str, residency_generation: int) -> None:
        raise asyncpg.CannotConnectNowError("the database system is starting up")


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("https://api.github.com/repos/owner/repo/pulls", "token"),
        ("https://github.com/owner/repo.git/info/refs?service=git-upload-pack", "basic"),
    ],
    ids=["gh", "git"],
)
async def test_git_and_gh_that_meet_a_restart_are_told_to_retry_in_their_own_shape(
    monkeypatch: pytest.MonkeyPatch, url: str, scheme: str
) -> None:
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "database-unavailable-test-secret")
    relay = create_github_relay_app()
    relay.dependency_overrides[get_storage] = lambda: SimpleNamespace(
        github_relay=_RestartingRelay(), source_control=None
    )
    relay.dependency_overrides[get_agent_app_credentials] = lambda: None
    relay.dependency_overrides[get_http_client] = lambda: None
    token = mint_residency_github_token(thread_id="athr_1", residency_generation=2)
    authorization = (
        f"token {token}"
        if scheme == "token"
        else "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    )

    transport = ASGITransport(app=relay, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://x") as client:
        response = await client.get(url, headers={"Authorization": authorization})

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    message = "GitHub is briefly unavailable here. Try again shortly."
    if scheme == "token":
        assert response.json() == {"message": message}
    else:
        assert response.text == message + "\n"


# ── The 503s a restart already produced now say when to retry too ─────────


async def test_sign_in_that_cannot_count_its_attempts_says_when_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_session_secret(monkeypatch)

    class Restarting:
        async def hit_rate_limit(self, **_: Any) -> float:
            raise asyncpg.CannotConnectNowError("the database system is starting up")

    app = FastAPI()
    app.state.storage = SimpleNamespace(local_accounts=Restarting())
    request = Request({"type": "http", "app": app, "headers": [], "method": "POST", "path": "/"})

    with pytest.raises(HTTPException) as refused:
        await enforce_rate_limits(request, [("login_ip", "192.0.2.1", 600, 4)])

    assert refused.value.status_code == 503
    assert refused.value.headers == {"Retry-After": str(RETRY_AFTER_SECONDS)}
