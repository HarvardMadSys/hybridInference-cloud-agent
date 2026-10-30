"""Connections are reused across calls, and released when the process ends.

Every one of these paths used to build an `httpx.AsyncClient` per call, so a
request paid a TCP connect — and a TLS handshake, where the peer is behind TLS
— before it did anything. On the interactive paths that is not occasional:
admission runs on every owner route including terminal input, repository
entitlement runs on every file read, and both were reconnecting each time.

`app.py` already carried a comment saying the grant client shared a pool. It
did not; the seam existed and nothing was passed to it. That is the reason
these are tests and not a comment: a claim about pooling that only a human
reads is a claim nothing checks.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from cloud_agent import sandbox_manager_routing
from cloud_agent.app import lifespan
from cloud_agent.github_app import AppConfig, GitHubAppCredentials

_KEY = "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----"


def _credentials(**kwargs) -> GitHubAppCredentials:
    return GitHubAppCredentials(AppConfig(app_id="1", private_key=_KEY), **kwargs)


async def test_the_lifespan_gives_both_gateway_clients_one_pool(monkeypatch) -> None:
    """The sharing `app.py` describes in a comment, now actual."""
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=AsyncMock()))
    # This test's pool is a socket-ownership sentinel, not a settings database.
    # Initialization/import semantics have separate repository-backed tests.
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace", AsyncMock(return_value=None)
    )
    app = FastAPI()

    async with lifespan(app, background=False):
        shared = app.state.http
        assert app.state.owners._client is shared
        assert app.state.grants._client is shared
        assert not shared.is_closed

    assert shared.is_closed, "the pool outlived the application that owned it"


async def test_the_shared_pool_keeps_no_cookies(monkeypatch) -> None:
    """One owner's answer must not set a cookie that rides on the next owner's request.

    The pool carries owners' grants to the gateway and repositories' tokens to
    GitHub. Each per-call client it replaced had a jar of its own.
    """
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    monkeypatch.setattr(asyncpg, "create_pool", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace", AsyncMock(return_value=None)
    )
    app = FastAPI()
    url = "https://gateway.example/v1/chat/completions"

    async with lifespan(app, background=False):
        shared = app.state.http
        # What the client does with every answer it receives.
        shared.cookies.extract_cookies(
            httpx.Response(
                200,
                headers={"set-cookie": "session=owner-a; Path=/"},
                request=httpx.Request("POST", url),
            )
        )
        follow_up = shared.build_request("POST", url)

    assert not shared.cookies
    assert "cookie" not in follow_up.headers


async def test_the_lifespan_opens_its_database_pool_without_jit(monkeypatch) -> None:
    """Every database session the service opens leaves PostgreSQL's JIT compiler off.

    The planner prices a correlated subquery once per outer row, so a statement
    that reads a few hundred indexed rows can be estimated far past
    ``jit_above_cost``. At a thousand hosts the admin host list then spent
    1.9 s compiling a statement that ran in 85 ms, and every reaper pass spent
    170 ms compiling a host-removal check that ran in under one.
    """
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://test-only")
    monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_ISSUER", raising=False)
    create_pool = AsyncMock(return_value=AsyncMock())
    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    monkeypatch.setattr("cloud_agent.settings.initialize_settings", AsyncMock())
    monkeypatch.setattr(
        "cloud_agent.identity_namespace.ensure_gateway_namespace", AsyncMock(return_value=None)
    )

    async with lifespan(FastAPI(), background=False):
        pass

    settings = create_pool.await_args.kwargs.get("server_settings") or {}
    assert settings.get("jit") == "off", f"the pool opens sessions with {settings or 'defaults'}"


async def test_the_github_app_keeps_one_client_for_its_lifetime() -> None:
    """Entitlement resolves through this object on every workspace request."""
    credentials = _credentials()

    async with credentials._http() as first:
        pass
    async with credentials._http() as second:
        pass

    assert first is second
    assert not first.is_closed

    await credentials.aclose()
    assert first.is_closed


async def test_a_borrowed_github_client_survives_aclose() -> None:
    """A client passed in belongs to the caller; closing it is not ours to do."""
    borrowed = httpx.AsyncClient()
    credentials = _credentials(client=borrowed)

    async with credentials._http() as client:
        assert client is borrowed

    await credentials.aclose()
    assert not borrowed.is_closed
    await borrowed.aclose()


@pytest.fixture
async def sandbox_manager_pools(monkeypatch):
    """Sandbox Manager pools are process state; hand each test a clean, closed-after set."""
    monkeypatch.setenv(sandbox_manager_routing.ENV_SANDBOX_MANAGER_TOKEN, "sandbox-manager-token")
    await sandbox_manager_routing.close_sandbox_manager_clients()
    yield
    await sandbox_manager_routing.close_sandbox_manager_clients()


def _row(host: str, url: str) -> dict[str, str]:
    """The two fields of a host row this path reads."""
    return {"host": host, "sandbox_manager_url": url}


async def test_one_pool_per_enrolled_host(sandbox_manager_pools) -> None:
    """A client is built per request, so the pool cannot live on the client."""
    first = sandbox_manager_routing._client_for(_row("host-a", "http://a:9000"))
    second = sandbox_manager_routing._client_for(_row("host-a", "http://a:9000"))
    other = sandbox_manager_routing._client_for(_row("host-b", "http://b:9000"))

    assert first is not second, "these are still built per request"
    assert set(sandbox_manager_routing._POOLS) == {"host-a", "host-b"}
    assert first._client is sandbox_manager_routing._POOLS["host-a"][1]
    assert second._client is first._client
    assert other._client is not first._client


async def test_a_host_that_moves_replaces_and_closes_its_pool(sandbox_manager_pools) -> None:
    """Keyed by host, not by address — a heartbeat overwrites the address.

    `host_pool.py` upserts `sandbox_manager_url = EXCLUDED.sandbox_manager_url`, so a rebuilt
    machine reports a new one. Keyed by URL, the old pool would sit in the
    dictionary holding sockets until the process ended, and with
    `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` unset the key would be a value the far
    side chooses.
    """
    sandbox_manager_routing._client_for(_row("host-a", "http://old:9000"))
    stale = sandbox_manager_routing._POOLS["host-a"][1]

    sandbox_manager_routing._client_for(_row("host-a", "http://new:9000"))

    # The close is scheduled rather than awaited, so that the factory on the
    # request path can stay synchronous. It still has to actually happen.
    assert sandbox_manager_routing._RETIRING, "the replaced pool was dropped, not retired"
    await asyncio.gather(*sandbox_manager_routing._RETIRING)
    assert stale.is_closed, "the pool for the old address was left open"
    assert not sandbox_manager_routing._RETIRING, "a finished close must not be held forever"
    assert list(sandbox_manager_routing._POOLS) == ["host-a"], "one host, one entry"
    assert sandbox_manager_routing._POOLS["host-a"][0] == "http://new:9000"


async def test_closing_the_pools_lets_the_next_request_open_a_new_one(
    sandbox_manager_pools,
) -> None:
    """Shutdown must release them, and must not leave a closed one behind."""
    before = sandbox_manager_routing._client_for(_row("host-a", "http://a:9000"))._client

    await sandbox_manager_routing.close_sandbox_manager_clients()
    assert before.is_closed
    assert not sandbox_manager_routing._POOLS

    after = sandbox_manager_routing._client_for(_row("host-a", "http://a:9000"))._client
    assert after is not before
    assert not after.is_closed


async def test_a_row_with_no_host_identity_gets_no_pool(sandbox_manager_pools) -> None:
    """Nothing to bound a dictionary by, so this row keeps its old behaviour."""
    client = sandbox_manager_routing._client_for({"sandbox_manager_url": "http://a:9000"})

    assert client is not None
    assert client._client is None, "an unbounded key must not become a pool key"
    assert not sandbox_manager_routing._POOLS


async def test_a_host_with_no_sandbox_manager_url_still_gets_no_client(
    sandbox_manager_pools,
) -> None:
    """Pooling must not turn an unreachable host row into a reachable one."""
    assert sandbox_manager_routing._client_for(_row("host-a", "")) is None
    assert sandbox_manager_routing._client_for({}) is None
    assert not sandbox_manager_routing._POOLS


async def test_a_stored_metadata_address_gets_no_client(sandbox_manager_pools, monkeypatch) -> None:
    """A row written before the refusal existed is refused when it is used.

    The address is checked where the token is about to be lent, so a sandbox
    manager URL already stored as the metadata service never receives the
    pool's credential, with or without an allowlist.
    """
    monkeypatch.delenv(sandbox_manager_routing.ENV_SANDBOX_MANAGER_HOSTS, raising=False)

    assert sandbox_manager_routing._client_for(_row("host-a", "http://169.254.169.254:80")) is None
    assert not sandbox_manager_routing._POOLS


async def test_removed_hosts_release_pool_and_probe_entries(sandbox_manager_pools) -> None:
    pools = []
    for index in range(50):
        name = f"host-{index}"
        client = sandbox_manager_routing._client_for(_row(name, "http://manager:9000"))
        pools.append(client._client)
        sandbox_manager_routing._PROBE_CACHE[name] = (float("inf"), "http://manager:9000")

    sandbox_manager_routing.retire_sandbox_manager_hosts(f"host-{index}" for index in range(50))
    await asyncio.gather(*sandbox_manager_routing._RETIRING)

    assert not sandbox_manager_routing._POOLS
    assert not sandbox_manager_routing._PROBE_CACHE
    assert not sandbox_manager_routing._POOL_LEASES
    assert all(pool.is_closed for pool in pools)


async def test_retirement_drains_a_borrower_and_reregistration_gets_a_new_pool(
    sandbox_manager_pools,
) -> None:
    row = _row("host-a", "http://manager:9000")
    old = sandbox_manager_routing._client_for(row)
    async with old._http(3) as borrowed:
        sandbox_manager_routing.retire_sandbox_manager_hosts(["host-a"])
        await asyncio.sleep(0)
        assert not borrowed.is_closed
        replacement = sandbox_manager_routing._client_for(row)
        assert replacement._client is not borrowed
        with pytest.raises(sandbox_manager_routing.SandboxManagerError, match="retired"):
            async with old._http(3):
                pytest.fail("a retired pool accepted another borrower")

    await asyncio.gather(*sandbox_manager_routing._RETIRING)
    assert borrowed.is_closed
    assert not replacement._client.is_closed


async def test_idle_sweep_expires_missed_removals_but_keeps_active_requests(
    sandbox_manager_pools,
    monkeypatch,
) -> None:
    idle = sandbox_manager_routing._client_for(_row("idle", "http://idle:9000"))
    active = sandbox_manager_routing._client_for(_row("active", "http://active:9000"))
    now = sandbox_manager_routing.time.monotonic()
    sandbox_manager_routing._PROBE_CACHE["orphan"] = (now + 60, "http://gone:9000")
    async with active._http(3):
        monkeypatch.setattr(sandbox_manager_routing.time, "monotonic", lambda: now + 601)
        sandbox_manager_routing.sweep_sandbox_manager_clients()
        await asyncio.gather(*sandbox_manager_routing._RETIRING)
        assert idle._client.is_closed
        assert not active._client.is_closed
        assert set(sandbox_manager_routing._POOLS) == {"active"}
        assert not sandbox_manager_routing._PROBE_CACHE


@pytest.mark.parametrize(
    "outcome, retired", [("ok", True), ("draining", False), ("moving_out", False)]
)
async def test_host_removal_retires_only_after_completion(monkeypatch, outcome, retired) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cloud_agent.application.host_commands import HostCommands

    retirement = Mock()
    monkeypatch.setattr(
        "cloud_agent.application.host_commands.retire_sandbox_manager_hosts", retirement
    )
    await HostCommands(SimpleNamespace(remove_host=AsyncMock(return_value=outcome))).remove(
        host="host-a"
    )
    assert retirement.call_args_list == ([((["host-a"],), {})] if retired else [])


@pytest.mark.parametrize(
    "outcome, retired",
    [
        ("deleted", True),
        ("active", False),
        ("fenced", False),
        ("unknown", False),
        ("retained_groups_present", False),
    ],
)
async def test_host_forget_retires_only_after_deletion(monkeypatch, outcome, retired) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cloud_agent.application.host_commands import HostCommandError, HostCommands

    retirement = Mock()
    monkeypatch.setattr(
        "cloud_agent.application.host_commands.retire_sandbox_manager_hosts", retirement
    )
    command = HostCommands(SimpleNamespace(forget_runner_host=AsyncMock(return_value=outcome)))
    if outcome == "deleted":
        await command.forget("host-a")
    else:
        with pytest.raises(HostCommandError):
            await command.forget("host-a")
    assert retirement.call_args_list == ([((["host-a"],), {})] if retired else [])


async def test_reaper_retires_completed_host_removals(monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from cloud_agent.workers.attempts import _reap_offline_hosts

    retirement = Mock()
    sweep = Mock()
    monkeypatch.setattr(sandbox_manager_routing, "retire_sandbox_manager_hosts", retirement)
    monkeypatch.setattr(sandbox_manager_routing, "sweep_sandbox_manager_clients", sweep)
    await _reap_offline_hosts(
        SimpleNamespace(
            reap_offline_hosts=AsyncMock(return_value=[]),
            finish_host_removals=AsyncMock(return_value=["gone"]),
        )
    )
    retirement.assert_called_once_with(["gone"])
    sweep.assert_called_once_with()
