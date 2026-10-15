"""Owner inference credentials reach the agent's commands, never the agent CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from cloud_agent.sandbox_manager_client import SandboxManagerClient, SandboxManagerError
from cloud_agent_host.chat_sandbox import (
    MEMBER_UID_BASE,
    ChatSandboxManager,
    ChatSandboxSpec,
    member_inference_env_path,
)
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)
from cloud_agent_shared.environment import (
    INFERENCE_NAMES,
    BoundEnvironment,
    EnvironmentVariable,
    inference_handoff,
    uses_inference_names,
    validate_name,
)


def _fake(label: str) -> str:
    """A placeholder credential, built so a secret scanner never sees a literal."""
    return f"{label}-{'0123456789' * 2}"


OWNER_OPENAI = _fake("owner-openai")
# What a CLI launch puts in its own environment: the platform's credential.
PLATFORM = {
    "OPENAI_API_KEY": _fake("platform-grant"),
    "OPENAI_BASE_URL": "http://gateway/v1",
    "ANTHROPIC_API_KEY": _fake("platform-grant"),
    "ANTHROPIC_BASE_URL": "http://gateway",
}


def _bound(*variables: EnvironmentVariable) -> BoundEnvironment:
    return BoundEnvironment(binding_id="binding", variables=list(variables))


@pytest.mark.parametrize("name", sorted(INFERENCE_NAMES))
def test_an_owner_may_set_an_inference_credential(name):
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CONFIG_DIR", "CODEX_API_KEY"]
)
def test_names_that_configure_the_agent_cli_stay_reserved(name):
    with pytest.raises(ValueError, match="reserved"):
        validate_name(name)


def _bash_sees(script: str, names: list[str]) -> dict[str, str | None]:
    """What a non-interactive bash started with the platform values reports."""
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    probe = "; ".join(f'printf "%s\\0" "${{{name}-<unset>}}"' for name in names)
    with subprocess.Popen(
        ["bash", "-c", f"source /dev/stdin <<'HANDOFF'\n{script}HANDOFF\n{probe}"],
        env={"PATH": "/usr/bin:/bin", **PLATFORM},
        stdout=subprocess.PIPE,
    ) as process:
        output, _ = process.communicate(timeout=10)
    values = output.decode().split("\0")[:-1]
    return {
        name: None if value == "<unset>" else value
        for name, value in zip(names, values, strict=True)
    }


def test_a_family_the_owner_set_is_entirely_the_owners():
    rest, script = inference_handoff({"OPENAI_API_KEY": OWNER_OPENAI, "APP_ENV": "development"})
    assert rest == {"APP_ENV": "development"}
    names = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"]
    assert _bash_sees(script, names) == {
        "OPENAI_API_KEY": OWNER_OPENAI,
        # Never left at the gateway's address with the owner's key.
        "OPENAI_BASE_URL": None,
        # A family the owner did not set is the platform's, as before.
        "ANTHROPIC_API_KEY": PLATFORM["ANTHROPIC_API_KEY"],
        "ANTHROPIC_BASE_URL": PLATFORM["ANTHROPIC_BASE_URL"],
    }


def test_values_are_quoted_and_never_executed(tmp_path):
    marker = tmp_path / "executed"
    hostile = f'it\'s "$(touch {marker})" `touch {marker}` $HOME\nsecond line'
    _rest, script = inference_handoff(
        {"ANTHROPIC_AUTH_TOKEN": hostile, "ANTHROPIC_BASE_URL": "https://api.example"}
    )
    seen = _bash_sees(script, ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"])
    assert seen == {
        "ANTHROPIC_AUTH_TOKEN": hostile,
        "ANTHROPIC_BASE_URL": "https://api.example",
        "ANTHROPIC_API_KEY": None,
    }
    assert not marker.exists()


def test_no_inference_credential_means_no_script():
    assert inference_handoff({"TOKEN": "private-example"}) == ({"TOKEN": "private-example"}, "")


def test_a_binding_or_its_json_names_inference_credentials():
    plain = _bound(EnvironmentVariable(name="TOKEN", value="private-example"))
    refreshed = plain.model_copy(
        update={
            "runtime_variables": [EnvironmentVariable(name="OPENAI_API_KEY", value=OWNER_OPENAI)]
        }
    )
    assert not uses_inference_names(plain)
    assert not uses_inference_names(plain.model_dump(mode="json"))
    assert uses_inference_names(refreshed)
    assert uses_inference_names(refreshed.model_dump(mode="json"))


def _launch_context(environment: BoundEnvironment | None):
    events: list[tuple] = []

    def spawn(spec, argv, **kwargs):
        events.append(("spawn", kwargs["env"]))
        return object()

    manager = SimpleNamespace(
        backend=SimpleNamespace(gid=10001, base_env=lambda: {}),
        reap_member_processes=lambda spec, **kwargs: None,
        write_member_inference_env=lambda spec, script, **kwargs: events.append(
            ("write", script, kwargs)
        ),
        spawn=spawn,
    )
    spec = ChatSandboxSpec(
        thread_id="athr_owner",
        sandbox_id="asbx_owner",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
    )
    launch = ResidentLaunch(
        thread_id="athr_owner",
        runtime_id="claude-code",
        runtime_version=None,
        native_session_id=None,
        model="model",
        reasoning_effort=None,
        gateway_base_url="http://gateway",
        credential="grant",
        slot_id=2,
        environment=environment,
    )
    context = ResidentTransportFactory(manager)._launch_context(spec, launch, credential="grant")
    return context, events


def test_the_cli_launch_keeps_owner_inference_credentials_for_its_bash():
    owner_key = _fake("owner-anthropic")
    context, events = _launch_context(
        _bound(
            EnvironmentVariable(name="ANTHROPIC_API_KEY", value=owner_key),
            EnvironmentVariable(name="APP_ENV", value="development", secret=False),
        )
    )
    assert "ANTHROPIC_API_KEY" not in context.base_env
    assert context.base_env["APP_ENV"] == "development"
    assert context.base_env["BASH_ENV"] == member_inference_env_path(2)
    context.spawn_live(["claude"], {**context.base_env, **PLATFORM})
    write, spawn = events
    assert write[0] == "write"
    assert f"export ANTHROPIC_API_KEY={owner_key}" in write[1]
    assert write[2] == {"slot_id": 2, "user_uid": MEMBER_UID_BASE + 2, "user_gid": 10001}
    # The CLI itself still authenticates with the platform's credential.
    assert spawn[1]["ANTHROPIC_API_KEY"] == PLATFORM["ANTHROPIC_API_KEY"]


@pytest.mark.parametrize("environment", [None, "plain"])
def test_a_launch_without_them_writes_nothing(environment):
    bound = _bound(EnvironmentVariable(name="TOKEN", value="private-example"))
    context, events = _launch_context(bound if environment else None)
    context.spawn_live(["claude"], context.base_env)
    assert "BASH_ENV" not in context.base_env
    assert [event[0] for event in events] == ["spawn"]


def test_the_script_crosses_stdin_into_the_member_tmpfs():
    calls = []

    def exec_capture(spec, argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    manager = SimpleNamespace(exec_capture=exec_capture)
    spec = ChatSandboxSpec(
        thread_id="athr_owner",
        sandbox_id="asbx_owner",
        generation=1,
        runtime_id="codex",
        shared_live=True,
    )
    script = f"unset OPENAI_API_KEY OPENAI_BASE_URL\nexport OPENAI_API_KEY={OWNER_OPENAI}\n"
    path = ChatSandboxManager.write_member_inference_env(
        manager, spec, script, slot_id=1, user_uid=MEMBER_UID_BASE + 1, user_gid=10001
    )
    assert path == member_inference_env_path(1)
    ((argv, kwargs),) = calls
    assert OWNER_OPENAI not in " ".join(argv)
    assert kwargs["input_bytes"] == script.encode()
    assert kwargs["user_uid"] == MEMBER_UID_BASE + 1
    with pytest.raises(ValueError, match="member uid"):
        ChatSandboxManager.write_member_inference_env(
            manager, spec, script, slot_id=1, user_uid=MEMBER_UID_BASE, user_gid=10001
        )


_OWNER_KEY = EnvironmentVariable(name="OPENAI_API_KEY", value=OWNER_OPENAI)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities,accepted",
    [(["environment-v1"], False), (["environment-v1", "environment-inference-v1"], True)],
)
async def test_an_older_broker_receives_no_inference_credential(capabilities, accepted):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"capabilities": capabilities})
        return httpx.Response(200, json={"id": "term"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        broker = SandboxManagerClient("http://broker", "credential", client=client)
        call = broker.create_terminal("workspace", rows=24, cols=80, environment=_bound(_OWNER_KEY))
        if accepted:
            await call
        else:
            with pytest.raises(SandboxManagerError, match="upgraded"):
                await call
    assert [request.url.path == "/health" for request in requests] == (
        [True, False] if accepted else [True]
    )


def test_the_runner_refuses_an_older_supervisor_before_sending_values():
    client = SessionSupervisorClient("http://sandbox_manager", "sandbox-manager-secret")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"capabilities": ["environment-v1"]})

    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(SessionSupervisorError, match="does not support"):
        client.prepare_environment("athr_owner", _bound(_OWNER_KEY))
    assert [request.url.path for request in requests] == ["/health"]


@pytest.mark.asyncio
@pytest.mark.parametrize("client_kind", ["control-plane", "runner"])
@pytest.mark.parametrize("inference_capable", [False, True])
@pytest.mark.parametrize("snapshot_capable", [False, True])
async def test_preparation_negotiates_inference_and_snapshot_capabilities_independently(
    client_kind, inference_capable, snapshot_capable
):
    bound = _bound(_OWNER_KEY).model_copy(
        update={"update_script": "npm run generate", "setup_fingerprint": "f" * 64}
    )
    capabilities = ["environment-v1"]
    if inference_capable:
        capabilities.append("environment-inference-v1")
    if snapshot_capable:
        capabilities.append("environment-snapshot-v1")
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"capabilities": capabilities})
        return httpx.Response(200, json={"status": "ready"})

    if client_kind == "control-plane":
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            broker = SandboxManagerClient("http://broker", "credential", client=client)
            if inference_capable:
                await broker.prepare_environment("workspace", bound)
            else:
                with pytest.raises(SandboxManagerError, match="upgraded"):
                    await broker.prepare_environment("workspace", bound)
    else:
        supervisor = SessionSupervisorClient("http://broker", "credential")
        supervisor._client.close()
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            supervisor._client = client
            if inference_capable:
                supervisor.prepare_environment("workspace", bound)
            else:
                with pytest.raises(SessionSupervisorError, match="does not support"):
                    supervisor.prepare_environment("workspace", bound)

    if not inference_capable:
        assert [request.url.path for request in requests] == ["/health"]
        return
    assert [request.url.path for request in requests] == [
        "/health",
        "/workspaces/workspace/environment/prepare",
    ]
    environment = json.loads(requests[-1].content)["environment"]
    assert environment["variables"][0]["value"] == OWNER_OPENAI
    if snapshot_capable:
        assert environment["update_script"] == "npm run generate"
        assert environment["setup_fingerprint"] == "f" * 64
    else:
        assert "update_script" not in environment
        assert "setup_fingerprint" not in environment
