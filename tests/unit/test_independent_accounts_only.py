"""Cloud Agent has one account system, and models run only on owners' connections.

Gateway sign-in, Gateway admission and Gateway inference are gone, not hidden
behind a setting: their modules and routes do not exist, configuration left
over from them stops startup with the variables named, and a turn that names
no model connection is refused rather than run anywhere else.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from tests.support.owners import (
    CONNECTION_ID,
    CONNECTION_MODEL,
    FakeConnections,
    use_session_secret,
)

from cloud_agent.accounts import config
from cloud_agent.accounts.directory import (
    LocalDirectoryUnavailable,
    LocalOwnerDirectory,
    OwnerDirectoryError,
)
from cloud_agent.application.job_admission import _admit_model
from cloud_agent.application.model_connections import model_connections_enabled
from cloud_agent.bootstrap.identity import bind_identity_source
from cloud_agent.schemas import AgentJobCreate
from cloud_agent_host.retired_configuration import refuse_retired_host_configuration

pytestmark = pytest.mark.asyncio

_OWNER = {"user_id": "01J0000000000000000000OWNR"}


class _UntouchablePool:
    """A pool whose first use fails the test: refusal must come before the database."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the database was touched ({name}) before configuration was checked")


@pytest.mark.parametrize(
    "module",
    [
        "cloud_agent.gateway",
        "cloud_agent.identity",
        "cloud_agent.grants",
        "cloud_agent.visible_models",
        "cloud_agent.model_ttft",
        "cloud_agent.prompt_rewrite",
        "cloud_agent.api.prompt_rewrite",
        "cloud_agent.api.model_preferences",
        "cloud_agent.api.admin_llm_requests",
        "cloud_agent.api.admin_steps",
        "cloud_agent.api.service_jobs",
        "cloud_agent.application.grant_revocation",
        "cloud_agent.storage.repositories.grants",
        "cloud_agent.ports.grants",
        "cloud_agent.providers.compute.host_inference",
        "cloud_agent_host.inference",
    ],
)
async def test_no_gateway_module_remains(module):
    assert importlib.util.find_spec(module) is None


async def test_the_app_serves_no_gateway_route():
    from cloud_agent.app import create_app

    paths = set(create_app().openapi()["paths"])
    assert "/v1/session/me" in paths
    assert "/v1/session/logout" in paths
    for removed in (
        "/v1/session/login",
        "/v1/session/callback",
        "/v1/agent/model-preference",
        "/v1/agent/prompts/rewrite",
        "/v1/agent/jobs/{job_id}/attempts/{attempt_id}/messages/{message_id}/request",
        "/v1/agent/jobs/{job_id}/usage/refresh",
        "/v1/agent/jobs/{job_id}/thread/model-ttft",
    ):
        assert removed not in paths
    assert not [
        path
        for path in paths
        if "oncall" in path or "llm-request" in path or path.endswith("/model-ttft")
    ]
    assert not [path for path in paths if path.startswith("/admin/agent/steps")]


@pytest.mark.parametrize("variable", config.RETIRED_GATEWAY_VARIABLES)
async def test_startup_refuses_each_retired_variable_before_the_database(monkeypatch, variable):
    use_session_secret(monkeypatch)
    for name in config.RETIRED_GATEWAY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "https://gateway.example" if "URL" in variable else "x")
    with pytest.raises(config.RetiredConfiguration) as refused:
        await bind_identity_source(_UntouchablePool())
    message = str(refused.value)
    assert variable in message
    assert config.CUTOVER_RUNBOOK in message


async def test_a_leftover_local_mode_is_refused_not_accepted(monkeypatch):
    """There is no mode to select; accepting ``local`` would hide an unreviewed environment."""
    monkeypatch.setenv("AGENT_AUTH_MODE", "local")
    with pytest.raises(config.RetiredConfiguration, match="AGENT_AUTH_MODE"):
        config.refuse_retired_configuration()


async def test_the_old_sign_in_callback_names_its_replacement(monkeypatch):
    monkeypatch.setenv(
        "AGENT_SESSION_REDIRECT_URI", "https://agents.example/api/v1/session/callback"
    )
    with pytest.raises(config.RetiredConfiguration, match="AGENT_PUBLIC_API_URL"):
        config.refuse_retired_configuration()


async def test_empty_retired_variables_are_not_configuration(monkeypatch):
    for name in config.RETIRED_GATEWAY_VARIABLES:
        monkeypatch.setenv(name, "  ")
    config.refuse_retired_configuration()


async def test_startup_needs_the_account_key(monkeypatch):
    for name in config.RETIRED_GATEWAY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(config.ENV_LOCAL_SESSION_SECRET, raising=False)
    with pytest.raises(config.LocalAuthNotConfigured, match=config.ENV_LOCAL_SESSION_SECRET):
        await bind_identity_source(_UntouchablePool())


