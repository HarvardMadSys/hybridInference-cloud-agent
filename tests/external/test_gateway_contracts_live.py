"""Drive a running Gateway's real answers against the contract documents.

`gateway_contracts/*.openapi.yaml` are hand-transcriptions of the gateway's routers, and
the tests under `tests/unit/` pin this service's *consumer* half against them.
What neither can check is the producer: a document that has drifted from the
router it transcribes agrees perfectly with a client that drifted with it, and
both suites stay green. `tests/unit/test_lookup_contract_agreement.py` says so in
its own closing section, and `docs/smoke.md` records the manual runs that closed
the gap by hand.

This file makes those runs repeatable, over HTTP, against a deployment.
`test_gateway_access_contract.py` does the same for one endpoint; this extends it
to the other three contract families:

* **identity** — `/v1/identity/jwks`, whose key must be a *usable* RSA key and
  not merely a well-shaped document. A schema-valid key set holding a broken
  modulus passes every local test and fails at the first sign-in.
* **internal lookups** — `/internal/model-catalog`, whose refusal must be a
  typed 403 and must reach the client as a fail-closed `GatewayError`. An
  unknown account that resolved to an empty catalog, or to every model, would
  narrow or widen a job with no error anywhere downstream.
* **inference grants** — the refusal paths on `/renew` and `/revoke`, whose 404
  must name *this grant* rather than the gateway's own configuration. That is
  the distinction `DEAD_GRANT_REFUSALS` keys on, and reading it backwards
  abandons every running attempt in the fleet over one unset environment
  variable.

**A response may not carry a field the document does not declare.** Schema
validation cannot see that: JSON Schema ignores properties it does not know, so
a response that grew a field passes while the document and the client stay silent
about the addition. `_assert_only_declared_keys` is what makes that an error,
and it is the check that caught `reasoning_efforts` reaching the catalog — read
by the client, never declared, every test on both sides green. The catalog now
declares that field and `tests/unit/test_lookup_contract_agreement.py` lists the
read, so this suite runs green against a current gateway and the next field to
arrive that way fails here rather than being triaged as a known failure.

Set `AGENT_CONTRACT_GATEWAY_URL` and `AGENT_CONTRACT_DISPATCH_TOKEN`, then run
with ``pytest -m external``.

**What writes.** Every case is read-only except
`test_grant_lifecycle_mint_renew_revoke`, which mints a grant — a row at the
gateway — and revokes it before returning, including when an assertion fails.
That one is opt-in: it needs `AGENT_CONTRACT_SUBJECT` to name an account an
operator is willing to see this happen to, and it skips without it. The
read-only cases use a randomly named subject that cannot exist, so they can run
against any deployment at any time.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator
from jwt.algorithms import RSAAlgorithm

from cloud_agent.gateway import GatewayClient, GatewayError
from cloud_agent.grants import GrantClient
from cloud_agent.workers.attempts import DEAD_GRANT_REFUSALS

pytestmark = [pytest.mark.external, pytest.mark.asyncio]

CONTRACTS = Path(__file__).resolve().parents[2] / "gateway_contracts"
GRANTS_CONTRACT = "inference-grants.openapi.yaml"
LOOKUPS_CONTRACT = "internal-lookups.openapi.yaml"

_MINT_PATH = "/internal/agent-grants"
_RENEW_PATH = "/internal/agent-grants/{grant_id}/renew"


def _base_url() -> str:
    """The gateway under test, or a loud failure.

    ``pytest.fail`` rather than a skip: selecting ``-m external`` is a statement
    that a deployment is available, so a missing one is a misconfiguration and
    not a case that does not apply.
    """
    base = os.environ.get("AGENT_CONTRACT_GATEWAY_URL", "").rstrip("/")
    if not base:
        pytest.fail("Set AGENT_CONTRACT_GATEWAY_URL to the gateway under test")
    return base


def _dispatch_token() -> str:
    """The service credential, or a loud failure. Never printed."""
    token = os.environ.get("AGENT_CONTRACT_DISPATCH_TOKEN", "")
    if not token:
        pytest.fail("Set AGENT_CONTRACT_DISPATCH_TOKEN to a dispatch credential")
    return token


def _subject() -> str:
    """The account an operator offers for the cases that must write.

    Skips rather than fails when unset: the read-only cases cover every
    deployment, and the writing ones need a decision no default can make.
    """
    subject = os.environ.get("AGENT_CONTRACT_SUBJECT", "").strip()
    if not subject:
        pytest.skip("set AGENT_CONTRACT_SUBJECT to an account to run the writing cases")
    return subject


def _document(name: str) -> dict:
    return yaml.safe_load((CONTRACTS / name).read_text())


def _response_schema(document: dict, path: str, method: str, status: str) -> dict:
    """One declared response schema, with its components reachable.

    The documents keep their schemas under ``components`` and reference them with
    a local ``#/components/...`` pointer, so validating a single response means
    handing the validator a root that has both.
    """
    declared = document["paths"][path][method]["responses"][status]
    schema = declared["content"]["application/json"]["schema"]
    return {**schema, "components": document["components"]}


def _validate(document: dict, path: str, method: str, status: str, body: object) -> None:
    """Validate one response against the schema the document declares for it."""
    Draft202012Validator(_response_schema(document, path, method, status)).validate(body)


def _declared_keys(schema: dict, document: dict) -> set[str]:
    """Every property name a schema allows, following ``$ref`` and ``allOf``."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _declared_keys(document["components"]["schemas"][name], document)
    keys = set(schema.get("properties", {}))
    for part in schema.get("allOf", []):
        keys |= _declared_keys(part, document)
    return keys


