"""The model relay, end to end: a placeholder in, the deployment's key out, or nothing at all."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import model_relay
from cloud_agent.api.deps import get_storage
from cloud_agent.application.model_connection_stream import CheckStop
from cloud_agent.application.model_connections import operator_connection_cipher
from cloud_agent.domain.operator_models import Allowance, end_user_tag
from cloud_agent.tokens import (
    mint_residency_github_token,
    mint_residency_mcp_token,
    mint_residency_model_token,
)

pytestmark = pytest.mark.asyncio

_KEY = "sk-deployment-key-0123456789"
_ROOT = "model-relay-settings-root-key"
_CONNECTION = "oconn_" + "c" * 32
_TODAY = date(2026, 11, 12)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "model-relay-test-secret")
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", _ROOT)


class _Store:
    """A running turn of ``athr_1`` (residency 2) by ``user_1`` on one provided connection."""

    def __init__(self, *, running: bool = True, limit: int | None = 100, used: int = 0) -> None:
        self.running = running
        self.limit, self.used = limit, used
        self.refunds = 0
        self.key_refused: list[bool] = []
        self.global_concurrency: int | None = None
        self.bound_revision: int | None = 3
        self.on_other_connection = False
        self.connection: dict[str, Any] | None = {
            "id": _CONNECTION,
            "name": "Provider",
            "base_url": "https://provider.example/v1",
            "models": ["model-1", "model-2"],
            "enabled": True,
            "deleted_at": None,
            "credential_revision": 3,
            "api_key_ciphertext": operator_connection_cipher().encrypt(_KEY),
            "end_user_header": None,
            "user_concurrency": 4,
            "key_refused_at": None,
        }
        self.operator_models = self

    async def resolve_model_relay_caller(self, *, thread_id, residency_generation, model):
        if not self.running or (thread_id, residency_generation) != ("athr_1", 2):
            return None
        connection = None if self.on_other_connection else self.connection
        offered = connection is not None and model in connection["models"]
        return {
            "job_id": "ajob_1",
            "attempt_id": 1,
            "thread_id": thread_id,
            "user_id": "user_1",
            "model_connection_id": _CONNECTION if connection else "mconn_" + "0" * 32,
            "bound_revision": self.bound_revision if connection else None,
            "connection": connection,
            "allowance": Allowance(limit=self.limit, used=self.used) if offered else None,
            "global_concurrency": self.global_concurrency,
        }

    async def count_operator_request(self, *, connection_id, user_id, model):
        assert (connection_id, user_id) == (_CONNECTION, "user_1")
        if self.limit == 0:
            return {"outcome": "not_offered", "day": None}
        if self.limit is not None and self.used >= self.limit:
            return {"outcome": "exhausted", "day": None}
        self.used += 1
        return {"outcome": "counted", "day": _TODAY}

    async def refund_operator_request(self, *, connection_id, user_id, model, day):
        assert day == _TODAY
        self.used -= 1
        self.refunds += 1

    async def mark_operator_key_refused(self, *, connection_id, refused):
        self.key_refused.append(refused)


class _Provider:
    """The provider, answering every request as scripted and recording what it got."""

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        *,
        fail: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.status, self.fail = status, fail
        self.body = body if body is not None else {"id": "answer"}
        self.headers = headers or {}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(await request.aread())
        if self.fail:
            raise httpx.ConnectError("no route", request=request)
        # A stream, as from the network: the relay streams it back unread.
        return httpx.Response(
            self.status,
            headers={
                "content-type": "application/json",
                "x-request-id": "req_1",
                **self.headers,
            },
            stream=httpx.ByteStream(json.dumps(self.body).encode()),
        )


def _placeholder(generation: int = 2) -> str:
    return mint_residency_model_token(thread_id="athr_1", residency_generation=generation)


def _upstream(handle: Any) -> httpx.AsyncClient:
    """The listener's own client, dialling ``handle`` in place of the network."""
    return model_relay.relay_client(httpx.MockTransport(handle))


