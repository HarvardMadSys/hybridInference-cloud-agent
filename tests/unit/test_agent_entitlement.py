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
    require_allowed_repo,
)


def test_an_unconfigured_deployment_allows_nothing():
    """Unset must mean "no repositories", not "every repository".

    This is the whole posture: a deployment nobody configured has to refuse
    jobs. Reading an empty allowlist as "unrestricted" would reproduce the bug
    with extra steps.
    """
    assert allowed_repos({}) == []
    assert repo_is_allowed("owner/name", {}) is False
    with pytest.raises(RepoNotAllowed, match="not configured"):
        require_allowed_repo("owner/name", {})


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
def test_a_malformed_repo_is_refused_whatever_the_allowlist_says(repo: str):
    """The value is interpolated into a clone URL and handed to git."""
    env = {"AGENT_REPO_ALLOWLIST": f"{repo},owner/name"}
    assert repo_is_allowed(repo, env) is False
    with pytest.raises(RepoNotAllowed):
        require_allowed_repo(repo, env)


def test_a_list_is_read_with_whitespace_and_blanks_tolerated():
    """Operators write these by hand in an env file."""
    env = {"AGENT_REPO_ALLOWLIST": " owner/one , owner/two ,, "}
    assert allowed_repos(env) == ["owner/one", "owner/two"]
    assert repo_is_allowed("owner/two", env)


class _Grants:
    """The two reads entitlement makes of a user's GitHub connection."""

    def __init__(self, installation_ids: list[int], *, unreadable: bool = False) -> None:
        self._installation_ids = installation_ids
        self._unreadable = unreadable

    async def list_repo_grants(self, *, user_id: str) -> list[dict]:
        if self._unreadable:
            raise ConnectionError("database unavailable")
        return [{"installation_id": iid} for iid in self._installation_ids]

    async def get_github_account(self, *, user_id: str) -> dict:
        return {"access_token_ciphertext": "sealed", "access_expires_at": None}


class _App:
    """Answers, per installation, which repositories the user can push to."""

    def __init__(self, answers: dict[int, list[str] | Exception]) -> None:
        self._answers = answers
        self.cipher = type("Cipher", (), {"decrypt": staticmethod(lambda value: "ghu_user")})()

    def cached_user_repositories(self, user_id, installation_ids):
        return None

    def remember_user_repositories(self, user_id, installation_ids, repositories):
        pass

    async def writable_repositories_for_user(self, token: str, installation_id: int) -> list[str]:
        answer = self._answers[installation_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_an_installation_github_could_not_list_is_not_a_settled_refusal():
    """A GitHub outage used to read as "you have not connected this repository".

    The publisher then failed the group's snapshot for good, saying the owner
    was "no longer entitled". Still a refusal, so nothing opens meanwhile.
    """
    from cloud_agent.entitlement import EntitlementUnavailable, require_entitled_repo
    from cloud_agent.github_app import GitHubAppError

    app = _App({1: GitHubAppError("GitHub is down", status=502), 2: ["o/other"]})

    with pytest.raises(EntitlementUnavailable, match="could not be asked") as raised:
        await require_entitled_repo("o/n", "u1", store=_Grants([1, 2]), app_credentials=app, env={})
    assert isinstance(raised.value, RepoNotAllowed)
    # What the readable installation does grant is still granted.
    await require_entitled_repo("o/other", "u1", store=_Grants([1, 2]), app_credentials=app, env={})


async def test_a_connection_that_could_not_be_read_is_not_a_settled_refusal():
    from cloud_agent.entitlement import EntitlementUnavailable, require_entitled_repo

    with pytest.raises(EntitlementUnavailable):
        await require_entitled_repo(
            "o/n", "u1", store=_Grants([1], unreadable=True), app_credentials=_App({}), env={}
        )


async def test_a_complete_answer_without_the_repository_is_a_settled_refusal():
    from cloud_agent.entitlement import EntitlementUnavailable, require_entitled_repo

    with pytest.raises(RepoNotAllowed, match="have not connected") as raised:
        await require_entitled_repo(
            "o/n", "u1", store=_Grants([1]), app_credentials=_App({1: ["o/other"]}), env={}
        )
    assert not isinstance(raised.value, EntitlementUnavailable)
