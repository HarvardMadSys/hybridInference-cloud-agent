"""Privileged actions revalidate the account against the gateway.

The session is a cache of an authorization decision the gateway made at
sign-in. Without something that re-asks, a user suspended or downgraded there
keeps this service's privileges until the cookie expires — which is what made
the seven-day session a real hole rather than a long one. Bounding it with the
TTL alone would mean a TTL of minutes, so the two halves are designed together:
a session measured in hours, and this.

**It hangs on the request method, not on a list of paths.** A list is a thing
the next route added quietly falls off; the last case here is the one that
notices if a mutating route is ever wired to a gate that does not revalidate.
Reads deliberately do not revalidate — a gateway outage should not close
everybody's open pages, and polling this surface should not amplify onto the
gateway.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import admin_hosts as admin_hosts_router, routes as agent_jobs_router
from cloud_agent.api.deps import get_gateway_client
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio

_OWNER = "user_1"


class _StatusGateway:
    """The gateway's user-status contract, answered in-process."""

    def __init__(self, record: dict[str, Any] | None = None, *, error: bool = False) -> None:
        self.record = record if record is not None else {"exists": True, "active": True}
        self.record.setdefault("role", "admin")
        self.error = error
        self.asked: list[str] = []

    async def user_status(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error:
            raise GatewayError("gateway unreachable: nope")
        return self.record


def _app(gateway: _StatusGateway) -> FastAPI:
    """Two routes that differ only by method, both behind the owner gate."""
    app = FastAPI()

    @app.get("/probe")
    async def read(user: dict[str, Any] = Depends(agent_jobs_router.require_agent_owner)):
        return {"role": user["role"]}

    @app.post("/probe")
    async def write(user: dict[str, Any] = Depends(agent_jobs_router.require_agent_owner)):
        return {"role": user["role"]}

    identity = {"user_id": _OWNER, "role": "admin", "authenticated": True}
    # Override the credential resolver, not the role gate: the gate is what is
    # under test.
    app.dependency_overrides[agent_jobs_router.authenticate_agent_owner] = lambda: identity
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


@pytest_asyncio.fixture()
async def client_for():
    async def build(gateway: _StatusGateway):
        transport = ASGITransport(app=_app(gateway))
        return AsyncClient(transport=transport, base_url="https://test")

    return build


async def test_a_write_by_a_suspended_account_is_refused(client_for):
    """The hole this closes: a valid session for an account that is not."""
    gateway = _StatusGateway({"exists": True, "active": False})
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 403
    assert gateway.asked == [_OWNER]


async def test_a_read_by_the_same_account_still_works(client_for):
    """Reads stay a pure session check, on purpose.

    Revalidating them would put every page load behind the gateway's
    availability — and make an outage close everyone's open tabs — for a
    question the session already answers well enough to *display* things.
    """
    gateway = _StatusGateway({"exists": True, "active": False})
    async with await client_for(gateway) as client:
        response = await client.get("/probe")

    assert response.status_code == 200
    assert gateway.asked == [], "a read must not cost a gateway round trip"


async def test_an_unreachable_gateway_refuses_writes(client_for):
    """Fail closed: an authority that cannot be reached granted nothing."""
    gateway = _StatusGateway(error=True)
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 503


async def test_a_downgraded_role_loses_the_surface(client_for):
    """A demotion at the gateway takes effect here without waiting for expiry."""
    gateway = _StatusGateway({"exists": True, "active": True, "role": "pro"})
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 403


async def test_the_gateways_current_role_reaches_the_route(client_for):
    """And the fresh role is what the handler sees, not the session's copy.

    A promotion that only took effect on the *next* request would be a
    surprising kind of correct; more to the point, a route that reads
    ``user["role"]`` should read the same answer this gate just checked.
    """
    gateway = _StatusGateway({"exists": True, "active": True, "role": "admin"})
    async with await client_for(gateway) as client:
        response = await client.post("/probe")

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def _gates(route: Any) -> set[str]:
    """Every dependency function name reachable from a route."""
    names: set[str] = set()
    stack = list(getattr(route.dependant, "dependencies", []))
    while stack:
        dependant = stack.pop()
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        stack.extend(getattr(dependant, "dependencies", []))
    return names


#: What a machine-to-machine route authenticates with instead. These carry
#: their own capability credential and have no browser session behind them to
#: have gone stale, so revalidating a *user* on them would be checking the
#: wrong thing. Named by the call each route actually makes, not by URL prefix:
#: a prefix exempts whatever is put under it, which is the same "the next one
#: falls off the list" failure this test exists to catch.
#: **``verify_admin_access`` is deliberately not in here**, and was, briefly.
#: It reads as a machine gate because of ``ADMIN_TOKEN``, but it is two doors:
#: the other one is a browser session whose role claim says ``admin``, which is
#: how the dashboard authenticates. Exempting it left every admin write —
#: draining a host, pinning a runtime version — trusting a role that was true
#: only whenever the session happened to be minted.
MACHINE_GATES = frozenset({"_worker_claims", "verify_dispatcher_access"})

#: Both routers a browser reaches with a session. The admin router is a second
#: mount point, not a second kind of caller.
SESSION_ROUTERS = (agent_jobs_router.router, admin_hosts_router.router)

#: Gates that re-ask the gateway before they allow a write.
REVALIDATING_GATES = frozenset({"require_agent_owner", "verify_admin_access"})


async def test_every_mutating_route_a_session_can_reach_revalidates():
    """No route gets to skip this by being added later.

    The method rule is only as good as its coverage: a mutating route wired to
    ``authenticate_agent_owner`` directly, or to nothing at all, is one that
    never re-asks.
    """
    mutating = {"POST", "PUT", "PATCH", "DELETE"}

    unguarded = []
    for router in SESSION_ROUTERS:
        for route in router.routes:
            methods = getattr(route, "methods", set()) or set()
            if not (methods & mutating):
                continue
            if _gates(route) & REVALIDATING_GATES:
                continue
            # Worker routes verify their capability token in the handler rather
            # than through a dependency, so the dependency tree cannot see it.
            try:
                body = inspect.getsource(route.endpoint)
            except (OSError, TypeError):  # pragma: no cover - defensive
                body = ""
            if any(gate in body for gate in MACHINE_GATES):
                continue
            unguarded.append(f"{sorted(methods)} {route.path}")

    assert not unguarded, (
        "these mutating routes authenticate nothing that can go stale — wire "
        f"them to a revalidating gate or to a machine credential: {unguarded}"
    )


# ── The admin door, which is two doors ────────────────────────────────────


def _admin_app(gateway: _StatusGateway) -> FastAPI:
    """A read and a write behind the admin gate, with no identity override.

    The session is real here — `verify_admin_access` verifies it itself, so
    stubbing it out would test nothing.
    """
    from cloud_agent.api.deps import verify_admin_access

    app = FastAPI()

    @app.get("/admin/probe")
    async def read(actor: str = Depends(verify_admin_access)):
        return {"actor": actor}

    @app.post("/admin/probe")
    async def write(actor: str = Depends(verify_admin_access)):
        return {"actor": actor}

    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


def _admin_session(role: str = "admin") -> str:
    """A session this service would have minted at sign-in."""
    from cloud_agent import identity

    return identity.mint_session(user_id=_OWNER, email=None, role=role)


@pytest_asyncio.fixture()
def admin_env(monkeypatch):
    from cloud_agent import identity
    from cloud_agent.api.deps import ENV_ADMIN_TOKEN

    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-test-secret-long-enough-for-hs256")
    monkeypatch.setenv(ENV_ADMIN_TOKEN, "the-machine-token")
    return None


async def test_a_demoted_admin_cannot_change_anything(admin_env):
    """The hole one door over from the owner surface.

    `require_agent_owner` re-asked; this gate did not, so an administrator the
    gateway had demoted kept the power to drain hosts and pin runtime versions
    until the session expired.
    """
    from cloud_agent import identity

    gateway = _StatusGateway({"exists": True, "active": True, "role": "internal"})
    transport = ASGITransport(app=_admin_app(gateway))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, _admin_session())
        response = await client.post("/admin/probe")

    assert response.status_code == 403
    assert gateway.asked == [_OWNER]