def _app(
    store: _Store | None,
    upstream: httpx.AsyncClient,
    *,
    slots: model_relay.Slots | None = None,
    guard: Any = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(model_relay.router)
    app.dependency_overrides[get_storage] = lambda: store
    app.dependency_overrides[model_relay.get_relay_client] = lambda: upstream
    held = slots or model_relay.Slots()
    app.dependency_overrides[model_relay.get_slots] = lambda: held

    async def allowed(_base_url: str) -> None:
        return None

    app.dependency_overrides[model_relay.get_endpoint_guard] = lambda: guard or allowed
    return app


async def _send(
    store: _Store | None,
    provider: _Provider,
    method: str = "POST",
    path: str = "/v1/chat/completions",
    *,
    bearer: str | None = "",
    slots: model_relay.Slots | None = None,
    guard: Any = None,
    upstream: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """One request to the relay; ``upstream`` is a client several requests share."""
    client = upstream or _upstream(provider.handle)
    app = _app(store, client, slots=slots, guard=guard)
    headers = dict(kwargs.pop("headers", {}))
    if bearer is not None:
        headers.setdefault("Authorization", f"Bearer {bearer or _placeholder()}")
    if method == "POST" and "json" not in kwargs and "content" not in kwargs:
        kwargs["json"] = {"model": "model-1", "messages": [{"role": "user", "content": "hi"}]}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://relay") as caller:
            return await caller.request(method, path, headers=headers, **kwargs)
    finally:
        if upstream is None:
            await client.aclose()


class _Caller:
    """One CLI's connection to the relay, driven by hand so that it can leave.

    ``spec_version`` is the ASGI version the server speaks. Up to 2.3 (what
    uvicorn speaks) a streamed response is told its caller left; from 2.4 it
    finds out when its next send fails.
    """

    def __init__(self, app: FastAPI, *, spec_version: str = "2.3") -> None:
        body = json.dumps({"model": "model-1", "messages": []}).encode()
        self._unread = [{"type": "http.request", "body": body, "more_body": False}]
        self._tells = spec_version == "2.3"
        self.left = asyncio.Event()
        self.sent: list[dict[str, Any]] = []
        self.heard = asyncio.Event()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "server": ("relay", 80),
            "client": ("192.0.2.1", 40000),
            "headers": [
                (b"host", b"relay"),
                (b"authorization", f"Bearer {_placeholder()}".encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
        self.call = asyncio.ensure_future(app(scope, self._receive, self._send))

    async def _receive(self) -> dict[str, Any]:
        if self._unread:
            return self._unread.pop(0)
        if not self._tells:
            await asyncio.Event().wait()
        await self.left.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if self.left.is_set():
            # Dropped, as uvicorn does, or refused, as ASGI 2.4 has it.
            if self._tells:
                return
            raise OSError("the caller has left")
        self.sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            self.heard.set()

    async def ended(self) -> BaseException | None:
        """Wait for the relay to finish with this request; what it raised, if anything."""
        done, _ = await asyncio.wait({self.call}, timeout=5)
        assert done, "the relay is still serving a caller that left"
        return self.call.exception()


class _OpenStream(httpx.AsyncByteStream):
    """An answer that has begun and says nothing more until it is told to."""

    def __init__(self, *, then: BaseException | bytes | None = None) -> None:
        self.go = asyncio.Event()
        self.closed = False
        self._then = then

    async def __aiter__(self):
        yield b"data: first\n\n"
        await self.go.wait()
        if isinstance(self._then, BaseException):
            raise self._then
        if self._then:
            yield self._then

    async def aclose(self) -> None:
        self.closed = True


def _slots_held(slots: model_relay.Slots) -> int:
    return slots.held(("user", _CONNECTION, "user_1")) + slots.held(
        ("model", _CONNECTION, "model-1")
    )


# -- what is forwarded -----------------------------------------------------------


async def test_a_running_turns_request_goes_out_with_the_deployments_key() -> None:
    store, provider = _Store(), _Provider()
    response = await _send(store, provider)

    assert response.status_code == 200 and response.json() == {"id": "answer"}
    assert response.headers["x-request-id"] == "req_1"
    [sent] = provider.requests
    assert str(sent.url) == "https://provider.example/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {_KEY}"
    assert "x-api-key" not in sent.headers
    # The body is the CLI's own, byte for byte.
    assert json.loads(provider.bodies[0])["messages"] == [{"role": "user", "content": "hi"}]
    assert store.used == 1 and store.refunds == 0
    # Neither the key nor the placeholder comes back.
    assert _KEY not in response.text and "amd." not in response.text


async def test_an_anthropic_client_keeps_its_own_way_of_carrying_a_key() -> None:
    store, provider = _Store(), _Provider()
    response = await _send(
        store,
        provider,
        path="/v1/messages",
        bearer=None,
        headers={"x-api-key": _placeholder(), "anthropic-version": "2023-06-01"},
        params={"beta": "true"},
    )
    assert response.status_code == 200
    [sent] = provider.requests
    assert str(sent.url) == "https://provider.example/v1/messages?beta=true"
    assert sent.headers["x-api-key"] == _KEY and "authorization" not in sent.headers
    assert sent.headers["anthropic-version"] == "2023-06-01"


async def test_only_the_headers_an_agent_needs_seen_go_on_to_the_provider() -> None:
    store, provider = _Store(), _Provider()
    kept = {
        "Accept": "text/event-stream",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "User-Agent": "agent-cli/1.2",
        "Anthropic-Version": "2023-06-01",
        "Anthropic-Beta": "tools-2024-04-04",
        "OpenAI-Beta": "responses=v1",
    }
    # Each of these would be the sandbox telling the provider what to do with
    # the deployment's key, or who and where the request is from.
    dropped = {
        "OpenAI-Organization": "org-other",
        "OpenAI-Project": "proj_other",
        "Cookie": "session=abc",
        "X-Forwarded-For": "203.0.113.9",
        "Forwarded": "for=203.0.113.9",
        "HTTP-Referer": "https://app.example",
        "X-Title": "Another app",
        "Idempotency-Key": "another-owners-request",
        "Proxy-Authorization": "Basic abc",
        "X-Stainless-Lang": "js",
        "X-Anything-Else": "1",
    }
    response = await _send(store, provider, headers={**kept, **dropped})

    assert response.status_code == 200
    [sent] = provider.requests
    for name, value in kept.items():
        assert sent.headers[name] == value
    for name in dropped:
        assert name not in sent.headers
    # Nothing else goes out than those, the key and what the relay's own
    # connection to the provider says of itself.
    assert set(sent.headers) == {name.lower() for name in kept} | {
        "authorization",
        "host",
        "content-length",
        "connection",
    }


async def test_a_client_that_names_no_encoding_is_answered_unencoded() -> None:
    """The answer goes back as the provider encoded it, so it may not choose one unasked."""
    store, provider = _Store(), _Provider()
    seen: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await provider.handle(request)

    # Sent by hand: a test client adds an Accept-Encoding of its own.
    upstream = _upstream(handle)
    caller = _Caller(_app(store, upstream))
    try:
        await caller.ended()
    finally:
        await upstream.aclose()
    [sent] = seen
    assert sent.headers["accept-encoding"] == "identity"


async def test_listing_models_and_counting_tokens_are_not_counted() -> None:
    store, provider = _Store(limit=1, used=1), _Provider()
    listed = await _send(store, provider, "GET", "/v1/models")
    counted = await _send(store, provider, path="/v1/messages/count_tokens")
    assert (listed.status_code, counted.status_code) == (200, 200)
    assert store.used == 1
    assert [str(request.url) for request in provider.requests] == [
        "https://provider.example/v1/models",
        "https://provider.example/v1/messages/count_tokens",
    ]


async def test_the_provider_is_told_who_only_by_an_opaque_tag() -> None:
    store, provider = _Store(), _Provider()
    assert store.connection is not None
    store.connection["end_user_header"] = "X-End-User"
    await _send(store, provider, headers={"X-End-User": "somebody-else"})
    [sent] = provider.requests
    # The relay's own, whatever the sandbox sent under that name.
    assert sent.headers["x-end-user"] == end_user_tag(_ROOT, "user_1")
    assert "user_1" not in sent.headers["x-end-user"]


async def test_the_provider_answers_and_errors_are_relayed_as_they_are() -> None:
    store = _Store()
    provider = _Provider(400, {"error": {"message": "context_length_exceeded"}})
    response = await _send(store, provider)
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "context_length_exceeded"}}
    # The provider served it, so it counts.
    assert store.used == 1 and store.refunds == 0


async def test_a_cookie_one_answer_sets_goes_out_with_no_later_request() -> None:
    """Every owner's requests share one client, which keeps nothing between them."""
    store = _Store()
    provider = _Provider(headers={"set-cookie": "session=first-caller; Path=/"})
    async with _upstream(provider.handle) as upstream:
        first = await _send(store, provider, upstream=upstream)
        second = await _send(store, provider, upstream=upstream)
    assert first.status_code == second.status_code == 200
    assert [request.headers.get("cookie") for request in provider.requests] == [None, None]


# -- who may use it --------------------------------------------------------------


@pytest.mark.parametrize(
    "credential",
    ["sk-an-owners-own-key", "not-a-token", "amd.forged.signature"],
)
async def test_anything_but_a_placeholder_is_refused_and_never_passed_on(credential) -> None:
    store, provider = _Store(), _Provider()
    response = await _send(store, provider, bearer=credential)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credential"
    assert provider.requests == [] and store.used == 0


async def test_no_credential_is_refused() -> None:
    provider = _Provider()
    response = await _send(_Store(), provider, bearer=None)
    assert response.status_code == 401 and provider.requests == []


@pytest.mark.parametrize("mint", [mint_residency_mcp_token, mint_residency_github_token])
async def test_another_relays_credential_buys_nothing_here(mint) -> None:
    provider = _Provider()
    other = mint(thread_id="athr_1", residency_generation=2)
    response = await _send(_Store(), provider, bearer=other)
    assert response.status_code == 401 and provider.requests == []


async def test_a_stopped_turn_or_a_rotated_residency_buys_nothing() -> None:
    provider = _Provider()
    stopped = await _send(_Store(running=False), provider)
    rotated = await _send(_Store(), provider, bearer=_placeholder(generation=3))
    for response in (stopped, rotated):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credential"
    assert provider.requests == []


async def test_a_turn_on_the_owners_own_connection_cannot_borrow_the_deployments_key() -> None:
    store, provider = _Store(), _Provider()
    store.on_other_connection = True
    response = await _send(store, provider)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "provided_unavailable"
    assert provider.requests == []


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"deleted_at": datetime(2026, 11, 12, tzinfo=UTC)},
        # Moved to another Base URL since this member's process started.
        {"credential_revision": 4},
    ],
)
async def test_a_connection_changed_under_a_running_process_stops_serving_it(change) -> None:
    store, provider = _Store(), _Provider()
    assert store.connection is not None
    store.connection.update(change)
    response = await _send(store, provider)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "provided_unavailable"
    assert provider.requests == [] and store.used == 0


