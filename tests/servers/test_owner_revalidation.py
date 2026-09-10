"""Every browser read/write requires a current, explicit Gateway permission."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity
from cloud_agent.api import admin_hosts as admin_hosts_router, routes as agent_jobs_router
from cloud_agent.api.deps import get_gateway_client, verify_admin_access
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio
_OWNER = "user_1"


class _AccessGateway:
    def __init__(
        self,
        permissions: list[str] | None = None,
        error: int | None = None,
        error_type: str | None = None,
    ):
        self.permissions = permissions if permissions is not None else ["agent.use"]
        self.error = error
        self.error_type = error_type
        self.asked: list[str] = []

    async def agent_access(self, user_id: str) -> dict[str, Any]:
        self.asked.append(user_id)
        if self.error is not None:
            raise GatewayError("unavailable", status_code=self.error, error_type=self.error_type)
        return {
            "user_id": user_id,
            "allowed": "agent.use" in self.permissions,
            "permissions": self.permissions,
        }


def _app(gateway: _AccessGateway, role: str = "admin") -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    @app.post("/probe")
    async def owner(user: dict[str, Any] = Depends(agent_jobs_router.require_agent_owner)):
        return user

    @app.get("/admin/probe")
    @app.post("/admin/probe")
    async def admin(actor: str = Depends(verify_admin_access)):
        return {"actor": actor}

    app.dependency_overrides[agent_jobs_router.authenticate_agent_owner] = lambda: {
        "user_id": _OWNER,
        "role": role,
        "authenticated": True,
    }
    app.dependency_overrides[get_gateway_client] = lambda: gateway
    return app


@pytest.mark.parametrize("role", ["free", "pro", "internal", "admin", "custom"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_gateway_allowed_owners_work_independently_of_role(role, method):
    gateway = _AccessGateway()
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway, role)), base_url="https://test"
    ) as client:
        response = await client.request(method, "/probe")
    assert response.status_code == 200
    assert response.json()["agent_access"] == {"allowed": True, "permissions": ["agent.use"]}
    assert response.json()["role"] == role
    assert response.json()["is_admin"] is False
    assert gateway.asked == [_OWNER]


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_an_old_admin_session_cannot_bypass_a_denial(method):
    gateway = _AccessGateway([])
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        response = await client.request(method, "/probe")
    assert response.status_code == 403
    assert gateway.asked == [_OWNER]


@pytest.mark.parametrize(
    "status, error_type, expected",
    [
        (401, None, 503),
        (403, None, 503),
        (403, "browser_check", 503),
        (403, "subject_unavailable", 403),
        (404, None, 503),
        (503, None, 503),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_unavailable_policy_never_falls_back_to_session_role(
    status, error_type, expected, method
):
    gateway = _AccessGateway(error=status, error_type=error_type)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        response = await client.request(method, "/probe")
    assert response.status_code == expected


async def test_permission_is_rechecked_after_every_successful_read():
    gateway = _AccessGateway()
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        assert (await client.get("/probe")).status_code == 200
        gateway.permissions = []
        assert (await client.get("/probe")).status_code == 403
        assert (await client.post("/probe")).status_code == 403
    assert gateway.asked == [_OWNER, _OWNER, _OWNER]


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
#: verify_admin_access is also a browser gate: its session path requires a
#: fresh agent.admin decision, even though ADMIN_TOKEN needs no user lookup.
MACHINE_GATES = frozenset(
    {
        "_worker_claims",
        "verify_claiming_host",
        "verify_dispatcher_access",
    }
)

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


@pytest.fixture()
def admin_env(monkeypatch):
    monkeypatch.setenv(identity.ENV_SESSION_SECRET, "admin-test-secret-long-enough-for-hs256")
    monkeypatch.setenv("ADMIN_TOKEN", "the-machine-token")


@pytest.mark.parametrize("role", ["free", "admin"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "permissions, expected", [([], 403), (["agent.use"], 403), (["agent.use", "agent.admin"], 200)]
)
async def test_admin_permission_is_scoped_separately(
    admin_env, role, method, permissions, expected
):
    gateway = _AccessGateway(permissions)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        client.cookies.set(identity.SESSION_COOKIE, identity.mint_session(user_id=_OWNER))
        response = await client.request(method, "/admin/probe")
    assert response.status_code == expected
    assert gateway.asked == [_OWNER]
    if expected == 200:
        assert response.json()["actor"] == f"admin:{_OWNER}"


async def test_the_machine_token_stays_independent_of_gateway_permissions(admin_env):
    gateway = _AccessGateway(error=503)
    async with AsyncClient(
        transport=ASGITransport(app=_app(gateway)), base_url="https://test"
    ) as client:
        response = await client.post(
            "/admin/probe", headers={"Authorization": "Bearer the-machine-token"}
        )
    assert response.status_code == 200
    assert response.json()["actor"] == "admin-token"
    assert gateway.asked == []


@pytest.fixture(autouse=True)
def _configured_identity_issuer(monkeypatch):
    monkeypatch.setenv("GATEWAY_ISSUER", "https://gateway.example")
