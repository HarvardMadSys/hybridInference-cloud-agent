"""A turn on an owner's model connection reaches its CLI with that connection, and only it.

The host half: the claim carries the connection, the runner masks its key,
the resident launch fixes its identity so a rotated key restarts the
process, the sandbox manager never replaces its URL with the Gateway relay,
and each runtime adapter puts the exact URL and key where its CLI reads them
-- never in an argument, and never in another protocol's spelling. Claude
Code on an Anthropic Messages connection is in
``test_model_connection_anthropic_launch``.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cloud_agent_host.claimed_job import (
    ClaimedJob,
    claim_secret_values,
    model_connection_launch,
    require_residency_connection,
)
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig, resident_launch
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransport
from cloud_agent_host.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    ExactBaseUrl,
    KiloRuntime,
    OpencodeRuntime,
    PiRuntime,
    RuntimeConnectionUnsupportedError,
)
from cloud_agent_shared.model_connections import (
    MODEL_CONNECTION_CAPABILITY,
    ModelConnectionAccess,
    runtime_protocol,
)
from cloud_agent_shared.secret_redaction import SecretRedactor

_KEY = "sk-owner-secret-0123456789"
_CONNECTION = "mconn_" + "c" * 32
_ACCESS = ModelConnectionAccess(
    connection_id=_CONNECTION,
    revision=3,
    base_url="https://generativelanguage.example/v1beta/openai",
    protocol="chat_completions",
    api_key=_KEY,
)


def _claim_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 1,
        "attempt_no": 1,
        "repo": "owner/repo",
        "task_prompt": "do it",
        "runtime": "opencode",
        "model": "gemini-2.5-pro",
        "worker_token": "w",
    }
    body.update(changes)
    return body


def test_the_capability_and_protocol_matrix_are_shared_vocabulary():
    assert MODEL_CONNECTION_CAPABILITY == "model-connections-v1"
    runtimes = ("opencode", "kilo", "pi", "codex", "claude-code")
    assert {name: runtime_protocol(name) for name in runtimes} == {
        "opencode": "chat_completions",
        "kilo": "chat_completions",
        "pi": "chat_completions",
        "codex": "responses",
        "claude-code": "anthropic_messages",
    }
    assert runtime_protocol("not-a-runtime") is None


def test_access_never_shows_its_key_and_refuses_what_a_config_cannot_carry():
    assert _KEY not in repr(_ACCESS)
    assert _ACCESS.source == f"{_CONNECTION}@3"
    assert _ACCESS.launch_fields() == {
        "inference_base_url": "https://generativelanguage.example/v1beta/openai",
        "inference_protocol": "chat_completions",
        "inference_source": f"{_CONNECTION}@3",
    }
    for bad in (
        {"base_url": "http://plain.example/v1"},
        {"api_key": "has space"},
        {"api_key": "line\nbreak"},
        {"protocol": "anthropic"},
        {"protocol": "messages"},
        {"connection_id": "mconn_notours"},
    ):
        with pytest.raises(ValidationError):
            ModelConnectionAccess(**{**_ACCESS.model_dump(), **bad})


def test_the_claim_carries_the_connection_and_the_runner_masks_its_key():
    job = ClaimedJob.from_response(_claim_body(model_connection=_ACCESS.model_dump()))
    assert job.model_connection == _ACCESS
    assert _KEY not in repr(job)

    secrets = claim_secret_values(job)
    assert _KEY in secrets.values()
    redacted = SecretRedactor(secrets).text(f"the agent printed {_KEY} in its output")
    assert _KEY not in redacted

    gateway = ClaimedJob.from_response(_claim_body())
    assert gateway.model_connection is None
    assert claim_secret_values(gateway) == {}
    assert model_connection_launch(gateway) == {}


def test_a_residency_must_name_the_revision_the_claim_named():
    job = ClaimedJob.from_response(_claim_body(model_connection=_ACCESS.model_dump()))
    direct = "chat_completions>chat_completions"
    require_residency_connection(
        job, {"model_connection_revision": 3, "model_connection_plan": direct}
    )
    with pytest.raises(ValueError, match="changed"):
        require_residency_connection(
            job, {"model_connection_revision": 4, "model_connection_plan": direct}
        )
    with pytest.raises(ValueError):
        require_residency_connection(job, {"model_connection_revision": None})

    gateway = ClaimedJob.from_response(_claim_body())
    require_residency_connection(gateway, {})
    with pytest.raises(ValueError):
        require_residency_connection(gateway, {"model_connection_revision": 3})


@pytest.mark.parametrize(
    ("claimed", "resolved"),
    [
        # Claimed native on a host without the helper; re-verified to translated.
        ({"protocol": "responses"}, "responses>chat_completions"),
        # Claimed translated; re-verified to native.
        (
            {
                "protocol": "chat_completions",
                "client_protocol": "responses",
                "local_token": "a" * 64,
            },
            "responses>responses",
        ),
        # A plan the residency never names is not assumed.
        ({"protocol": "responses"}, None),
    ],
)
def test_a_residency_must_name_the_plan_the_claim_named(claimed, resolved):
    """Codex review 1: same revision, but a verification moved the model's path."""
    access = ModelConnectionAccess(**{**_ACCESS.model_dump(), **claimed})
    job = ClaimedJob.from_response(_claim_body(model_connection=access.model_dump()))
    require_residency_connection(
        job, {"model_connection_revision": 3, "model_connection_plan": access.plan.identity}
    )
    with pytest.raises(ValueError, match="changed"):
        require_residency_connection(
            job, {"model_connection_revision": 3, "model_connection_plan": resolved}
        )


