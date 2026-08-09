"""What the curated catalogue must be true of, entry by entry.

Two different kinds of claim live here and they are worth separating.

The **invariants** hold for every entry, present and future: an agent runs
untrusted repository content, so a server it can reach must have a bound on
what it may call, and that bound must be one somebody chose rather than one
that fell out of an empty field. These are the tests that make adding a
provider a reviewed act.

The **entry checks** are facts about somebody else's service — a URL, a
read-only mode. They are pinned because a wrong one fails at the first tool
call of a real job, which is far from here; they are kept few and named,
because they are the assertions that go stale when a provider changes something
and no amount of testing here will notice.
"""

from __future__ import annotations

import pytest

from cloud_agent.mcp_catalogue import CATALOGUE, McpProvider, catalogue, get_provider


def _provider(**overrides) -> McpProvider:
    """Build an entry that is valid unless a test breaks one field."""
    fields = {
        "name": "example",
        "label": "Example",
        "description": "",
        "url": "https://mcp.example.com/mcp/readonly",
        "read_scope": "endpoint",
        "scope_note": "the provider serves a read-only endpoint",
    }
    fields.update(overrides)
    return McpProvider(**fields)


# ── invariants every entry must satisfy ───────────────────────────────


@pytest.mark.parametrize("provider", CATALOGUE, ids=lambda entry: entry.name)
def test_every_entry_bounds_its_tool_surface(provider: McpProvider) -> None:
    """No entry may leave what an agent can call open-ended.

    ``read_scope`` is the whole safety argument of a per-user catalogue: with
    no operator left to write ``tools:`` per server, an entry that bounds
    nothing hands an agent every tool the upstream grows, silently, on the
    upstream's release schedule.
    """
    assert provider.read_scope in {"endpoint", "allowlist"}
    if provider.read_scope == "allowlist":
        assert provider.tools, "an allowlist scope with no tools bounds nothing"


@pytest.mark.parametrize("provider", CATALOGUE, ids=lambda entry: entry.name)
def test_every_entry_explains_why_its_scope_is_safe(provider: McpProvider) -> None:
    """A reviewer has to be able to check the claim, not just see one."""
    assert provider.scope_note.strip()


@pytest.mark.parametrize("provider", CATALOGUE, ids=lambda entry: entry.name)
def test_every_entry_is_reached_over_https(provider: McpProvider) -> None:
    """The credential travels on this connection; plaintext would publish it."""
    assert provider.url.startswith("https://")


def test_names_are_unique() -> None:
    """A name is how a job asks for a server; two entries sharing one is a coin toss."""
    names = [provider.name for provider in CATALOGUE]
    assert len(names) == len(set(names))


def test_an_entry_cannot_claim_a_bound_it_does_not_implement() -> None:
    """The declaration is checked against the entry, not trusted.

    Both directions are rejected, and the second is the one that would
    otherwise pass review: a tool list beside an endpoint bound reads as
    defence in depth and is actually a second, staler bound that silently drops
    tools the provider adds.
    """
    with pytest.raises(ValueError, match="lists no tools"):
        _provider(read_scope="allowlist", tools=frozenset())
    with pytest.raises(ValueError, match="endpoint scope"):
        _provider(read_scope="endpoint", tools=frozenset({"get_issue"}))
    with pytest.raises(ValueError, match="explain"):
        _provider(scope_note="  ")


def test_a_connection_to_something_not_in_the_catalogue_resolves_to_nothing() -> None:
    """Unknown is a normal answer here — a retired provider leaves live rows."""
    assert get_provider("no-such-server") is None


# ── facts about the providers themselves ──────────────────────────────


def test_github_is_pinned_to_its_read_only_endpoint() -> None:
    """The bound is the URL, so the URL is the thing to get right.

    GitHub serves the same toolsets at ``/mcp/`` and ``/mcp/readonly`` and
    excludes write tools from the second server-side. Pointing at the first
    would leave an agent holding the user's own write access to every
    repository their account can reach, with nothing here to notice.
    """
    github = get_provider("github")
    assert github is not None
    assert github.url == "https://api.githubcopilot.com/mcp/readonly"
    assert github.read_scope == "endpoint"


def test_the_catalogue_is_what_the_module_exposes() -> None:
    """``catalogue()`` is the accessor every consumer reads; it must not diverge."""
    assert catalogue() == CATALOGUE
