"""Named administrator credentials on a real database, through verify_admin_access (WP9)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from tests.integration.storage.test_schema_upgrade import store as store

from cloud_agent.api import admin_credentials, admin_deployments
from cloud_agent.api.deps import get_gateway_client, get_storage, verify_admin_access

pytestmark = [pytest.mark.dbtest, pytest.mark.asyncio]

_ADMIN = "full-admin-token-of-at-least-thirty-two-bytes"


async def _no_audit(*args: Any, **kwargs: Any) -> None:
    return None


def _app(storage: Any, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("ADMIN_TOKEN", _ADMIN)
    monkeypatch.setattr(admin_credentials, "log_admin_action", _no_audit)
    monkeypatch.setattr(admin_deployments, "log_admin_action", _no_audit)
    app = FastAPI()
    app.state.storage = storage
    app.include_router(admin_credentials.router)
    app.include_router(admin_deployments.router)

    @app.get("/admin/whoami")
    async def whoami(admin: str = Depends(verify_admin_access)) -> dict[str, str]:
        return {"admin": admin}

    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_gateway_client] = lambda: None
    return app


async def test_a_credential_is_shown_once_scoped_and_revoked_on_the_next_request(
    store, monkeypatch
) -> None:
    app = _app(store, monkeypatch)
    admin = {"Authorization": f"Bearer {_ADMIN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        created = await client.post("/admin/agent/credentials", json={"name": "ci"}, headers=admin)
        assert created.status_code == 200, created.text
        secret = created.json()["credential"]
        listed = (await client.get("/admin/agent/credentials", headers=admin)).json()
        assert secret not in str(listed)
        bearer = {"Authorization": f"Bearer {secret}"}
        who = await client.get("/admin/whoami", headers=bearer)
        assert who.json()["admin"].startswith("admin-credential:")
        # It cannot mint or revoke credentials, nor act on deployments without the scope.
        assert (
            await client.post("/admin/agent/credentials", json={"name": "x"}, headers=bearer)
        ).status_code == 403
        token = await client.post(
            "/admin/agent/deployments/enroll-token", json={"role": "host"}, headers=bearer
        )
        assert token.status_code == 403
        scoped = (
            await client.post(
                "/admin/agent/credentials",
                json={"name": "deployer", "deployment": True},
                headers=admin,
            )
        ).json()
        allowed = await client.post(
            "/admin/agent/deployments/enroll-token",
            json={"role": "host"},
            headers={"Authorization": f"Bearer {scoped['credential']}"},
        )
        assert allowed.status_code == 200
        revoked = await client.post(
            f"/admin/agent/credentials/{created.json()['credential_id']}/revoke", headers=admin
        )
        assert revoked.status_code == 200
        assert (await client.get("/admin/whoami", headers=bearer)).status_code == 401
        # ADMIN_TOKEN keeps its full meaning throughout.
        assert (await client.get("/admin/whoami", headers=admin)).json() == {"admin": "admin-token"}