def _assert_only_declared_keys(
    document: dict, path: str, method: str, status: str, body: dict
) -> None:
    """A response may not carry a field the document does not declare.

    The check ``_validate`` cannot make. JSON Schema ignores properties it does
    not know, so a response that grew a field validates cleanly while the
    document and the client stay silent about the addition — which is how
    ``reasoning_efforts`` reached the catalog. The client reads it, the document
    never declared it, and every test on both sides passed.
    """
    declared = _declared_keys(_response_schema(document, path, method, status), document)
    undeclared = sorted(set(body) - declared)
    assert not undeclared, (
        f"{path} {method} {status} returned fields the document does not declare: {undeclared}"
    )


async def test_identity_jwks_publishes_a_usable_key() -> None:
    """The key set must verify tokens, not merely describe them.

    Two properties the document cannot express are checked here: that each key is
    a real RSA public key (``RSAAlgorithm.from_jwk`` raises otherwise), and that
    each carries a non-empty ``kid``. The service resolves a signing key by
    ``kid``, so a set of anonymous keys is unusable however well shaped.
    """
    base = _base_url()
    document = _document("identity.openapi.yaml")

    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(f"{base}/v1/identity/jwks")

        if response.status_code == 404:
            # "Identity is switched off here" is a valid deployment and the
            # document says so — but only that one reason may be skipped. A base
            # URL pointing at the wrong host, or a proxy that does not forward
            # `/v1/identity`, answers 404 too, and skipping on any of them would
            # turn a broken setup into a passing suite.
            body = response.json()
            _validate(document, "/v1/identity/jwks", "get", "404", body)
            assert body["error"]["type"] == "identity_not_configured", (
                "a 404 from this route must be the documented 'not offered here'"
            )
            pytest.skip("this gateway has cross-service identity switched off")

        assert response.status_code == 200, response.text
        body = response.json()
        _validate(document, "/v1/identity/jwks", "get", "200", body)

        keys = body["keys"]
        assert keys, "a key set with no keys cannot verify anything"
        for key in keys:
            # Raises if the modulus or exponent is not a usable RSA public key.
            RSAAlgorithm.from_jwk(key)
            assert key["kid"], "the service resolves a signing key by kid"


