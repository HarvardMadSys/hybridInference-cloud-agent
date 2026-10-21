"""A cached CLI name keeps its meaning when another capability disappears."""

import re

import pytest

from cloud_agent.mcp import protocol
from cloud_agent.mcp.models import Capability, CapabilityKind


@pytest.mark.parametrize(
    "pairs",
    [
        [("a__b", "c"), ("a", "b__c")],
        [("a_", "b"), ("a", "_b")],
        [("docs", "a.b"), ("docs", "a/b"), ("docs", "a_b")],
        [("docs", "x" * 70 + "a"), ("docs", "x" * 70 + "b")],
        [("a" * 70, "search"), ("a" * 71, "search")],
        [("docs", ""), ("docs", "tool")],
    ],
)
def test_names_are_independent_of_catalogue_order_and_membership(pairs):
    """Sanitizing, truncating and joining must not reassign another tool's name."""
    individually = {pair: protocol.presented_name(*pair) for pair in pairs}
    assert len(set(individually.values())) == len(pairs)
    for ordered in (pairs, list(reversed(pairs))):
        names = {}
        for pair in ordered:
            name = protocol.presented_name(*pair)
            names[name] = pair
            assert name == individually[pair]
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            assert name == name.strip("_")
            assert len("mcp__session__" + name) <= 64


_ALIASES = ["github", "docs", "a__b", "a", "a_", "mcp_h", "mcp", "Docs.v2", "über", "x" * 70, ""]
_CAPABILITIES = ["get_issue", "search", "b__c", "_b", "a.b", "a/b", "tool", "", "x" * 70]


@pytest.mark.parametrize("alias", _ALIASES)
@pytest.mark.parametrize("capability", _CAPABILITIES)
def test_routing_never_rules_out_the_alias_that_presents_a_name(alias, capability):
    """A call is routed by resolving only the aliases that may present its name."""
    assert protocol.may_present(alias, protocol.presented_name(alias, capability))


def test_a_readable_name_rules_out_every_other_alias():
    """The common case resolves one binding, not all of them."""
    name = protocol.presented_name("docs", "search")
    assert protocol.may_present("docs", name)
    assert not protocol.may_present("github", name)
    assert not protocol.may_present("doc", name)
    assert not protocol.may_present("docs", "github__get_issue")


def test_a_routed_identity_keeps_only_the_bindings_that_may_present_its_name():
    """Everything else about the job is kept, and the identity itself is not changed."""
    bindings = [{"alias": "github"}, {"alias": "docs", "connection_id": 8}, {"alias": ""}]
    identity = {"job_id": "job-1", "user_id": "user-1", "mcp_bindings": bindings}

    narrowed = protocol.presenting(identity, protocol.presented_name("docs", "search"))

    assert narrowed == {**identity, "mcp_bindings": [{"alias": "docs", "connection_id": 8}]}
    assert identity["mcp_bindings"] == bindings
    assert protocol.presenting({"job_id": "job-1"}, "docs__search")["mcp_bindings"] == []


def test_common_short_names_keep_their_readable_form():
    """Existing ordinary names remain stable too."""
    assert protocol.presented_name("github", "get_issue") == "github__get_issue"


def test_a_literal_name_cannot_claim_the_generated_name_namespace():
    """A server cannot choose a literal that impersonates a hashed capability."""
    generated = protocol.presented_name("docs", "a.b")
    alias, _, tool = generated.partition("__")
    assert protocol.presented_name(alias, tool) != generated


def test_namespace_mapping_survives_removing_a_colliding_capability():
    """The mapping used by adapters keeps the same original tool names."""
    capabilities = [
        Capability(kind=CapabilityKind.TOOL, name=name) for name in ("a.b", "a/b", "a_b")
    ]
    complete = protocol.build_namespace("docs", capabilities)
    remaining = protocol.build_namespace("docs", capabilities[1:])
    assert all(complete[name] == original for name, original in remaining.items())
