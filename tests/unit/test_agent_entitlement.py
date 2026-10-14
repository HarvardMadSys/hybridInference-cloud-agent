"""Which repositories a deployment will run agent jobs against.

The hole this closes is a confused deputy: the requester picks the repository
string, and the platform later mints a real GitHub installation token for it —
a read token to clone with and a write token to push with. Neither asked whose
repository it was, so any authenticated user could name any repo the App
reached and read it back through their own job's events and retained workspace.
"""

from __future__ import annotations

import pytest

from cloud_agent.entitlement import (
    RepoNotAllowed,
    allowed_repos,
    repo_is_allowed,
    require_entitled_repo,
)


async def test_an_unconfigured_deployment_allows_nothing():
    """Unset must mean "no repositories", not "every repository".

    This is the whole posture: a deployment nobody configured has to refuse
    jobs. Reading an empty allowlist as "unrestricted" would reproduce the bug
    with extra steps.
    """
    assert allowed_repos({}) == []
    assert repo_is_allowed("owner/name", {}) is False
    with pytest.raises(RepoNotAllowed, match="allowlist"):
        await require_entitled_repo("owner/name", "user_1", env={})


async def test_the_allowlist_entitles_every_admitted_user_without_a_connection():
    """Not per user: anyone admitted may have the platform work on a listed repo."""
    env = {"AGENT_REPO_ALLOWLIST": "owner/*"}
    for user_id in ("user_1", "user_2"):
        await require_entitled_repo("owner/name", user_id, env=env)


def test_an_exact_entry_is_allowed_and_its_neighbours_are_not():
    """Entitlement is per repository, not per prefix."""
    env = {"AGENT_REPO_ALLOWLIST": "ExampleOrg/ExampleRepo"}

    assert repo_is_allowed("ExampleOrg/ExampleRepo", env)
    # Case-insensitive, as GitHub is.
    assert repo_is_allowed("exampleorg/examplerepo", env)
    # A different repo under the same owner is not implied.
    assert not repo_is_allowed("ExampleOrg/other", env)
    # And a repo whose name merely starts the same must not match.
    assert not repo_is_allowed("ExampleOrg/ExampleRepo-evil", env)


def test_an_owner_wildcard_covers_that_owner_only():
    """`owner/*` is enough for "our own org" without a pattern language."""
    env = {"AGENT_REPO_ALLOWLIST": "ExampleOrg/*"}

    assert repo_is_allowed("ExampleOrg/anything", env)
    assert not repo_is_allowed("SomeoneElse/anything", env)
    # Not a prefix match on the owner either.
    assert not repo_is_allowed("ExampleOrgEvil/anything", env)


@pytest.mark.parametrize(
    "repo",
    [
        "not-a-slug",
        "a/b/c",
        "--upload-pack=/bin/sh",
        "/etc/passwd",
        "../../etc/passwd",
        "owner/",
        "",
    ],
)
async def test_a_malformed_repo_is_refused_whatever_the_allowlist_says(repo: str):
    """The value is interpolated into a clone URL and handed to git."""
    env = {"AGENT_REPO_ALLOWLIST": f"{repo},owner/name"}
    assert repo_is_allowed(repo, env) is False
    with pytest.raises(RepoNotAllowed):
        await require_entitled_repo(repo, "user_1", env=env)


def test_a_list_is_read_with_whitespace_and_blanks_tolerated():
    """Operators write these by hand in an env file."""
    env = {"AGENT_REPO_ALLOWLIST": " owner/one , owner/two ,, "}
    assert allowed_repos(env) == ["owner/one", "owner/two"]
    assert repo_is_allowed("owner/two", env)
