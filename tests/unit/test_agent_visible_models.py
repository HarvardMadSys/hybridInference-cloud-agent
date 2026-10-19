"""The catalog, now asked for rather than computed.

The source's four tests walked a fake route registry — role gates, alias
resolution, unpublished/disabled/embedding exclusion, visibility-resolver
overrides. **None of that behaviour lives here**; it stays in the
gateway, which still owns the registry, and is covered there by
``tests/unit/test_internal_lookups.py`` (see MIGRATION.md's manifest notes for
the case-by-case record).

What this service can still get wrong is different, and is what these test:
whether it faithfully reports what the gateway said, and whether it fails
*closed* when the gateway cannot answer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cloud_agent.gateway import GatewayClient, GatewayError
from cloud_agent.visible_models import (
    ModelCatalogUnavailable,
    agent_canonical_model,
    agent_model_reasoning_efforts,
    agent_model_resolvable,
    agent_visible_models,
)

BASE = "https://gateway.example"
TOKEN = "dispatch-token"
CATALOG = ["glm-5.1", "qwen3.6-35b", "kimi-k2"]


def _client(handler) -> GatewayClient:
    transport = httpx.MockTransport(handler)
    return GatewayClient(BASE, TOKEN, client=httpx.AsyncClient(transport=transport))


def _serves(payload: dict[str, Any], status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# ---------------------------------------------------------------------------
# Reporting what the gateway said
# ---------------------------------------------------------------------------


async def test_the_catalog_is_whatever_the_gateway_returned() -> None:
    gateway = _client(_serves({"models": CATALOG}))
    assert await agent_visible_models(gateway, user_id="user_1") == CATALOG


async def test_the_request_carries_the_user_and_the_service_credential() -> None:
    """The endpoint resolves per user, so the user has to be on the wire.

    And the credential is a *service* one: it travels here and never toward a
    sandbox or a browser.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"models": CATALOG})

    await agent_visible_models(_client(handler), user_id="user_42")
    assert "user_id=user_42" in seen["url"]
    assert "/internal/model-catalog" in seen["url"]
    assert seen["auth"] == f"Bearer {TOKEN}"


async def test_the_catalog_is_not_the_public_models_route() -> None:
    """`/v1/models` cannot authenticate a service credential.

    It would answer with the anonymous catalog, silently narrowing every
    non-free user to what a stranger sees — a degrade with no error attached.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"models": CATALOG})

    await agent_visible_models(_client(handler), user_id="user_1")
    assert seen["path"] != "/v1/models"


async def test_resolvable_accepts_a_listed_model_and_refuses_an_unlisted_one() -> None:
    gateway = _client(_serves({"models": CATALOG}))
    assert await agent_model_resolvable("glm-5.1", gateway, user_id="user_1") is True
    assert await agent_model_resolvable("gpt-5-secret", gateway, user_id="user_1") is False


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "handler"),
    [
        ("gateway 500", _serves({}, 500)),
        ("gateway 403", _serves({}, 403)),
        ("no models key", _serves({"role": "pro"})),
        ("models not a list", _serves({"models": "everything"})),
    ],
)
async def test_an_unusable_answer_yields_no_models_not_every_model(name, handler) -> None:
    """The direction of this default is the whole point.

    An outage that widened what a job may call would do so at exactly the
    moment nobody is watching. An empty picker is visibly broken; a picker
    offering models the user cannot call produces a job that dies at its first
    request, far from the cause.
    """
    assert await agent_visible_models(_client(handler), user_id="user_1") == []


async def test_an_unreachable_gateway_yields_no_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert await agent_visible_models(_client(handler), user_id="user_1") == []


async def test_resolvable_refuses_everything_when_the_catalog_is_unavailable() -> None:
    """Fail-closed has to hold on the create-time check too.

    Otherwise an outage lets any model name through job creation, and the
    failure surfaces an attempt later.
    """
    gateway = _client(_serves({}, 503))
    assert await agent_model_resolvable("glm-5.1", gateway, user_id="user_1") is False


async def test_an_unconfigured_deployment_raises_rather_than_guessing() -> None:
    """At the client level the error is loud; only the caller softens it."""
    with pytest.raises(GatewayError):
        await GatewayClient("", "").model_catalog("user_1")


# ---------------------------------------------------------------------------
# Aliases: translate once, then work in canonical ids
# ---------------------------------------------------------------------------
#
# Before the split this service held the gateway's registry and resolved an
# alias for free. Afterwards it asks over HTTP, and a catalog of canonical ids
# alone silently stopped accepting every alias a user had been typing. The
# gateway now hands over its translation table; this is the one place that
# reads it, and nothing downstream — the store, the grant, the gateway's own
# scope check — ever sees an alias again.

_ALIASED = {"models": CATALOG, "aliases": {"legacy-name": CATALOG[0]}}


async def test_an_alias_resolves_to_the_canonical_id() -> None:
    gateway = _client(_serves(_ALIASED))

    assert await agent_canonical_model("legacy-name", gateway, user_id="user_1") == CATALOG[0]


async def test_a_canonical_id_resolves_to_itself() -> None:
    gateway = _client(_serves(_ALIASED))

    assert await agent_canonical_model(CATALOG[0], gateway, user_id="user_1") == CATALOG[0]


async def test_an_unknown_name_is_refused_rather_than_passed_through() -> None:
    """Returning the input unchanged would send an unknown model to the store
    and the grant, and fail at the sandbox's first call instead of at create."""
    gateway = _client(_serves(_ALIASED))

    assert await agent_canonical_model("no-such-model", gateway, user_id="user_1") is None