def _launch(**changes: object) -> ResidentLaunch:
    fields: dict[str, object] = {
        "thread_id": "athr_1",
        "runtime_id": "opencode",
        "runtime_version": None,
        "native_session_id": "native-1",
        "model": "gemini-2.5-pro",
        "reasoning_effort": None,
        "gateway_base_url": "http://relay.local:8080",
        "credential": None,
        **_ACCESS.launch_fields(),
    }
    fields.update(changes)
    return ResidentLaunch(**fields)  # type: ignore[arg-type]


def test_a_rotated_key_or_a_source_switch_never_borrows_the_live_process():
    launch = _launch(credential=_KEY)
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())

    # The probe carries no credential; the source decides.
    assert transport.compatible_with(replace(launch, credential=None))
    assert not transport.compatible_with(
        replace(launch, inference_source=f"{_CONNECTION}@4", credential=None)
    )
    assert not transport.compatible_with(
        replace(launch, inference_base_url="https://other.example/v1", credential=None)
    )
    gateway = replace(launch, inference_base_url="", inference_protocol="", inference_source="")
    assert not transport.compatible_with(gateway)
    assert not ResidentTransport(launch=gateway, session=SimpleNamespace()).compatible_with(
        replace(launch, credential=None)
    )


def test_a_connection_launch_names_all_of_its_identity_or_none():
    with pytest.raises(ValueError, match="model connection launch"):
        _launch(inference_source="")
    with pytest.raises(ValueError, match="model connection launch"):
        _launch(inference_base_url="")


def test_the_sandbox_manager_keeps_the_connection_url_and_replaces_only_the_relays(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://10.0.0.1:8080")
    body = SharedResidentLaunchConfig(
        runtime_id="opencode",
        model="gemini-2.5-pro",
        credential=_KEY,
        slot_id=1,
        **_ACCESS.launch_fields(),
    )
    launch = resident_launch("athr_1", body)

    assert launch.gateway_base_url == "http://10.0.0.1:8080"
    assert launch.inference_base_url == _ACCESS.base_url
    assert launch.inference_source == _ACCESS.source
    assert _KEY not in repr(body)
    assert _KEY not in repr(launch)
    for bad in (
        {"inference_base_url": "http://plain.example/v1"},
        {"inference_protocol": "anthropic"},
        {"inference_source": "mconn_x@1"},
    ):
        with pytest.raises(ValidationError):
            SharedResidentLaunchConfig(
                runtime_id="opencode", model="m", slot_id=1, **{**_ACCESS.launch_fields(), **bad}
            )


@pytest.mark.parametrize("runtime", [OpencodeRuntime(), KiloRuntime(), PiRuntime()])
def test_chat_completions_runtimes_get_the_exact_url_and_key_in_their_environment(runtime):
    argv, env = runtime.prepare_live(
        workdir="/workspace",
        model="gemini-2.5-pro",
        gateway_base_url=ExactBaseUrl(_ACCESS.base_url),
        credential=_KEY,
    )
    # Not normalized to ``/v1`` the way the Gateway relay's address is.
    assert env["OPENAI_BASE_URL"] == "https://generativelanguage.example/v1beta/openai"
    assert env["OPENAI_API_KEY"] == _KEY
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env
    assert all(_KEY not in arg for arg in argv)


@pytest.mark.parametrize("runtime", [OpencodeRuntime(), PiRuntime()])
def test_the_gateway_relay_is_still_normalized_and_given_both_spellings(runtime):
    _argv, env = runtime.prepare_live(
        workdir="/workspace",
        model="glm-5.1",
        gateway_base_url="http://relay.local:8080",
        credential="grant",
    )
    assert env["OPENAI_BASE_URL"] == "http://relay.local:8080/v1"
    assert env["ANTHROPIC_BASE_URL"] == "http://relay.local:8080"


def test_codex_points_its_provider_at_the_exact_url_with_the_key_in_the_environment():
    argv, env = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="gpt-5.1-codex",
        gateway_base_url=ExactBaseUrl("https://api.example.com/openai/v1"),
        credential=_KEY,
    )
    joined = " ".join(argv)
    assert 'model_providers.hybridinference.base_url="https://api.example.com/openai/v1"' in joined
    assert 'wire_api="responses"' in joined
    assert env["CODEX_API_KEY"] == _KEY
    assert _KEY not in joined


def test_claude_code_refuses_a_connection_that_does_not_speak_anthropic_messages():
    # The protocol, not the runtime, is what it lacks: an OpenAI-only URL.
    with pytest.raises(RuntimeConnectionUnsupportedError, match="does not offer"):
        ClaudeCodeRuntime().prepare_live(
            workdir="/workspace",
            model="claude-sonnet",
            gateway_base_url=ExactBaseUrl("https://openai-compatible.example/v1"),
            credential=_KEY,
        )
