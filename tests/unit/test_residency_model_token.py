"""The model relay placeholder: one residency's, and never another relay's credential."""

from __future__ import annotations

import pytest

from cloud_agent import tokens
from cloud_agent.tokens import InvalidAgentToken
from cloud_agent_shared.model_connections import valid_api_key
from cloud_agent_shared.shared_protocol import (
    is_residency_github_credential,
    is_residency_mcp_credential,
    is_residency_model_credential,
)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(tokens.ENV_SIGNING_SECRET, "model-relay-token-test-secret")


def _mint(kind: str, generation: int = 3) -> str:
    return getattr(tokens, f"mint_residency_{kind}_token")(
        thread_id="athr_1", residency_generation=generation
    )


def test_a_placeholder_names_its_residency_and_is_deterministic() -> None:
    token = _mint("model")
    assert token == _mint("model")
    assert token != _mint("model", generation=4)
    assert is_residency_model_credential(token)
    assert tokens.parse_residency_model_token(token) == {
        "thread_id": "athr_1",
        "residency_generation": 3,
    }


def test_a_cli_can_carry_it_as_an_api_key() -> None:
    # A header, a config file and an environment variable all carry it intact.
    assert valid_api_key(_mint("model"))


@pytest.mark.parametrize("other", ["mcp", "github"])
def test_no_relay_accepts_another_relays_credential(other: str) -> None:
    theirs, ours = _mint(other), _mint("model")
    assert not is_residency_model_credential(theirs)
    assert not is_residency_mcp_credential(ours)
    assert not is_residency_github_credential(ours)
    with pytest.raises(InvalidAgentToken):
        tokens.parse_residency_model_token(theirs)
    with pytest.raises(InvalidAgentToken):
        getattr(tokens, f"parse_residency_{other}_token")(ours)


def test_a_relabelled_credential_fails_its_signature() -> None:
    # The scope is signed: swapping the prefix does not make an MCP
    # credential a model one.
    _prefix, rest = _mint("mcp").split(".", 1)
    with pytest.raises(InvalidAgentToken):
        tokens.parse_residency_model_token(f"amd.{rest}")


@pytest.mark.parametrize("token", ["", "amd.", "amd.x.y", "sk-provider-key", "amd.a.b.c"])
def test_anything_else_is_not_a_placeholder(token: str) -> None:
    with pytest.raises(InvalidAgentToken):
        tokens.parse_residency_model_token(token)


def test_a_placeholder_signed_with_another_secret_is_refused(monkeypatch) -> None:
    token = _mint("model")
    monkeypatch.setenv(tokens.ENV_SIGNING_SECRET, "a-different-deployment-secret")
    with pytest.raises(InvalidAgentToken):
        tokens.parse_residency_model_token(token)