async def test_a_suspended_admin_cannot_change_anything(admin_env):
    """Active is checked as well as the role — a banned admin is not an admin."""
    from cloud_agent import identity

    gateway = _StatusGateway({"exists": True, "active": False, "role": "admin"})
    transport = ASGITransport(app=_admin_app(gateway))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, _admin_session())
        response = await client.post("/admin/probe")

    assert response.status_code == 403


async def test_an_admin_the_gateway_still_vouches_for_may_write(admin_env):
    """And the ordinary case keeps working, which is the other half."""
    from cloud_agent import identity

    gateway = _StatusGateway({"exists": True, "active": True, "role": "admin"})
    transport = ASGITransport(app=_admin_app(gateway))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, _admin_session())
        response = await client.post("/admin/probe")

    assert response.status_code == 200
    assert response.json()["actor"] == f"admin:{_OWNER}"


async def test_an_admin_read_is_not_re_asked(admin_env):
    """Same trade as the owner surface: an outage must not close every page."""
    from cloud_agent import identity

    gateway = _StatusGateway({"exists": True, "active": False, "role": "free"})
    transport = ASGITransport(app=_admin_app(gateway))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        client.cookies.set(identity.SESSION_COOKIE, _admin_session())
        response = await client.get("/admin/probe")

    assert response.status_code == 200
    assert gateway.asked == []


async def test_the_machine_token_is_untouched_by_any_of_this(admin_env):
    """`ADMIN_TOKEN` has no user behind it to go stale, and no gateway to ask.

    It must keep working when the gateway is unreachable — a deployment that
    authenticates machines only has no session secret and no gateway at all,
    and this door is what it uses.
    """
    gateway = _StatusGateway(error=True)
    transport = ASGITransport(app=_admin_app(gateway))
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/admin/probe", headers={"Authorization": "Bearer the-machine-token"}
        )

    assert response.status_code == 200
    assert response.json()["actor"] == "admin-token"
    assert gateway.asked == [], "the machine door must not depend on the gateway"
