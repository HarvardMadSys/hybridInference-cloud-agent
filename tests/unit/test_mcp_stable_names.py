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