async def test_the_client_drops_an_alias_for_a_model_outside_the_catalog() -> None:
    """First of two layers: the client refuses to carry it inward."""
    gateway = _client(_serves({"models": CATALOG, "aliases": {"sneaky": "admin-only-model"}}))

    _models, aliases = await gateway.model_catalog_with_aliases("user_1")
    assert aliases == {}


async def test_the_client_drops_an_alias_spelled_like_a_real_model() -> None:
    """Same layer, the shadowing case."""
    gateway = _client(_serves({"models": CATALOG, "aliases": {CATALOG[0]: CATALOG[1]}}))

    _models, aliases = await gateway.model_catalog_with_aliases("user_1")
    assert aliases == {}


class _UnfilteredGateway:
    """A gateway whose table was *not* cleaned — the second layer's subject.

    `model_catalog_with_aliases` filters, so the checks in
    `agent_canonical_model` are unreachable through the real client and a
    mutation test cannot see them. That is precisely the argument for testing
    them directly rather than deleting them: this is a trust boundary, the
    table crosses a network from another service, and a guard that only holds
    because of what the layer above it happens to do is a guard nobody can
    verify.
    """

    def __init__(self, models: list[str], aliases: dict[str, str]) -> None:
        self._models = models
        self._aliases = aliases

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        return self._models, self._aliases


async def test_an_alias_for_a_model_outside_the_catalog_is_refused() -> None:
    """Even handed one directly, resolution refuses it."""
    gateway = _UnfilteredGateway(CATALOG, {"sneaky": "admin-only-model"})

    assert await agent_canonical_model("sneaky", gateway, user_id="user_1") is None


async def test_an_alias_cannot_shadow_a_real_model() -> None:
    """An exact catalog entry wins outright, whatever the table claims.

    A table that maps a real model's id somewhere else would otherwise redirect
    every request for it — and look valid at both ends. Driven past the
    client's filter, so this measures the resolution order and not the layer
    above it.
    """
    gateway = _UnfilteredGateway(CATALOG, {CATALOG[0]: CATALOG[1]})

    assert await agent_canonical_model(CATALOG[0], gateway, user_id="user_1") == CATALOG[0]


async def test_an_older_gateway_without_the_table_still_resolves_canonicals() -> None:
    """**Rollout order.** The gateway ships first, but a control plane that met
    an older one must narrow to canonical-only rather than stop creating jobs.
    """
    gateway = _client(_serves({"models": CATALOG}))

    assert await agent_canonical_model(CATALOG[0], gateway, user_id="user_1") == CATALOG[0]
    assert await agent_canonical_model("legacy-name", gateway, user_id="user_1") is None


async def test_an_unreachable_gateway_resolves_nothing() -> None:
    """Fail closed, as everywhere else: no catalog is no permission.

    Said as an outage, not as ``None``: an unknown name and an unread catalog
    need different answers, and only the first is the caller's to fix.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelCatalogUnavailable):
        await agent_canonical_model(CATALOG[0], _client(handler), user_id="user_1")


async def test_unreadable_effort_domains_raise_rather_than_offer_none() -> None:
    with pytest.raises(ModelCatalogUnavailable):
        await agent_model_reasoning_efforts(_client(_serves({}, 503)), user_id="user_1")


async def test_resolvable_accepts_an_alias_again() -> None:
    """The create-time fail-fast has to agree with what resolution accepts, or
    a job is refused at creation for a name that would have worked."""
    gateway = _client(_serves(_ALIASED))

    assert await agent_model_resolvable("legacy-name", gateway, user_id="user_1") is True