async def test_model_catalog_refusal_is_typed_and_reaches_the_client() -> None:
    """An unknown account is a typed 403, and the client must not soften it.

    The catalog decides which models a job may call. A refusal that resolved to
    an empty list would create a job with no models; one that resolved to every
    model would be clamped to nothing at the grant. Neither is an error anywhere
    downstream, which is why the refusal has to arrive as one here.
    """
    base = _base_url()
    token = _dispatch_token()
    subject = f"contract-missing-{uuid4().hex}"
    document = _document(LOOKUPS_CONTRACT)

    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(
            f"{base}/internal/model-catalog",
            params={"user_id": subject},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403, response.text
        _validate(document, "/internal/model-catalog", "get", "403", response.json())

        with pytest.raises(GatewayError) as raised:
            await GatewayClient(base, token, client=http).model_catalog(subject)

    # The type is not asserted by name: this consumer turns any refusal into an
    # empty catalog, so what it needs is a typed refusal rather than a specific
    # one. `subject_unavailable` is the documented case, and the schema above is
    # what holds the gateway to it.
    assert raised.value.status_code == 403
    assert raised.value.error_type, "a refusal the client cannot classify is unusable"
    assert raised.value.definitive is True


async def test_model_catalog_answers_the_declared_shape_for_a_real_account() -> None:
    """The 200 path, for an operator willing to name an account to read.

    Two things are checked that local tests cannot. Every alias must resolve to a
    model in the list and none may shadow one — this service drops the entries
    that break it, so a violation narrows a user's catalog silently instead of
    failing. And the response may carry no field the document does not declare,
    which is the check that catches the document falling behind the gateway.
    """
    subject = _subject()
    base = _base_url()
    token = _dispatch_token()
    document = _document(LOOKUPS_CONTRACT)

    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(
            f"{base}/internal/model-catalog",
            params={"user_id": subject},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        _validate(document, "/internal/model-catalog", "get", "200", body)
        _assert_only_declared_keys(document, "/internal/model-catalog", "get", "200", body)

        models = set(body["models"])
        aliases = body["aliases"]
        assert set(aliases.values()) <= models, "an alias points outside the catalog"
        assert not (set(aliases) & models), "an alias shadows a real model id"

        client = GatewayClient(base, token, client=http)
        read_models, read_aliases = await client.model_catalog_with_aliases(subject)

    assert read_models == body["models"]
    assert read_aliases == aliases


async def test_grant_renewal_refusal_is_a_verdict_on_the_grant() -> None:
    """A 404 on renew must name the grant, not the gateway's configuration.

    Three facts share this status: the grant is unknown, it is expired or
    revoked, or this deployment exposes no internal endpoints at all. The control
    plane's give-up branches on exactly that difference
    (``DEAD_GRANT_REFUSALS``), and reading the third as the first destroys every
    running attempt in the fleet over one unset environment variable.
    """
    base = _base_url()
    token = _dispatch_token()
    grant_id = f"agr_contract_{uuid4().hex}"
    document = _document(GRANTS_CONTRACT)

    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.post(
            f"{base}/internal/agent-grants/{grant_id}/renew",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404, response.text
        body = response.json()
        _validate(document, _RENEW_PATH, "post", "404", body)

        # A dispatch token was supplied above, so "not configured" is a mismatch
        # between this run and the deployment rather than a case that does not
        # apply. Failing here is the same call the catalog case makes when a
        # refusal arrives as a status it did not expect.
        error_type = body["error"]["type"]
        assert error_type != "internal_api_not_configured", (
            "a dispatch token was supplied, but the gateway reports its internal API "
            "is not configured: this deployment cannot answer the contract"
        )

        client = GrantClient(base, token, client=http)
        with pytest.raises(GatewayError) as raised:
            await client.renew(grant_id)

        # Documented to fail soft: a failed revoke costs lateness, not safety,
        # because the TTL is the mechanism. The reference consumer reports rather
        # than raises, and that is what this asserts.
        revoke_response = await http.post(
            f"{base}/internal/agent-grants/{grant_id}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revoke_response.status_code == 404, revoke_response.text
        assert await client.revoke(grant_id) is False

    assert raised.value.status_code == 404
    assert raised.value.definitive is True
    assert raised.value.error_type in DEAD_GRANT_REFUSALS, (
        "the gateway refused this grant with a type the control plane does not read "
        "as a verdict; renewal would retry it until the capability expired"
    )


async def test_grant_lifecycle_mint_renew_revoke() -> None:
    """The whole lifecycle against a real gateway, for a willing subject.

    **This one writes.** Minting creates a grant row at the gateway; the case
    revokes it before returning, including when an assertion fails, so what it
    leaves behind is a revoked grant. It is opt-in for that reason, and it is the
    only case here that is.

    The last assertion is the point of the case: a revoked grant must be refused
    on renew with a type the control plane reads as a verdict on the grant. That
    is the property `DEAD_GRANT_REFUSALS` encodes, and making one and killing it
    is the only way to check the gateway agrees.
    """
    subject = _subject()
    base = _base_url()
    token = _dispatch_token()
    document = _document(GRANTS_CONTRACT)
    token_prefix = document["x-constants"]["token_prefix"]
    attempt = f"contract-{uuid4().hex}"

    async with httpx.AsyncClient(timeout=10) as http:
        client = GrantClient(base, token, client=http)

        minted = await client.mint(
            user_id=subject,
            job_id=f"contract-job-{attempt}",
            attempt_id=attempt,
        )

        # Read the id before validating anything. A response that fails the
        # document's schema still created a grant, and this is the only handle on
        # it — validating first would leave that grant live until its TTL.
        grant_id = minted.get("grant_id")
        if not isinstance(grant_id, str) or not grant_id:
            pytest.fail("mint returned no grant_id, so the grant it created cannot be revoked")

        try:
            _validate(document, _MINT_PATH, "post", "200", minted)
            _assert_only_declared_keys(document, _MINT_PATH, "post", "200", minted)
            assert minted["user_id"] == subject
            assert minted["token"].startswith(token_prefix)

            renewed = await client.renew(grant_id)
            _validate(document, _RENEW_PATH, "post", "200", renewed)
            _assert_only_declared_keys(document, _RENEW_PATH, "post", "200", renewed)
            assert renewed["grant_id"] == grant_id
            assert renewed["revoked_at"] is None
        finally:
            # Revoked even if an assertion above failed: a case that leaves a
            # live capability behind is worse than one that fails.
            revoked = await client.revoke(grant_id)
        assert revoked is True, "revoke reported a failure for a grant it just minted"

        # Idempotent, and it says so — the contract's `already_revoked`.
        again = await http.post(
            f"{base}/internal/agent-grants/{grant_id}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert again.status_code == 200, again.text
        assert again.json()["already_revoked"] is True

        # And the capability is genuinely dead, with a type the control plane
        # reads as a verdict rather than as its own misconfiguration.
        with pytest.raises(GatewayError) as raised:
            await client.renew(grant_id)

    assert raised.value.status_code == 404
    assert raised.value.error_type in DEAD_GRANT_REFUSALS
