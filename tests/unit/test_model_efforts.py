"""Reasoning levels on an owner's connection come from the provider's own declaration.

Pins the regression this replaced -- every level refused on every connection,
the composer offering none -- and the rules that replaced it:

- the two documented declarations (Anthropic Models API ``capabilities.effort``,
  an OpenAI-style listing's ``reasoning.supported_efforts``) read as declared,
  including the HybridInference gateway's real answers
  (``tests/fixtures/gateway_model_effort_listing``);
- missing, refused, malformed or incomplete metadata is *unknown*, distinct
  from a declared "no levels";
- each runtime is offered the declared levels of the API its plan finally
  calls that its pinned CLI can send, in its own order, and nothing else;
- ``require_usable`` accepts exactly those, with reasons an owner can act on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cloud_agent.application import model_connection_efforts as efforts_io
from cloud_agent.domain.model_connections import (
    ModelConnectionUnusable,
    model_effort_offer,
    require_usable,
)
from cloud_agent.domain.model_efforts import (
    ListingRead,
    anthropic_effort,
    effort_facts,
    effort_levels,
    listing_effort,
    stored_fact,
)
from cloud_agent_shared.model_connections import PROTOCOLS

GATEWAY = Path(__file__).resolve().parents[1] / "fixtures" / "gateway_model_effort_listing"
DEEPSEEK_LIKE = "deepseek-like-reasoner"
#: The gateway's route with a reasoning_effort_map: public low/high/max (it
#: sends other names upstream, which the listing never shows).
MAPPED = "mapped-reasoner"
_GATEWAY_MODELS = [
    DEEPSEEK_LIKE,
    MAPPED,
    "plain-chat",
    "undeclared-reasoner",
    "malformed-domain",
    "native-anthropic",
    "mixed-routes",
    "minimal-only",
]


def _gateway(shape: str) -> dict[str, Any]:
    return json.loads((GATEWAY / f"{shape}.json").read_text())


def _gateway_facts(models: list[str] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    return effort_facts(
        models or _GATEWAY_MODELS,
        anthropic=ListingRead(body=_gateway("anthropic")),
        openai=ListingRead(body=_gateway("openai")),
    )


def _status(fact: dict[str, Any]) -> tuple[str, list[str], str]:
    return fact["status"], fact["levels"], fact["reason"]


def _statuses(facts: dict[str, dict[str, Any]]) -> list[tuple[str, list[str], str]]:
    """Every API's outcome, de-duplicated."""
    seen: list[tuple[str, list[str], str]] = []
    for fact in facts.values():
        if _status(fact) not in seen:
            seen.append(_status(fact))
    return seen


# -- reading a declaration -----------------------------------------------------


