"""Grant POSTs must stop at a total deadline even when response bytes progress."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress

import httpx
import pytest

from cloud_agent import settings
from cloud_agent.gateway import GatewayError
from cloud_agent.grants import GrantClient

BASE = "https://gateway.example"
TOKEN = "dispatch-token"
_DEADLINE_S = 0.1


@pytest.fixture
def short_request_deadline():
    settings.apply_settings(
        1_000_000,
        settings.encode_changes({"gateway_request_timeout_s": _DEADLINE_S}),
    )
    try:
        yield
    finally:
        settings.reset_settings()


class _SlowDrip(httpx.AsyncByteStream):
    async def __aiter__(self):
        for chunk in (b'{"grant_', b'id":', b'"agr_1",', b'"token":"x"}'):
            await asyncio.sleep(0.04)
            yield chunk

    async def aclose(self) -> None:
        return None


class _SlowDripTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowDrip(), request=request)


class _DelayedResponseTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(_DEADLINE_S * 1.6)
        return httpx.Response(200, json={"grant_id": "agr_1"}, request=request)


class _StalledBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed.set()


class _StalledBodyTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: _StalledBody) -> None:
        self.stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=self.stream, request=request)


def _client(transport: httpx.AsyncBaseTransport) -> tuple[GrantClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=transport)
    return GrantClient(BASE, TOKEN, client=http), http


def _assert_ambiguous_timeout(error: GatewayError) -> None:
    assert error.status_code is None
    assert error.definitive is False
    assert error.error_type == "gateway_timeout"


def _assert_ambiguous_transport_failure(error: GatewayError) -> None:
    assert error.status_code is None
    assert error.definitive is False


async def test_mint_stops_a_slow_drip_at_the_total_deadline(short_request_deadline) -> None:
    client, http = _client(_SlowDripTransport())
    started = time.monotonic()
    try:
        with pytest.raises(GatewayError) as raised:
            await client.mint(user_id="user_1", job_id="job_1", attempt_id="1")
    finally:
        await http.aclose()

    assert time.monotonic() - started < 0.5
    _assert_ambiguous_timeout(raised.value)


async def test_renew_stops_a_delayed_response_at_the_total_deadline(short_request_deadline) -> None:
    client, http = _client(_DelayedResponseTransport())
    started = time.monotonic()
    try:
        with pytest.raises(GatewayError) as raised:
            await client.renew("agr_1")
    finally:
        await http.aclose()

    assert time.monotonic() - started < 0.5
    _assert_ambiguous_timeout(raised.value)


async def test_stalled_response_body_stops_at_the_request_deadline(short_request_deadline) -> None:
    stream = _StalledBody()
    client, http = _client(_StalledBodyTransport(stream))
    started = time.monotonic()
    try:
        with pytest.raises(GatewayError) as raised:
            await asyncio.wait_for(client.renew("agr_1"), timeout=0.4)
    finally:
        await http.aclose()

    assert time.monotonic() - started < 0.5
    _assert_ambiguous_timeout(raised.value)
    assert stream.closed.is_set()


async def test_revoke_remains_best_effort_when_the_total_deadline_expires(
    short_request_deadline,
) -> None:
    client, http = _client(_SlowDripTransport())
    started = time.monotonic()
    try:
        assert await client.revoke("agr_1") is False
    finally:
        await http.aclose()

    assert time.monotonic() - started < 0.5


async def test_caller_cancellation_closes_a_stalled_post_body() -> None:
    stream = _StalledBody()
    client, http = _client(_StalledBodyTransport(stream))
    task = asyncio.create_task(client.renew("agr_1"))
    try:
        await asyncio.wait_for(stream.started.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed.is_set()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await http.aclose()


async def test_lost_mint_response_reuses_the_idempotent_attempt_grant(
    short_request_deadline,
) -> None:
    """A committed grant remains recoverable after a slow, lost HTTP response."""
    grants: dict[tuple[str, str], dict[str, object]] = {}
    requests: list[dict[str, object]] = []
    server_errors: list[str] = []
    grant_response = {"grant_id": "agr_committed", "token": "grant-token"}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = {
                name.strip().lower(): value.strip()
                for line in head.decode("latin-1").split("\r\n")[1:]
                if ":" in line
                for name, value in (line.split(":", 1),)
            }
            request_body = await reader.readexactly(int(headers["content-length"]))
            payload = json.loads(request_body)
            requests.append(payload)
            key = (payload["external_job_id"], payload["external_attempt_id"])
            grant = grants.setdefault(key, dict(grant_response))
            response_body = json.dumps(grant).encode()

            if len(requests) == 1:
                # The remote commits the grant, then loses its HTTP response.
                writer.close()
                await writer.wait_closed()
                return

            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\nConnection: close\r\n\r\n".encode()
                + response_body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            server_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    http = httpx.AsyncClient()
    client = GrantClient(f"http://127.0.0.1:{port}", TOKEN, client=http)
    try:
        with pytest.raises(GatewayError) as raised:
            await client.mint(user_id="user_1", job_id="job_1", attempt_id="1")
        _assert_ambiguous_transport_failure(raised.value)

        try:
            recovered = await client.mint(user_id="user_1", job_id="job_1", attempt_id="1")
        except GatewayError as exc:
            pytest.fail(
                f"retry failed: {exc}; server_errors={server_errors!r}; requests={requests!r}"
            )
    finally:
        server.close()
        await server.wait_closed()
        await http.aclose()

    assert recovered["grant_id"] == "agr_committed"
    assert len(grants) == 1
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert server_errors == []