async def test_startup_needs_the_connection_key(monkeypatch):
    for name in config.RETIRED_GATEWAY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    use_session_secret(monkeypatch)
    monkeypatch.delenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(config.LocalAuthNotConfigured, match="AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"):
        await bind_identity_source(_UntouchablePool())


async def test_connections_are_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_CONNECTIONS_ENABLED", raising=False)
    assert model_connections_enabled() is True
    for off in ("false", "0", "no"):
        monkeypatch.setenv("AGENT_MODEL_CONNECTIONS_ENABLED", off)
        assert model_connections_enabled() is False


async def test_a_job_without_a_connection_is_a_verdict_not_an_outage():
    body = AgentJobCreate(repo=None, task_prompt="fix it", model="any-model")
    with pytest.raises(HTTPException) as refused:
        await _admit_model(body, _OWNER, connections=FakeConnections())
    assert refused.value.status_code == 409
    assert refused.value.detail["error"]["type"] == "model_connection_required"


async def test_a_job_on_the_owners_connection_is_admitted(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_CONNECTIONS_ENABLED", raising=False)
    body = AgentJobCreate(
        repo=None,
        task_prompt="fix it",
        model=CONNECTION_MODEL,
        model_connection_id=CONNECTION_ID,
    )
    assert await _admit_model(body, _OWNER, connections=FakeConnections()) == (
        CONNECTION_MODEL,
        None,
    )


async def test_the_directory_refuses_an_account_that_cannot_sign_in():
    class _Accounts:
        async def get_account(self, user_id):
            return None

    with pytest.raises(OwnerDirectoryError) as refused:
        await LocalOwnerDirectory(_Accounts()).agent_access("missing")
    assert (refused.value.status_code, refused.value.error_type) == (403, "subject_unavailable")


async def test_an_unreadable_store_is_no_verdict():
    class _Accounts:
        async def get_account(self, user_id):
            raise ConnectionError("database down")

    with pytest.raises(LocalDirectoryUnavailable) as refused:
        await LocalOwnerDirectory(_Accounts()).agent_access("anyone")
    assert refused.value.status_code == 503


@pytest.mark.parametrize("variable", ["AGENT_AUTH_MODE", "AGENT_GATEWAY_URL"])
async def test_a_host_refuses_the_retired_relay(variable):
    with pytest.raises(ValueError, match=variable):
        refuse_retired_host_configuration({variable: "x"})
    refuse_retired_host_configuration({variable: ""})


async def test_host_profiles_refuse_the_retired_relay():
    from cloud_agent.providers.compute.host_bootstrap import _retired_inference

    assert _retired_inference({"AGENT_TUNNEL_SSH_DESTINATION": "agent-inference@gateway"})
    assert _retired_inference({"AGENT_AUTH_MODE": "local"})
    assert not _retired_inference({"AGENT_TUNNEL_IMAGE": "ghcr.io/example/tunnel@sha256:0"})
    assert not _retired_inference({"AGENT_MCP_TUNNEL_SSH_DESTINATION": "agent-mcp@control"})


async def test_the_namespace_check_names_the_cutover():
    from cloud_agent import identity_namespace

    assert config.CUTOVER_RUNBOOK in identity_namespace.GATEWAY_BOUND
    assert config.CUTOVER_RUNBOOK in identity_namespace.UNBOUND_OWNER_DATA
    assert "untouched" in identity_namespace.GATEWAY_BOUND


async def test_the_namespace_check_on_an_empty_database_changes_nothing():
    from cloud_agent.identity_namespace import check_namespace

    class _Empty:
        async def fetchval(self, query, *args):
            return None

        async def fetch(self, query, *args):
            return []

        async def execute(self, *args):
            raise AssertionError("a check never writes")

    assert await check_namespace(_Empty()) is False


async def test_owner_directory_is_built_over_the_store_when_the_lifespan_did_not():
    from cloud_agent.api.deps import get_owner_directory

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(storage=SimpleNamespace(local_accounts=object())))
    )
    owners = get_owner_directory(request)
    assert isinstance(owners, LocalOwnerDirectory)
    assert get_owner_directory(request) is owners


async def test_owner_directory_without_a_store_refuses():
    from cloud_agent.api.deps import get_owner_directory, require_agent_access

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    owners = get_owner_directory(request)
    with pytest.raises(HTTPException) as refused:
        await require_agent_access(owners, "anyone")
    assert refused.value.status_code == 503
    with pytest.raises(OwnerDirectoryError):
        await owners.user_email("anyone")
