"""Unit tests for per-attempt agent worker capability tokens."""

from __future__ import annotations

import pytest

from cloud_agent.tokens import (
    ENV_SIGNING_SECRET,
    SCOPE_MCP,
    InvalidAgentToken,
    mint_residency_mcp_token,
    mint_worker_token,
    parse_residency_mcp_token,
    parse_worker_token,
)
from cloud_agent_shared.shared_protocol import is_residency_mcp_credential


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    """Provide the signing secret these tokens derive from.

    The one edit this file needed: the secret is now this service's own
    (`AGENT_CONTROL_TOKEN_SECRET`), read straight from the environment, rather
    than the gateway's `API_KEY_SECRET` behind a cached settings object. Every
    assertion below is unchanged, which is the point — re-keying must not
    change what a token *means*.
    """
    monkeypatch.setenv(ENV_SIGNING_SECRET, "unit-test-secret")


def test_round_trip_preserves_the_fencing_triple():
    """A minted token parses back to exactly the triple it was bound to."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=7, lease_generation=3)
    assert parse_worker_token(token) == {
        "job_id": "ajob_abc",
        "attempt_id": 7,
        "lease_generation": 3,
        "scope": "full",
    }


def test_tokens_are_domain_prefixed():
    """Tokens carry the agent prefix so they can't be confused with API keys."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=1, lease_generation=1)
    assert token.startswith("ajt.")


def test_tampering_with_claims_is_rejected():
    """Swapping the payload without re-signing fails verification."""
    token = mint_worker_token(job_id="ajob_abc", attempt_id=1, lease_generation=1)
    other = mint_worker_token(job_id="ajob_xyz", attempt_id=9, lease_generation=4)
    forged = ".".join([token.split(".")[0], other.split(".")[1], token.split(".")[2]])
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(forged)


def test_signature_from_a_different_secret_is_rejected(monkeypatch):
    """A token minted under another secret does not verify.

    This is what makes the re-key real rather than cosmetic: after the split a
    token signed by the gateway's root is exactly "another secret" here, and
    must be refused.
    """
    token = mint_worker_token(job_id="ajob_abc", attempt_id=1, lease_generation=1)
    monkeypatch.setenv(ENV_SIGNING_SECRET, "a-different-secret")
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(token)


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-token", "ajt.only-two-parts", "xxx.aaa.bbb", "ajt.!!!.???"],
)
def test_malformed_tokens_raise(bad):
    """Structurally invalid tokens raise rather than returning junk claims."""
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(bad)


def test_scopes_round_trip():
    """A minted token reports the scope it was minted with."""
    from cloud_agent.tokens import SCOPE_FULL, SCOPE_MCP

    full = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1)
    mcp = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope=SCOPE_MCP)
    assert parse_worker_token(full)["scope"] == SCOPE_FULL
    assert parse_worker_token(mcp)["scope"] == SCOPE_MCP
    # Different scopes are different tokens: one cannot be swapped for the other.
    assert full != mcp


def test_the_retired_model_scope_is_no_longer_minted_or_parsed():
    """``model`` was the pre-grant sandbox credential; the cutover ended it (#822).

    Minting refuses it, and an old signed ``model`` token — the shape a job
    started before the H4 cutover would still carry — no longer parses at all
    rather than parsing into a scope nothing accepts.
    """
    from cloud_agent.tokens import (
        _TOKEN_PREFIX,
        InvalidAgentToken,
        _b64encode,
        _signing_key,
    )

    with pytest.raises(ValueError):
        mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope="model")

    import hashlib
    import hmac
    import json

    payload = json.dumps(
        {"j": "j", "a": 1, "g": 1, "s": "model"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    # Signed correctly on purpose: the refusal below must be the scope check,
    # not a signature failure.
    signature = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    legacy = f"{_TOKEN_PREFIX}.{_b64encode(payload)}.{_b64encode(signature)}"
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(legacy)


def test_scope_is_signed_not_advisory():
    """Editing the scope claim invalidates the signature."""
    from cloud_agent.tokens import SCOPE_MCP

    mcp = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope=SCOPE_MCP)
    full = mint_worker_token(job_id="j", attempt_id=1, lease_generation=1)
    # Splice the full token's payload onto the mcp token's signature.
    forged = ".".join([mcp.split(".")[0], full.split(".")[1], mcp.split(".")[2]])
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(forged)


def test_unknown_scope_is_rejected():
    """A signed token naming a scope we do not implement is refused."""
    with pytest.raises(ValueError):
        mint_worker_token(job_id="j", attempt_id=1, lease_generation=1, scope="admin")


def test_a_residency_credential_names_the_residency_and_is_stable():
    """Every mint for one residency is the same credential, so a probe can match it."""
    token = mint_residency_mcp_token(thread_id="athr_1", residency_generation=4)
    assert token == mint_residency_mcp_token(thread_id="athr_1", residency_generation=4)
    assert token != mint_residency_mcp_token(thread_id="athr_1", residency_generation=5)
    assert is_residency_mcp_credential(token)
    assert parse_residency_mcp_token(token) == {"thread_id": "athr_1", "residency_generation": 4}


def test_residency_and_worker_credentials_do_not_parse_as_each_other():
    """Neither parser accepts the other's credential, whatever its signature."""
    residency = mint_residency_mcp_token(thread_id="athr_1", residency_generation=4)
    worker = mint_worker_token(job_id="ajob_1", attempt_id=1, lease_generation=1, scope=SCOPE_MCP)
    assert not is_residency_mcp_credential(worker)
    with pytest.raises(InvalidAgentToken):
        parse_worker_token(residency)
    with pytest.raises(InvalidAgentToken):
        parse_residency_mcp_token(worker)
    # A worker payload re-signed under the residency prefix is still refused.
    _, payload, signature = worker.split(".")
    with pytest.raises(InvalidAgentToken):
        parse_residency_mcp_token(f"amr.{payload}.{signature}")


def test_a_tampered_residency_credential_is_rejected():
    token = mint_residency_mcp_token(thread_id="athr_1", residency_generation=4)
    prefix, _, signature = token.split(".")
    other = mint_residency_mcp_token(thread_id="athr_2", residency_generation=4).split(".")[1]
    with pytest.raises(InvalidAgentToken):
        parse_residency_mcp_token(f"{prefix}.{other}.{signature}")


@pytest.mark.parametrize(("thread_id", "generation"), [("", 1), ("athr_1", 0), ("athr_1", -1)])
def test_a_residency_credential_needs_a_thread_and_a_generation(thread_id, generation):
    with pytest.raises(ValueError):
        mint_residency_mcp_token(thread_id=thread_id, residency_generation=generation)
