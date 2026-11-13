"""An MCP OAuth client secret authenticates to the authorization server, never upstream."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.unit.test_mcp_connections_api import (
    _ENCRYPTION_KEY,
    FakeStore,
    _binding,
    _client,
    _key as _key,
    _seed,
)

from cloud_agent.mcp.registry import mcp_cipher

if TYPE_CHECKING:
    import pytest


class TestTheClientSecretIsNotACredential:
    """A client secret authenticates to the authorization server, never upstream.

    A user who brings a client id and secret has them stored before the browser
    flow starts, so an abandoned or failed sign-in leaves a row holding a client
    secret and no access token. That row needs authorizing; it has no credential.
    """

    def test_an_unfinished_sign_in_needs_authorizing_rather_than_reconnecting(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = FakeStore()
        connection_id = _seed(
            store, auth_kind="oauth", auth_config={}, access_token_ciphertext=None
        )
        with _client(store) as client:
            client.post(
                f"/v1/agent/mcp/connections/{connection_id}/authorize",
                json={"client_id": "our-client", "client_secret": "shh"},
            )
            assert store.connections[connection_id]["client_secret_ciphertext"]
            view = client.get(f"/v1/agent/mcp/connections/{connection_id}").json()
            with caplog.at_level("WARNING"):
                result = client.post(f"/v1/agent/mcp/connections/{connection_id}/test").json()
        assert view["has_credential"] is False
        assert result["ok"] is False
        assert result["error_kind"] == "needs_authorization"
        assert result["message"] == "This connection needs to be authorized."
        assert "agent_mcp_credential_unreadable" not in caplog.text

    async def test_a_client_secret_is_never_resolved_as_the_bearer(self) -> None:
        """Not even one the credential cipher could read."""
        from cloud_agent.mcp.registry import resolve_connection, server_for

        store = FakeStore()
        connection_id = _seed(
            store,
            auth_kind="oauth",
            auth_config={},
            access_token_ciphertext=None,
            client_secret_ciphertext=mcp_cipher(_ENCRYPTION_KEY).encrypt("client-secret"),
        )
        resolved = await resolve_connection(store, row=store._row(connection_id))
        assert not resolved.usable
        assert "client-secret" not in json.dumps(dict(resolved.credential))
        assert resolved.error == "This connection needs to be authorized."
        assert "client-secret" not in json.dumps(
            server_for(resolved, _binding(connection_id)).headers
        )