def test_the_gateways_listings_read_as_declared():
    facts = _gateway_facts()
    reasoner = facts[DEEPSEEK_LIKE]
    assert _status(reasoner["anthropic_messages"]) == ("supported", ["high", "max"], "")
    assert reasoner["anthropic_messages"]["source"] == "anthropic_models_api"
    for protocol in ("chat_completions", "responses"):
        assert _status(reasoner[protocol]) == ("supported", ["high", "max"], "")
        assert reasoner[protocol]["source"] == "models_listing_reasoning"
    # Declared "no level": an empty list in one shape, ``supported: false`` in the other.
    assert {f["status"] for f in facts["plain-chat"].values()} == {"unsupported"}
    # The gateway does not know: unknown, never unsupported.
    for model in ("undeclared-reasoner", "malformed-domain", "native-anthropic"):
        assert _statuses(facts[model]) == [("unknown", [], "not_declared")]
    # A mapped route lists its public levels in both shapes, the same three.
    assert _statuses(facts[MAPPED]) == [("supported", ["low", "high", "max"], "")]
    # Levels the Anthropic shape cannot name stay on the OpenAI APIs only.
    minimal = facts["minimal-only"]
    assert _status(minimal["chat_completions"]) == ("supported", ["none", "minimal"], "")
    assert minimal["anthropic_messages"]["status"] == "unsupported"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"id": "m"}, ("unknown", [], "not_declared")),
        ({"id": "m", "capabilities": None}, ("unknown", [], "not_declared")),
        ({"id": "m", "capabilities": {"thinking": {}}}, ("unknown", [], "not_declared")),
        (
            {"id": "m", "capabilities": {"effort": {"supported": "yes"}}},
            ("unknown", [], "not_declared"),
        ),
        ({"id": "m", "capabilities": {"effort": {"supported": False}}}, ("unsupported", [], "")),
        (
            # Anthropic's own shape, with ``xhigh: null`` (not declared).
            {
                "id": "m",
                "capabilities": {
                    "effort": {
                        "supported": True,
                        "low": {"supported": True},
                        "medium": {"supported": True},
                        "high": {"supported": True},
                        "xhigh": None,
                        "max": {"supported": True},
                    }
                },
            },
            ("supported", ["low", "medium", "high", "max"], ""),
        ),
        (
            {"id": "m", "capabilities": {"effort": {"supported": True}}},
            ("unknown", [], "not_declared"),
        ),
    ],
)
def test_anthropic_capabilities(entry, expected):
    assert _status(anthropic_effort(entry)) == expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"id": "m"}, ("unknown", [], "not_declared")),
        ({"id": "m", "reasoning": None}, ("unknown", [], "not_declared")),
        ({"id": "m", "reasoning": {"supported_efforts": "high"}}, ("unknown", [], "not_declared")),
        # OpenRouter: a reasoning model without effort selection.
        ({"id": "m", "reasoning": {"mandatory": True}}, ("unsupported", [], "")),
        ({"id": "m", "reasoning": {"supported_efforts": []}}, ("unsupported", [], "")),
        (
            {"id": "m", "reasoning": {"supported_efforts": ["high", "medium", "low", "minimal"]}},
            ("supported", ["high", "medium", "low", "minimal"], ""),
        ),
        # ``null``: every value the parameter takes; ``mandatory`` refuses ``none``.
        (
            {"id": "m", "reasoning": {"supported_efforts": None, "mandatory": True}},
            ("supported", ["minimal", "low", "medium", "high", "xhigh", "max"], ""),
        ),
        (
            {"id": "m", "reasoning": {"supported_efforts": ["high", 7, "", "high", "x y"]}},
            ("supported", ["high"], ""),
        ),
    ],
)
def test_listing_reasoning(entry, expected):
    assert _status(listing_effort(entry)) == expected


#: DeepSeek's List Models entry, as its API reference documents the shape.
_EFFORT_ENTRY = {
    "id": "m",
    "object": "model",
    "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (_EFFORT_ENTRY, ("supported", ["low", "high", "max"], "")),
        # ``none`` turns thinking off and is excluded by the shape: never inferred.
        ({"id": "m", "effort": {"supported_levels": ["high"]}}, ("supported", ["high"], "")),
        ({"id": "m", "effort": {"supported_levels": []}}, ("unsupported", [], "")),
        ({"id": "m"}, ("unknown", [], "not_declared")),
        ({"id": "m", "effort": None}, ("unknown", [], "not_declared")),
        # Without the required list nothing is declared -- not "every level".
        ({"id": "m", "effort": {"default_level": "high"}}, ("unknown", [], "not_declared")),
        ({"id": "m", "effort": {"supported_levels": "high"}}, ("unknown", [], "not_declared")),
        (
            {"id": "m", "effort": {"supported_levels": [7, "", "x y"]}},
            ("unknown", [], "not_declared"),
        ),
        (
            {"id": "m", "effort": {"supported_levels": ["max", 7, "max", "low"]}},
            ("supported", ["max", "low"], ""),
        ),
    ],
)
def test_listing_effort_levels(entry, expected):
    assert _status(effort_levels(entry)) == expected


