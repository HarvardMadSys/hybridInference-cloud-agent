"""Saved integration changes must reach clients that are already running."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from cloud_agent import gateway, github_app, grants, publisher, source_control


async def test_existing_gateway_and_grant_clients_use_new_connection_settings(monkeypatch):
    values = {
        "GATEWAY_BASE_URL": "https://first.example",
        "GATEWAY_GRANT_DISPATCH_TOKEN": "first-token",
        "GATEWAY_GRANT_BASE_PATH": "/internal/agent-grants",
    }

    def setting_env(name, default=None):
        return values.get(name, default)

    monkeypatch.setattr(gateway, "setting_env", setting_env)
    monkeypatch.setattr(grants, "setting_env", setting_env)
    requests = []

    def handle(request):
        requests.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        lookup = gateway.GatewayClient(client=http)
        grant = grants.GrantClient(client=http)
        first_binding = grant.binding_id
        await lookup.model_catalog("user")
        await grant.mint(user_id="user", job_id="job", attempt_id="attempt")
        values.update(
            GATEWAY_BASE_URL="https://second.example/",
            GATEWAY_GRANT_DISPATCH_TOKEN="second-token",
            GATEWAY_GRANT_BASE_PATH="/new/grants",
        )
        await lookup.model_catalog("user")
        await grant.renew("grant")
        assert grant.binding_id != first_binding
        assert requests == [
            ("https://first.example/internal/model-catalog?user_id=user", "Bearer first-token"),
            ("https://first.example/internal/agent-grants", "Bearer first-token"),
            ("https://second.example/internal/model-catalog?user_id=user", "Bearer second-token"),
            ("https://second.example/new/grants/grant/renew", "Bearer second-token"),
        ]

        values["GATEWAY_GRANT_BINDING_ID"] = "same-installation"
        explicit_binding = grant.binding_id
        values["GATEWAY_BASE_URL"] = "https://third.example"
        assert grant.binding_id == explicit_binding
        values["GATEWAY_GRANT_DISPATCH_TOKEN"] = ""
        assert not lookup.configured
        assert not grant.configured


def test_explicit_connection_configuration_is_not_replaced(monkeypatch):
    monkeypatch.setattr(gateway, "setting_env", lambda name, default=None: "changed")
    monkeypatch.setattr(grants, "setting_env", lambda name, default=None: "changed")
    lookup = gateway.GatewayClient("https://fixed.example", "fixed-token")
    grant = grants.GrantClient(
        "https://fixed.example", "fixed-token", base_path="/fixed", binding_id="fixed"
    )
    assert lookup._base_url == grant._base_url == "https://fixed.example"
    assert lookup._token == grant._token == "fixed-token"
    assert grant._base_path == "/fixed"
    assert not gateway.GatewayClient("", "fixed-token").configured


def test_fully_injected_grant_connection_keeps_inherited_path_and_binding(monkeypatch):
    values = {
        "GATEWAY_GRANT_BASE_PATH": "/first/grants",
        "GATEWAY_GRANT_BINDING_ID": "first-installation",
    }
    monkeypatch.setattr(grants, "setting_env", lambda name, default=None: values.get(name, default))
    client = grants.GrantClient("https://fixed.example", "fixed-token")
    first_binding = client.binding_id
    values.update(
        GATEWAY_GRANT_BASE_PATH="/second/grants", GATEWAY_GRANT_BINDING_ID="second-installation"
    )
    assert client._base_path == "/first/grants"
    assert client.binding_id == first_binding
    assert grants.GrantClient("https://fixed.example", "fixed-token").binding_id != first_binding


def test_git_identity_changes_on_the_next_git_command(monkeypatch):
    values = {
        "AGENT_GIT_AUTHOR_NAME": "First Author",
        "AGENT_GIT_AUTHOR_EMAIL": "first@example.org",
    }
    monkeypatch.setattr(
        publisher, "setting_env", lambda name, default=None: values.get(name, default)
    )
    environments = []

    def run(*args, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(publisher.subprocess, "run", run)
    publisher._run_git(["status"])
    values.update(
        AGENT_GIT_AUTHOR_NAME="Second Author", AGENT_GIT_AUTHOR_EMAIL="second@example.org"
    )
    publisher._run_git(["status"])
    assert environments[0]["GIT_AUTHOR_NAME"] == "First Author"
    assert (
        environments[1]["GIT_AUTHOR_NAME"]
        == environments[1]["GIT_COMMITTER_NAME"]
        == "Second Author"
    )
    assert environments[1]["GIT_AUTHOR_EMAIL"] == "second@example.org"
    assert environments[1]["GIT_CONFIG_GLOBAL"] == "/dev/null"


async def test_existing_gateway_and_grant_clients_use_new_timeout_settings(monkeypatch):
    values = {"gateway_request_timeout_s": 10.0, "gateway_connect_timeout_s": 5.0}
    for module in (gateway, grants):
        monkeypatch.setattr(
            module, "number_setting", lambda name, default=None: values.get(name, default)
        )
    timeouts = []

    def handle(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        lookup = gateway.GatewayClient("https://gateway.example", "token", client=http)
        grant = grants.GrantClient("https://gateway.example", "token", client=http)
        await lookup.model_catalog("user")
        values.update(gateway_request_timeout_s=23.0, gateway_connect_timeout_s=4.0)
        await lookup.model_catalog("user")
        await grant.renew("grant")
    assert timeouts[0]["read"] == 10.0
    assert timeouts[1]["read"] == timeouts[2]["read"] == 23.0
    assert timeouts[1]["connect"] == timeouts[2]["connect"] == 4.0


async def test_provider_timeouts_update_existing_credentials_and_keep_explicit_overrides(
    monkeypatch,
):
    values = {"github_request_timeout_s": 30.0, "gitlab_request_timeout_s": 20.0}
    for module in (github_app, source_control):
        monkeypatch.setattr(
            module, "number_setting", lambda name, default=None: values.get(name, default)
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