async def test_a_residency_never_bound_to_the_connection_is_refused() -> None:
    store, provider = _Store(), _Provider()
    store.bound_revision = None
    response = await _send(store, provider)
    assert response.status_code == 403 and provider.requests == []


# -- what may be asked -----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/responses/resp_1"),
        ("POST", "/v1/embeddings"),
        # Dot segments are refused too; ``test_operator_models_domain`` has
        # them, since this client normalizes one away before sending it.
        ("POST", "/v1/chat/completions/"),
        ("POST", "/v1/chat%2Fcompletions"),
        ("POST", "/v2/chat/completions"),
        ("GET", "/healthz"),
        ("GET", "/v1/agent/jobs"),
    ],
)
async def test_only_the_listed_paths_are_forwarded(method, path) -> None:
    store, provider = _Store(), _Provider()
    response = await _send(store, provider, method, path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert provider.requests == [] and store.used == 0


async def test_the_destination_is_the_connections_whatever_the_request_says() -> None:
    store, provider = _Store(), _Provider()
    await _send(store, provider, headers={"Host": "evil.example", "X-Forwarded-Host": "evil"})
    assert provider.requests[0].url.host == "provider.example"
    assert provider.requests[0].headers["host"] == "provider.example"


@pytest.mark.parametrize("model", ["model-9", "MODEL-1"])
async def test_a_model_the_connection_does_not_list_is_refused(model) -> None:
    store, provider = _Store(), _Provider()
    response = await _send(store, provider, json={"model": model, "messages": []})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"
    assert provider.requests == [] and store.used == 0


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"messages": []}', b'{"model": 7}'])
async def test_an_inference_request_must_name_its_model(body) -> None:
    store, provider = _Store(), _Provider()
    response = await _send(
        store, provider, content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert provider.requests == []


async def test_a_body_too_large_is_refused_before_it_is_read(monkeypatch) -> None:
    monkeypatch.setattr(model_relay, "MAX_BODY_BYTES", 64)
    store, provider = _Store(), _Provider()
    response = await _send(store, provider, content=b"x" * 65)
    assert response.status_code == 413 and provider.requests == []


# -- allowances ------------------------------------------------------------------


async def test_a_model_withheld_from_the_owner_is_refused() -> None:
    store, provider = _Store(limit=0), _Provider()
    response = await _send(store, provider)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"
    assert provider.requests == []


async def test_a_spent_allowance_is_a_403_a_client_does_not_retry() -> None:
    store, provider = _Store(limit=2, used=2), _Provider()
    response = await _send(store, provider)
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "allowance_exhausted" and "00:00 UTC" in error["message"]
    assert "retry-after" not in response.headers
    assert provider.requests == [] and store.used == 2


async def test_the_last_request_of_the_day_is_served_and_the_next_is_not() -> None:
    store, provider = _Store(limit=1), _Provider()
    assert (await _send(store, provider)).status_code == 200
    assert (await _send(store, provider)).status_code == 403
    assert len(provider.requests) == 1 and store.used == 1


async def test_a_refusal_is_shaped_for_the_api_it_was_sent_to() -> None:
    provider = _Provider()
    anthropic = await _send(_Store(limit=1, used=1), provider, path="/v1/messages")
    assert anthropic.status_code == 403
    assert anthropic.json() == {
        "type": "error",
        "error": {
            "type": "permission_error",
            "message": anthropic.json()["error"]["message"],
            "code": "allowance_exhausted",
        },
    }
    openai = await _send(_Store(limit=1, used=1), provider)
    assert set(openai.json()["error"]) == {"message", "type", "param", "code"}


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
async def test_a_request_the_provider_did_not_serve_is_taken_back(status) -> None:
    store, provider = _Store(), _Provider(status, {"error": "busy"})
    response = await _send(store, provider)
    # Relayed as it is: the CLI decides its own retry from it.
    assert response.status_code == status and response.json() == {"error": "busy"}
    assert store.used == 0 and store.refunds == 1


async def test_an_unreachable_provider_is_a_502_and_is_taken_back() -> None:
    store, provider = _Store(), _Provider(fail=True)
    response = await _send(store, provider)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unreachable"
    assert store.used == 0 and store.refunds == 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
async def test_a_provider_that_never_answers_is_a_502_and_is_taken_back(error) -> None:
    store, slots = _Store(), model_relay.Slots()

    async def handle(request: httpx.Request) -> httpx.Response:
        raise error("no answer", request=request)

    upstream = _upstream(handle)
    try:
        response = await _send(store, _Provider(), slots=slots, upstream=upstream)
    finally:
        await upstream.aclose()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unreachable"
    assert store.used == 0 and store.refunds == 1
    assert _slots_held(slots) == 0


async def test_a_failure_of_the_relays_own_before_sending_is_taken_back() -> None:
    """The owner is not out a request the provider never saw."""
    store, provider, slots = _Store(), _Provider(), model_relay.Slots()

    async def broken(_base_url: str) -> None:
        raise RuntimeError("the guard itself failed")

    with pytest.raises(RuntimeError, match="the guard itself failed"):
        await _send(store, provider, slots=slots, guard=broken)
    assert provider.requests == []
    assert store.used == 0 and store.refunds == 1
    assert _slots_held(slots) == 0


async def test_a_failure_after_the_provider_had_the_request_stays_counted() -> None:
    """The provider may have served and billed it; only the slots come back."""
    store, slots = _Store(), model_relay.Slots()
    had: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        had.append(request)
        raise RuntimeError("lost after sending")

    upstream = _upstream(handle)
    try:
        with pytest.raises(RuntimeError, match="lost after sending"):
            await _send(store, _Provider(), slots=slots, upstream=upstream)
    finally:
        await upstream.aclose()
    assert len(had) == 1
    assert store.used == 1 and store.refunds == 0
    assert _slots_held(slots) == 0


async def test_a_base_url_that_stopped_resolving_publicly_is_never_dialled() -> None:
    async def blocked(_base_url: str) -> None:
        raise CheckStop("blocked_address")

    store, provider = _Store(), _Provider()
    response = await _send(store, provider, guard=blocked)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unreachable"
    assert provider.requests == [] and store.used == 0


async def test_the_provider_refusing_the_deployments_key_is_not_the_clis_401() -> None:
    store, provider = _Store(), _Provider(401, {"error": {"message": f"bad key {_KEY}"}})
    response = await _send(store, provider)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_refused_credential"
    # Nothing the provider said about the key is passed on.
    assert _KEY not in response.text
    assert store.key_refused == [True]
    assert store.used == 0 and store.refunds == 1


async def test_a_key_that_works_again_clears_the_warning() -> None:
    store, provider = _Store(), _Provider()
    assert store.connection is not None
    store.connection["key_refused_at"] = datetime(2026, 11, 12, tzinfo=UTC)
    assert (await _send(store, provider)).status_code == 200
    assert store.key_refused == [False]


# -- requests in flight ----------------------------------------------------------


async def test_an_owner_at_their_concurrency_cap_is_asked_to_retry() -> None:
    store, provider, slots = _Store(), _Provider(), model_relay.Slots()
    assert store.connection is not None
    store.connection["user_concurrency"] = 1
    user = ("user", _CONNECTION, "user_1")
    assert slots.take([(user, 1)])
    response = await _send(store, provider, slots=slots)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["code"] == "concurrency_limited"
    assert provider.requests == [] and store.used == 0
    # The refused request took nothing.
    assert slots.held(user) == 1


async def test_a_model_at_its_deployment_wide_cap_is_asked_to_retry() -> None:
    store, provider, slots = _Store(), _Provider(), model_relay.Slots()
    store.global_concurrency = 2
    model = ("model", _CONNECTION, "model-1")
    assert slots.take([(model, 2)]) and slots.take([(model, 2)])
    response = await _send(store, provider, slots=slots)
    assert response.status_code == 429 and provider.requests == []
    # Another model of the same connection is not held back by it.
    other = await _send(store, provider, slots=slots, json={"model": "model-2", "messages": []})
    assert other.status_code == 200


@pytest.mark.parametrize(
    "provider",
    [_Provider(), _Provider(500), _Provider(401), _Provider(fail=True)],
    ids=["served", "provider-error", "key-refused", "unreachable"],
)
async def test_every_ending_gives_its_slots_back(provider) -> None:
    store, slots = _Store(), model_relay.Slots()
    await _send(store, provider, slots=slots)
    assert slots.held(("user", _CONNECTION, "user_1")) == 0
    assert slots.held(("model", _CONNECTION, "model-1")) == 0


async def test_a_caller_that_leaves_before_the_answer_ends_its_request() -> None:
    """A stopped turn's request is not waited on, at the provider or in the caps."""
    store, slots = _Store(), model_relay.Slots()
    reached, abandoned = asyncio.Event(), asyncio.Event()

    async def silent(request: httpx.Request) -> httpx.Response:
        reached.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            abandoned.set()
            raise
        raise AssertionError("unreachable")

    async with _upstream(silent) as upstream:
        caller = _Caller(_app(store, upstream, slots=slots))
        await asyncio.wait_for(reached.wait(), 5)
        assert _slots_held(slots) == 2 and not caller.call.done()

        caller.left.set()
        assert await caller.ended() is None
    assert abandoned.is_set()
    assert _slots_held(slots) == 0
    # The provider had the request, so it stays counted.
    assert store.used == 1 and store.refunds == 0


async def test_a_caller_that_stays_is_answered_however_long_the_provider_takes() -> None:
    store, slots = _Store(), model_relay.Slots()
    reached, answer = asyncio.Event(), asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        reached.set()
        await answer.wait()
        return httpx.Response(200, stream=httpx.ByteStream(b'{"id": "answer"}'))

    async with _upstream(slow) as upstream:
        caller = _Caller(_app(store, upstream, slots=slots))
        await asyncio.wait_for(reached.wait(), 5)
        answer.set()
        assert await caller.ended() is None
    assert caller.sent[0]["status"] == 200
    assert json.loads(b"".join(m.get("body", b"") for m in caller.sent[1:])) == {"id": "answer"}
    assert _slots_held(slots) == 0


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_a_caller_that_leaves_mid_answer_closes_the_providers_stream(spec_version) -> None:
    store, slots = _Store(), model_relay.Slots()
    stream = _OpenStream(then=b"data: more\n\n")

    async def streaming(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with _upstream(streaming) as upstream:
        caller = _Caller(_app(store, upstream, slots=slots), spec_version=spec_version)
        await asyncio.wait_for(caller.heard.wait(), 5)
        assert _slots_held(slots) == 2 and not stream.closed

        caller.left.set()
        if spec_version == "2.4":
            # Nothing tells this server; its next send is what fails.
            stream.go.set()
        await caller.ended()
    assert stream.closed
    assert _slots_held(slots) == 0


async def test_an_answer_the_provider_breaks_off_is_closed_and_gives_its_slots_back() -> None:
    store, slots = _Store(), model_relay.Slots()
    stream = _OpenStream(then=httpx.ReadError("connection reset"))

    async def streaming(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with _upstream(streaming) as upstream:
        caller = _Caller(_app(store, upstream, slots=slots))
        await asyncio.wait_for(caller.heard.wait(), 5)
        stream.go.set()
        assert isinstance(await caller.ended(), httpx.ReadError)
    assert stream.closed
    assert _slots_held(slots) == 0


async def test_a_spent_allowance_found_at_the_count_gives_its_slots_back() -> None:
    """Two requests pass the first read together; the count lets only one through."""
    store, provider, slots = _Store(limit=1), _Provider(), model_relay.Slots()
    real = store.resolve_model_relay_caller

    async def stale(**kwargs):
        caller = await real(**kwargs)
        caller["allowance"] = Allowance(limit=1, used=0)
        return caller

    store.resolve_model_relay_caller = stale  # type: ignore[method-assign]
    store.used = 1
    response = await _send(store, provider, slots=slots)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "allowance_exhausted"
    assert slots.held(("user", _CONNECTION, "user_1")) == 0 and provider.requests == []


# -- what is recorded ------------------------------------------------------------


async def test_a_request_is_logged_by_who_and_what_never_by_content(caplog) -> None:
    store, provider = _Store(), _Provider()
    secret_prompt = "the-owners-private-prompt"
    with caplog.at_level(logging.INFO, logger="cloud_agent.model_relay"):
        await _send(
            store, provider, json={"model": "model-1", "messages": [{"content": secret_prompt}]}
        )
    [record] = [r for r in caplog.records if r.getMessage() == "agent_model_relay_request"]
    assert (record.job_id, record.user_id, record.model, record.status) == (
        "ajob_1",
        "user_1",
        "model-1",
        200,
    )
    logged = json.dumps({k: str(v) for k, v in record.__dict__.items()})
    for never in (secret_prompt, _KEY, _placeholder()):
        assert never not in logged


async def test_without_a_database_the_relay_refuses() -> None:
    provider = _Provider()
    response = await _send(None, provider)
    assert response.status_code == 503 and provider.requests == []
