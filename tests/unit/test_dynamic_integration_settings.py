"""Saved integration changes must reach clients that are already running."""

from __future__ import annotations

from cloud_agent import github_app, source_control
from cloud_agent.settings import number_setting


async def test_provider_timeouts_update_existing_credentials_and_keep_explicit_overrides(
    monkeypatch,
):
    values = {"github_request_timeout_s": 30.0, "gitlab_request_timeout_s": 20.0}
    for module in (github_app, source_control):
        monkeypatch.setattr(
            module, "number_setting", lambda name: values.get(name, number_setting(name))
        )
    implicit = github_app.GitHubAppCredentials(github_app.AppConfig("app", "unused-key"))
    explicit = github_app.GitHubAppCredentials(
        github_app.AppConfig("app", "unused-key"), timeout_s=8
    )
    gitlab = source_control.GitLabOAuthClient(
        source_control.GitLabOAuthConfig("client", "secret", "https://example.org/callback"),
        cipher=source_control.SourceControlCipher("test-key"),
    )
    async with implicit._http() as first:
        assert first.timeout.read == 30
    assert implicit.request_timeout_s == 30
    assert explicit.request_timeout_s == 8
    async with gitlab._client() as first_gitlab:
        assert first_gitlab.timeout.read == 20
    assert gitlab.refresh_lease_seconds == 50
    values.update(github_request_timeout_s=19.0, gitlab_request_timeout_s=23.0)
    async with implicit._http() as second:
        assert second is first
        assert second.timeout.read == 19
    assert implicit.request_timeout_s == 19
    async with explicit._http() as fixed:
        assert fixed.timeout.read == 8
    async with gitlab._client() as second_gitlab:
        assert second_gitlab.timeout.read == 23
    assert gitlab.refresh_lease_seconds == 53
    await implicit.aclose()
    await explicit.aclose()
