"""The host lifecycle, as a table you can read and a credential you cannot forge.

These are the parts of phase G that need no database and no HTTP: which status
changes are legal, and whether a host credential is separable from a worker
one. Both are the kind of thing that is cheap to test here and expensive to
discover in an incident.
"""

from __future__ import annotations

import pytest

from cloud_agent.domain.host_pool import (
    HOST_DISABLED,
    HOST_DRAINING,
    HOST_ENROLLING,
    HOST_OFFLINE,
    HOST_ONLINE,
    HOST_STATUSES,
    allowed_targets,
    hash_secret,
    new_enroll_token,
    normalize_capabilities,
    transition_allowed,
)
from cloud_agent.tokens import (
    SCOPE_FULL,
    SCOPE_HOST,
    InvalidAgentToken,
    InvalidHostToken,
    mint_host_token,
    mint_worker_token,
    parse_host_token,
    parse_worker_token,
)

SECRET_ENV = "AGENT_CONTROL_TOKEN_SECRET"


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    """Sign with this service's own secret, never the gateway's."""
    monkeypatch.setenv(SECRET_ENV, "unit-test-control-secret")


# ── The status machine ─────────────────────────────────────────────────


def test_only_an_online_host_is_reachable_from_every_other_live_state():
    """Coming back must never need an operator.

    A machine that was offline, or is finishing a drain, has to be able to
    return to service — from the reaper's point of view for the first, from an
    admin's for the second. If either were a dead end, an outage would leave
    the pool permanently smaller than the hardware.
    """
    for status in (HOST_ENROLLING, HOST_OFFLINE, HOST_DRAINING):
        assert transition_allowed(status, HOST_ONLINE), status


def test_disabled_is_a_sink():
    """Revoking a host must not be undoable by a status change.

    ``remove`` clears the credential. If ``disabled -> online`` were legal, an
    admin could put a machine back in the pool while it holds no working
    credential — it would show as claimable and never claim — or worse, a
    future caller could re-enable a host that was removed for cause without
    anyone re-issuing what it lost.
    """
    for status in HOST_STATUSES:
        if status == HOST_DISABLED:
            continue
        assert not transition_allowed(HOST_DISABLED, status), status
    assert allowed_targets(HOST_DISABLED) == frozenset({HOST_DISABLED})


def test_every_live_status_can_be_disabled():
    """Revocation is always available. An admin must never be told to wait."""
    for status in HOST_STATUSES:
        assert transition_allowed(status, HOST_DISABLED), status


def test_self_transitions_are_no_ops_not_errors():
    """A heartbeat from an online host, or a second drain, must not 409."""
    for status in HOST_STATUSES:
        assert transition_allowed(status, status), status


def test_enrolling_cannot_be_drained():
    """There is nothing to drain: an enrolling host has never claimed."""
    assert not transition_allowed(HOST_ENROLLING, HOST_DRAINING)


def test_an_unrecognised_status_moves_nowhere():
    """A row written by a version this code is not answers 'no', not 'sure'."""
    assert not transition_allowed("quantum", HOST_ONLINE)
    assert allowed_targets("quantum") == frozenset()


# ── Capabilities ───────────────────────────────────────────────────────


def test_capabilities_are_sorted_and_deduplicated():
    assert normalize_capabilities(["kata", "runc", "kata", " gpu "]) == ["gpu", "kata", "runc"]


def test_blank_capabilities_are_dropped_rather_than_stored():
    """An empty string would be a capability no job can ever ask for by name."""
    assert normalize_capabilities(["", "   ", "kata"]) == ["kata"]
    assert normalize_capabilities(None) == []


# ── Enrollment secrets ─────────────────────────────────────────────────


def test_enroll_tokens_do_not_repeat():
    assert new_enroll_token() != new_enroll_token()


def test_hashing_is_stable_and_hides_the_token():
    token = new_enroll_token()
    assert hash_secret(token) == hash_secret(token)
    assert token not in hash_secret(token)


# ── Host credentials ───────────────────────────────────────────────────


def test_a_host_credential_round_trips_to_its_machine():
    token = mint_host_token(host="runner-a")
    assert parse_host_token(token) == {"host": "runner-a", "scope": SCOPE_HOST}


def test_re_minting_the_same_host_yields_a_different_credential():
    """Otherwise revoke-and-reissue hands back the credential just revoked.

    Revocation is a stored hash comparison, so two identical tokens for the
    same host would be the same credential — and clearing the hash then
    re-enrolling would restore access for whoever kept the old copy.
    """
    assert mint_host_token(host="runner-a") != mint_host_token(host="runner-a")


def test_a_host_credential_is_not_a_worker_token_and_the_reverse():
    """The two namespaces must be mutually unparseable.

    A worker token authorizes writes to one attempt; a host credential
    authorizes claiming for one machine. If either parser accepted the other's
    output, the scope check would be a comparison somebody has to remember to
    write rather than a structural property.
    """
    host_token = mint_host_token(host="runner-a")
    worker_token = mint_worker_token(
        job_id="job-1", attempt_id=1, lease_generation=1, scope=SCOPE_FULL
    )

    with pytest.raises(InvalidAgentToken):
        parse_worker_token(host_token)
    with pytest.raises(InvalidHostToken):
        parse_host_token(worker_token)


def test_a_tampered_host_credential_is_refused():
    token = mint_host_token(host="runner-a")
    prefix, payload, signature = token.split(".")
    forged = f"{prefix}.{payload}.{signature[:-4]}AAAA"

    with pytest.raises(InvalidHostToken):
        parse_host_token(forged)


def test_a_credential_signed_by_another_secret_is_refused(monkeypatch):
    """The signing root is this service's, and re-keying must invalidate."""
    token = mint_host_token(host="runner-a")
    monkeypatch.setenv(SECRET_ENV, "a-different-secret")

    with pytest.raises(InvalidHostToken):
        parse_host_token(token)


@pytest.mark.parametrize("token", ["", "not-a-token", "aht.only-two", "ajt.a.b"])
def test_malformed_host_credentials_are_refused(token):
    with pytest.raises(InvalidHostToken):
        parse_host_token(token)
