"""A conversation's commits are its owner's, with the agent named as co-author."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent.source_control.git_identity import git_identity, residency_github_fields
from cloud_agent.tokens import parse_residency_github_token
from cloud_agent_shared.egress import RelayEndpoint, network_from_env

_COAUTHOR = "FreeInference <9+freeinference[bot]@users.noreply.github.com>"


def _accounts(account):
    return SimpleNamespace(get_github_account=AsyncMock(return_value=account))


@pytest.mark.asyncio
async def test_the_owner_authors_with_their_github_noreply_address(monkeypatch):
    monkeypatch.setenv("AGENT_GITHUB_COAUTHOR", _COAUTHOR)
    identity = await git_identity(_accounts({"login": "octo", "github_user_id": 7}), "user_1")
    assert identity == {
        "git_author_name": "octo",
        "git_author_email": "7+octo@users.noreply.github.com",
        "git_coauthor": _COAUTHOR,
    }
    monkeypatch.delenv("AGENT_GITHUB_COAUTHOR")
    assert await git_identity(_accounts(None), "user_1") == {"git_coauthor": ""}


@pytest.mark.asyncio
async def test_a_residency_gets_its_own_placeholder_with_the_identity(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "git-identity-test-secret")
    fields = await residency_github_fields(
        _accounts({"login": "octo", "github_user_id": 7}),
        "athr_1",
        {"generation": 4, "user_id": "user_1"},
    )
    assert parse_residency_github_token(fields["github_credential"]) == {
        "thread_id": "athr_1",
        "residency_generation": 4,
    }
    assert fields["git_author_name"] == "octo"


def test_group_bridges_carry_the_github_names_only_with_the_relay_configured():
    env = {
        "AGENT_SANDBOX_NAMESPACE": "ns",
        "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
        "AGENT_EGRESS_DNS_SERVER": "192.0.2.53",
        "AGENT_RELAY_NETWORK": "relays",
        "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
    }
    assert network_from_env(env).relays == (RelayEndpoint("agent-mcp", 8000),)
    with_relay = network_from_env({**env, "AGENT_SANDBOX_GITHUB_RELAY_CA_FILE": "/ca.pem"})
    assert with_relay.relays == (
        RelayEndpoint("agent-mcp", 8000),
        RelayEndpoint("github.com", 443),
        RelayEndpoint("api.github.com", 443),
    )
