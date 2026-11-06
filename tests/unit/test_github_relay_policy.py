"""What the GitHub relay lets a conversation do as its owner, rule by rule."""

from __future__ import annotations

import pytest

from cloud_agent.source_control.github_relay_policy import (
    API_HOST,
    WEB_HOST,
    Outcome,
    PushRefused,
    RefUpdate,
    check_push_commands,
    check_ref_creation,
    decide,
    split_push_commands,
)
from cloud_agent.tokens import (
    InvalidAgentToken,
    mint_residency_github_token,
    mint_residency_mcp_token,
    parse_residency_github_token,
    parse_residency_mcp_token,
)
from cloud_agent_shared.shared_protocol import (
    is_residency_github_credential,
    is_residency_mcp_credential,
)

_REPO = "Owner/Repo"
_OID = "a" * 40
_ZERO = "0" * 40


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "relay-policy-test-secret")


def _api(method: str, path: str, repo: str | None = _REPO):
    return decide(host=API_HOST, method=method, path=path, query="", session_repo=repo)


def _web(method: str, path: str, query: str = "", repo: str | None = _REPO):
    return decide(host=WEB_HOST, method=method, path=path, query=query, session_repo=repo)


def test_the_placeholder_names_its_residency_and_is_no_mcp_credential():
    token = mint_residency_github_token(thread_id="athr_1", residency_generation=3)
    assert is_residency_github_credential(token)
    assert not is_residency_mcp_credential(token)
    assert parse_residency_github_token(token) == {"thread_id": "athr_1", "residency_generation": 3}
    with pytest.raises(InvalidAgentToken):
        parse_residency_mcp_token(token)
    with pytest.raises(InvalidAgentToken):
        parse_residency_github_token(
            mint_residency_mcp_token(thread_id="athr_1", residency_generation=3)
        )
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidAgentToken):
        parse_residency_github_token(forged)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/repos/owner/repo"),
        ("GET", "/repos/owner/repo/pulls/7"),
        ("GET", "/repos/owner/repo/commits/" + _OID + "/check-runs"),
        ("POST", "/repos/owner/repo/pulls"),
        ("PATCH", "/repos/owner/repo/pulls/7"),
        ("PUT", "/repos/owner/repo/pulls/7/merge"),
        ("POST", "/repos/owner/repo/issues/7/comments"),
        ("POST", "/repos/owner/repo/pulls/7/comments/9/replies"),
        ("POST", "/repos/owner/repo/actions/runs/5/rerun-failed-jobs"),
        ("PATCH", "/repos/owner/repo/git/refs/heads/feature/x"),
        ("GET", "/user"),
        ("GET", "/rate_limit"),
    ],
)
def test_the_work_of_a_pull_request_speaks_as_the_owner(method, path):
    assert _api(method, path).outcome is Outcome.CREDENTIAL


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/repos/owner/repo"),
        ("DELETE", "/repos/owner/repo"),
        ("POST", "/repos/owner/repo/hooks"),
        ("GET", "/repos/owner/repo/hooks"),
        ("GET", "/repos/owner/repo/actions/secrets"),
        ("PUT", "/repos/owner/repo/actions/secrets/TOKEN"),
        ("GET", "/repos/owner/repo/environments/prod/secrets"),
        ("PUT", "/repos/owner/repo/branches/dev/protection"),
        ("PUT", "/repos/owner/repo/collaborators/someone"),
        ("POST", "/repos/owner/repo/transfer"),
        ("DELETE", "/repos/owner/repo/git/refs/heads/dev"),
        ("POST", "/repos/owner/repo/keys"),
    ],
)
def test_a_repository_s_administration_and_secrets_are_refused(method, path):
    assert _api(method, path).outcome is Outcome.REFUSE


def test_graphql_is_refused_with_the_rest_alternative():
    decision = _api("POST", "/graphql")
    assert decision.outcome is Outcome.REFUSE
    assert "gh api repos/" in decision.reason


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/repos/other/repo/pulls"),
        ("POST", "/repos/other/repo/pulls"),
        ("GET", "/search/code"),
        ("GET", "/users/someone"),
        ("GET", "/repositories/123"),
    ],
)
def test_anything_else_goes_as_nobody(method, path):
    assert _api(method, path).outcome is Outcome.ANONYMOUS


def test_a_conversation_without_a_repository_never_speaks_as_anyone():
    assert _api("GET", "/repos/owner/repo/pulls", repo=None).outcome is Outcome.ANONYMOUS
    assert _web(
        "GET", "/owner/repo.git/info/refs", "service=git-upload-pack", repo=None
    ).outcome is (Outcome.ANONYMOUS)


def test_git_smart_http_for_the_conversation_s_repository():
    fetch = _web("GET", "/owner/repo.git/info/refs", "service=git-upload-pack")
    advertise = _web("GET", "/OWNER/REPO/info/refs", "service=git-receive-pack")
    upload = _web("POST", "/owner/repo.git/git-upload-pack")
    push = _web("POST", "/owner/repo.git/git-receive-pack")
    assert {fetch.outcome, advertise.outcome, upload.outcome, push.outcome} == {Outcome.CREDENTIAL}
    assert push.inspect_push and not upload.inspect_push
    assert _web("GET", "/owner/repo.git/info/refs", "service=other").outcome is Outcome.REFUSE
    assert _web("GET", "/owner/repo/pulls").outcome is Outcome.ANONYMOUS
    assert _web("POST", "/other/repo.git/git-receive-pack").outcome is Outcome.ANONYMOUS


def test_creating_a_pull_request_is_recorded_and_creating_a_ref_is_inspected():
    assert _api("POST", "/repos/owner/repo/pulls").records_pull_request
    assert not _api("PATCH", "/repos/owner/repo/pulls/7").records_pull_request
    assert _api("POST", "/repos/owner/repo/git/refs").inspect_ref_creation
    check_ref_creation("refs/heads/feature")
    for ref in ("refs/tags/v1", "refs/heads/", "refs/notes/x", None):
        with pytest.raises(PushRefused):
            check_ref_creation(ref)


def _pkt(text: str) -> bytes:
    return f"{len(text) + 4:04x}".encode() + text.encode()


def test_push_commands_are_read_through_the_flush_packet():
    body = (
        _pkt(f"{_ZERO} {_OID} refs/heads/feature\0report-status side-band-64k\n")
        + _pkt(f"{_OID} {'b' * 40} refs/heads/other\n")
        + b"0000PACK..."
    )
    updates, consumed = split_push_commands(body)
    assert updates == [
        RefUpdate(_ZERO, _OID, "refs/heads/feature"),
        RefUpdate(_OID, "b" * 40, "refs/heads/other"),
    ]
    assert body[consumed:] == b"PACK..."
    assert split_push_commands(body[:10]) is None
    with pytest.raises(PushRefused):
        split_push_commands(b"zzzz")


def test_a_push_may_write_branches_only_and_delete_nothing():
    check_push_commands(
        [RefUpdate(_ZERO, _OID, "refs/heads/new"), RefUpdate(_OID, "c" * 40, "refs/heads/old")]
    )
    for update in (
        RefUpdate(_OID, _ZERO, "refs/heads/dev"),
        RefUpdate(_ZERO, _OID, "refs/tags/v1"),
        RefUpdate(_ZERO, _OID, "refs/pull/1/head"),
    ):
        with pytest.raises(PushRefused):
            check_push_commands([update])
    with pytest.raises(PushRefused):
        check_push_commands([])