def _listed(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return effort_facts(
        [entry["id"]],
        anthropic=ListingRead(failure="not_found"),
        openai=ListingRead(body={"object": "list", "data": [entry]}),
    )[entry["id"]]


def test_an_effort_levels_listing_speaks_for_chat_completions_only():
    facts = _listed(_EFFORT_ENTRY)
    assert _status(facts["chat_completions"]) == ("supported", ["low", "high", "max"], "")
    assert facts["chat_completions"]["source"] == "models_listing_effort"
    # Nothing in it describes Responses or Messages; neither is assumed.
    assert _status(facts["responses"]) == ("unknown", [], "not_declared")
    assert _status(facts["anthropic_messages"]) == ("unknown", [], "listing_unavailable")


def test_both_shapes_in_one_entry_must_agree():
    agreeing = {**_EFFORT_ENTRY, "reasoning": {"supported_efforts": ["max", "high", "low"]}}
    facts = _listed(agreeing)
    assert _status(facts["chat_completions"]) == ("supported", ["max", "high", "low"], "")
    assert _status(facts["responses"]) == ("supported", ["max", "high", "low"], "")
    conflicting = {**_EFFORT_ENTRY, "reasoning": {"supported_efforts": ["high", "xhigh"]}}
    facts = _listed(conflicting)
    assert _status(facts["chat_completions"]) == ("unknown", [], "conflicting_declarations")
    # Responses reads only the reasoning object, which is not in conflict with itself.
    assert _status(facts["responses"]) == ("supported", ["high", "xhigh"], "")


def test_a_failed_or_silent_listing_is_unknown_with_its_reason():
    facts = effort_facts(
        ["listed", "absent"],
        anthropic=ListingRead(failure="auth"),
        openai=ListingRead(body={"data": [{"id": "listed"}]}),
    )
    assert _status(facts["listed"]["anthropic_messages"]) == ("unknown", [], "listing_unavailable")
    assert _status(facts["listed"]["chat_completions"]) == ("unknown", [], "not_declared")
    assert _status(facts["absent"]["responses"]) == ("unknown", [], "not_listed")


def test_a_model_verified_before_levels_were_read_is_unknown_not_read():
    assert _status(stored_fact({"protocols": {}}, "chat_completions")) == (
        "unknown",
        [],
        "not_read",
    )
    garbage = {"efforts": {"chat_completions": {"status": "supported", "levels": "high"}}}
    assert stored_fact(garbage, "chat_completions")["status"] == "unknown"


# -- what each runtime is offered ---------------------------------------------


def _verified(protocols: list[str], efforts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "revision": 1,
        "protocols": {
            protocol: {
                "status": "supported" if protocol in protocols else "unsupported",
                "category": "supported" if protocol in protocols else "request_rejected",
            }
            for protocol in PROTOCOLS
        },
        "efforts": efforts,
    }


def _row(protocols: list[str], efforts: dict[str, dict[str, Any]], model: str = DEEPSEEK_LIKE):
    return {
        "enabled": True,
        "deleted_at": None,
        "models": [model],
        "config_revision": 1,
        "model_checks": {model: _verified(protocols, efforts)},
    }


def _all(levels: list[str], source: str = "models_listing_reasoning") -> dict[str, Any]:
    fact = {"status": "supported", "levels": levels, "source": source, "reason": ""}
    return dict.fromkeys(PROTOCOLS, fact)


def _offer(row: dict[str, Any], runtime: str, model: str = DEEPSEEK_LIKE):
    offer = model_effort_offer(row, model, runtime)
    assert offer is not None
    return offer.status, offer.levels, offer.reason


def test_each_runtime_is_offered_only_the_declared_levels_its_cli_can_send():
    row = _row(
        ["chat_completions", "responses", "anthropic_messages"],
        _all(["none", "minimal", "low", "medium", "high", "xhigh", "max"]),
    )
    assert _offer(row, "claude-code")[1] == ["low", "medium", "high", "xhigh", "max"]
    # pi's ``xhigh`` arrives as ``high`` on the wire; it is never offered.
    assert _offer(row, "pi")[1] == ["minimal", "low", "medium", "high"]
    for runtime in ("codex", "opencode", "kilo"):
        assert _offer(row, runtime)[1] == [
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ]


def test_a_runtime_whose_cli_can_send_no_declared_level_is_offered_auto_only():
    row = _row(["chat_completions"], _all(["xhigh", "max"]))
    assert _offer(row, "pi") == ("unsupported", [], "agent_cannot_express")
    assert _offer(row, "opencode") == ("supported", ["xhigh", "max"], "")


def test_the_screenshots_pairing_reads_the_gateways_anthropic_declaration_directly():
    """Claude Code on a DeepSeek-like model a gateway serves over Messages."""
    facts = _gateway_facts([DEEPSEEK_LIKE])[DEEPSEEK_LIKE]
    direct = _row(["chat_completions", "anthropic_messages"], facts)
    assert _offer(direct, "claude-code") == ("supported", ["high", "max"], "")
    assert _offer(direct, "opencode") == ("supported", ["high", "max"], "")


@pytest.mark.parametrize(
    "protocols",
    [
        pytest.param(["anthropic_messages", "chat_completions"], id="direct-messages"),
        pytest.param(["chat_completions"], id="translated-onto-chat"),
    ],
)
def test_a_configured_gateway_route_offers_its_three_public_levels(protocols):
    """The gateway's mapped route: Auto plus low/high/max on either plan, never xhigh."""
    facts = _gateway_facts([MAPPED])[MAPPED]
    row = _row(protocols, facts, model=MAPPED)
    for runtime in ("claude-code", "codex"):
        assert _offer(row, runtime, model=MAPPED) == ("supported", ["low", "high", "max"], "")
        for level in (None, "low", "high", "max"):
            require_usable(row, runtime=runtime, model=MAPPED, reasoning_effort=level)
        for level in ("xhigh", "medium", "none"):
            with pytest.raises(ModelConnectionUnusable) as refused:
                require_usable(row, runtime=runtime, model=MAPPED, reasoning_effort=level)
            assert refused.value.reason == "effort_level_unsupported"
            assert refused.value.offered == ["low", "high", "max"]


def test_a_deepseek_style_listing_offers_chat_levels_without_none():
    facts = _listed(_EFFORT_ENTRY)
    row = _row(["chat_completions"], facts, model="m")
    # Codex's CLI can send ``none``; the declaration does not list it, so it is not offered.
    assert _offer(row, "codex", model="m") == ("supported", ["low", "high", "max"], "")
    assert _offer(row, "claude-code", model="m") == ("supported", ["low", "high", "max"], "")
    # A Responses plan reads nothing from it.
    responses = _row(["responses"], facts, model="m")
    assert _offer(responses, "codex", model="m")[0] == "unknown"


def test_a_translated_plan_is_offered_what_its_chat_upstream_declares():
    # The Messages declaration would say otherwise; Claude Code is translated
    # onto Chat Completions here, so only Chat's counts.
    efforts = {
        **_all(["low", "high"]),
        "anthropic_messages": {
            "status": "supported",
            "levels": ["max"],
            "source": "anthropic_models_api",
            "reason": "",
        },
    }
    row = _row(["chat_completions"], efforts)
    assert _offer(row, "claude-code") == ("supported", ["low", "high"], "")
    assert _offer(row, "codex") == ("supported", ["low", "high"], "")


def test_an_unknown_declaration_offers_auto_only_and_says_why():
    row = _row(["chat_completions"], {})
    assert _offer(row, "opencode") == ("unknown", [], "not_read")


# -- what a turn may run at ----------------------------------------------------


def test_a_declared_level_runs_and_auto_always_does():
    row = _row(["anthropic_messages"], _gateway_facts([DEEPSEEK_LIKE])[DEEPSEEK_LIKE])
    for level in (None, "high", "max"):
        require_usable(row, runtime="claude-code", model=DEEPSEEK_LIKE, reasoning_effort=level)


@pytest.mark.parametrize(
    ("efforts", "runtime", "level", "reason", "offered"),
    [
        (_all(["high", "max"]), "claude-code", "low", "effort_level_unsupported", ["high", "max"]),
        # A level the provider declares but the CLI cannot send is not offered.
        (_all(["high", "xhigh"]), "pi", "xhigh", "effort_level_unsupported", ["high"]),
        # "Supported" with no level is not a declaration anyone could pick from.
        (_all([]), "opencode", "high", "effort_unknown", []),
        (
            {p: {"status": "unsupported", "levels": [], "source": "x"} for p in PROTOCOLS},
            "opencode",
            "high",
            "effort_unsupported",
            [],
        ),
        ({}, "codex", "high", "effort_unknown", []),
    ],
)
def test_anything_else_is_refused_with_what_is_offered(efforts, runtime, level, reason, offered):
    row = _row(["chat_completions", "anthropic_messages"], efforts)
    with pytest.raises(ModelConnectionUnusable) as refused:
        require_usable(row, runtime=runtime, model=DEEPSEEK_LIKE, reasoning_effort=level)
    assert refused.value.reason == reason
    assert refused.value.offered == offered
    if offered:
        assert ", ".join(offered) in str(refused.value)


# -- reading the listings -------------------------------------------------------


async def test_one_read_makes_two_listing_requests_with_each_apis_own_authentication():
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        shape = "anthropic" if "anthropic-version" in request.headers else "openai"
        return httpx.Response(200, json=_gateway(shape))

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    facts = await efforts_io.read_effort_facts(
        base_url="https://gateway.example/v1",
        api_key="sk-owner-fixture",
        models=[DEEPSEEK_LIKE, "plain-chat"],
        resolve=resolve,
        transport=httpx.MockTransport(answer),
    )

    assert sorted(str(r.url) for r in seen) == [
        "https://gateway.example/v1/models",
        "https://gateway.example/v1/models?limit=1000",
    ]
    for request in seen:
        if "anthropic-version" in request.headers:
            assert request.headers["x-api-key"] == "sk-owner-fixture"
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == "Bearer sk-owner-fixture"
            assert "x-api-key" not in request.headers
        assert request.method == "GET"
    reasoner = facts[DEEPSEEK_LIKE]["efforts"]
    assert reasoner["anthropic_messages"]["levels"] == ["high", "max"]
    assert reasoner["chat_completions"]["levels"] == ["high", "max"]
    assert facts["plain-chat"]["efforts"]["chat_completions"]["status"] == "unsupported"
    assert facts[DEEPSEEK_LIKE]["efforts_checked_at"]


async def test_a_listing_that_fails_is_unknown_and_never_raises():
    def answer(request: httpx.Request) -> httpx.Response:
        if "anthropic-version" in request.headers:
            return httpx.Response(404)
        return httpx.Response(200, content=b"not json")

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    facts = await efforts_io.read_effort_facts(
        base_url="https://provider.example/v1",
        api_key="k",
        models=["m"],
        resolve=resolve,
        transport=httpx.MockTransport(answer),
    )
    assert _statuses(facts["m"]["efforts"]) == [("unknown", [], "listing_unavailable")]


async def test_a_blocked_address_reads_nothing():
    calls: list[httpx.Request] = []

    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.5"]

    facts = await efforts_io.read_effort_facts(
        base_url="https://internal.example/v1",
        api_key="k",
        models=["m"],
        resolve=private,
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
    )
    assert calls == []
    assert {f["reason"] for f in facts["m"]["efforts"].values()} == {"listing_unavailable"}
