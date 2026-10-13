"""Cookie-authenticated writes from another site are refused before any route.

``SameSite=Lax`` keeps the session cookie off a cross-*site* POST, not off a
same-*site* one, and the web app shares its site with the gateway. What a
browser says about where a request came from (``Sec-Fetch-Site``, ``Origin``)
decides; a request with no session cookie, or with no browser behind it, is
left alone so hosts, workers and scripts are unaffected.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent import identity, settings
from cloud_agent.api._cross_site import CrossSiteWriteGuard
from cloud_agent.app import create_app, create_mcp_app

pytestmark = pytest.mark.asyncio

_WEB = "https://agents.example.org/agents"


@pytest.fixture(autouse=True)
def _deployment(monkeypatch):
    settings.reset_settings()
    monkeypatch.setenv("AGENT_WEB_BASE_URL", _WEB)
    monkeypatch.delenv("AGENT_PGADMIN_PUBLIC_URL", raising=False)
    yield
    settings.reset_settings()


@pytest.fixture()
async def client():
    app = FastAPI()
    app.add_middleware(CrossSiteWriteGuard)

    @app.api_route("/write", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def write() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://agents.example.org"
    ) as async_client:
        yield async_client


def _session(name: str = identity.SESSION_COOKIE) -> dict[str, str]:
    return {"Cookie": f"{name}=the-session"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
async def test_a_write_from_another_site_with_the_session_is_refused(client, method, site):
    response = await client.request(
        method, "/write", headers={**_session(), "Sec-Fetch-Site": site}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "cross_site_request"
    assert response.headers["cache-control"] == "no-store"


async def test_the_pre_rename_cookie_name_is_guarded_too(client):
    response = await client.post(
        "/write",
        headers={**_session(identity.LEGACY_SESSION_COOKIE), "Sec-Fetch-Site": "same-site"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
async def test_a_write_from_this_origin_passes(client, site):
    response = await client.post("/write", headers={**_session(), "Sec-Fetch-Site": site})
    assert response.status_code == 200


async def test_the_configured_web_origin_passes_from_a_sibling_host(client):
    """A deployment whose web app and API are separate hosts of one site."""
    response = await client.post(
        "/write",
        headers={
            **_session(),
            "Sec-Fetch-Site": "same-site",
            "Origin": "https://agents.example.org",
        },
    )
    assert response.status_code == 200


async def test_the_pgadmin_public_origin_passes(client, monkeypatch):
    monkeypatch.setenv(
        "AGENT_PGADMIN_PUBLIC_URL", "https://db.example.org:8443/api/admin/agent/pgadmin"
    )
    response = await client.post(
        "/write",
        headers={
            **_session(),
            "Sec-Fetch-Site": "same-site",
            "Origin": "https://db.example.org:8443",
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("origin", "status"),
    [
        ("https://agents.example.org", 200),
        ("https://evil.example.org", 403),
        ("https://agents.example.org.evil.example", 403),
        ("null", 403),
    ],
)
async def test_without_fetch_metadata_the_origin_decides(client, monkeypatch, origin, status):
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://web.example.org")
    response = await client.post("/write", headers={**_session(), "Origin": origin})
    assert response.status_code == status


@pytest.mark.parametrize(
    ("secure", "origin", "status"),
    [
        (None, "http://agents.example.org", 403),
        (None, "https://agents.example.org:443", 200),
        (None, "https://agents.example.org:8443", 403),
        ("false", "http://agents.example.org", 200),
        ("false", "https://agents.example.org", 200),
    ],
)
async def test_without_fetch_metadata_the_scheme_and_port_count(
    client, monkeypatch, secure, origin, status
):
    """An older browser treats http and https of one host as one site."""
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://web.example.org")
    if secure is None:
        monkeypatch.delenv("AGENT_SESSION_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("AGENT_SESSION_COOKIE_SECURE", secure)
    response = await client.post("/write", headers={**_session(), "Origin": origin})
    assert response.status_code == status


async def test_a_request_with_no_browser_behind_it_passes(client):
    """Neither header: a script, a test client or a CLI holding the cookie."""
    assert (await client.post("/write", headers=_session())).status_code == 200


async def test_a_bearer_caller_is_never_checked(client):
    """Hosts, workers and the on-call relay send no cookie; nothing ambient to abuse."""
    response = await client.post(
        "/write",
        headers={"Authorization": "Bearer aht.host.credential", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "OPTIONS"])
async def test_safe_methods_pass(client, method):
    response = await client.request(
        method, "/write", headers={**_session(), "Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 200


async def test_the_main_listener_installs_the_guard_and_the_relay_does_not():
    assert any(entry.cls is CrossSiteWriteGuard for entry in create_app().user_middleware)
    assert not any(entry.cls is CrossSiteWriteGuard for entry in create_mcp_app().user_middleware)


async def test_the_main_listener_refuses_a_cross_site_sign_out():
    """End to end on a real route that needs no database or gateway."""
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="https://agents.example.org"
    ) as main:
        refused = await main.post(
            "/v1/session/logout", headers={**_session(), "Sec-Fetch-Site": "same-site"}
        )
        accepted = await main.post(
            "/v1/session/logout", headers={**_session(), "Sec-Fetch-Site": "same-origin"}
        )
    assert refused.status_code == 403
    assert "set-cookie" not in refused.headers
    assert accepted.status_code == 200
