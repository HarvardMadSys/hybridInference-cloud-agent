"""Environment inputs, frozen execution stages and streaming secret boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cloud_agent.source_control import SourceControlCipher, SourceControlError
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.environment import (
    ENVIRONMENT_DOMAIN,
    BoundEnvironment,
    EnvironmentAction,
    EnvironmentVariable,
    merge_variables,
    parse_env,
    validate_name,
)
from cloud_agent_shared.secret_redaction import SecretRedactor


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "DOCKER_HOST",
        "https_proxy",
        "AGENT_FAKE",
        "WORKSPACE_COMMAND",
        "GIT_CONFIG_COUNT",
    ],
)
def test_reserved_names_cannot_change_host_or_platform_credentials(name):
    with pytest.raises(ValueError):
        validate_name(name)


def test_phase_override_replaces_global_definition_before_filtering():
    global_value = EnvironmentVariable(name="TOKEN", value="global-token", phase="runtime")
    local = EnvironmentVariable(name="TOKEN", value="build-token", phase="setup")
    temporary = EnvironmentVariable(name="APP_ENV", value="test", secret=False)
    merged = merge_variables([global_value], [local], [temporary])
    bound = BoundEnvironment(binding_id="bound", variables=merged)
    assert bound.values("setup") == {"TOKEN": "build-token", "APP_ENV": "test"}
    assert bound.values("runtime") == {"APP_ENV": "test"}
    assert {v.name: v.source for v in bound.variables} == {
        "TOKEN": "environment",
        "APP_ENV": "override",
    }


def test_dotenv_is_atomic_and_never_interpolates_or_executes():
    values = parse_env(
        "export TOKEN='private value'\nURL=${TOKEN}\nCOMMAND=$(touch /tmp/should-not-exist)\nEMPTY=\n"
    )
    assert [v.value for v in values] == [
        "private value",
        "${TOKEN}",
        "$(touch /tmp/should-not-exist)",
        "",
    ]
    assert all(v.secret for v in values)
    for invalid in ("TOKEN=one\nTOKEN=two", "SAFE=x\nOPENAI_API_KEY=bad", "BROKEN='unterminated"):
        with pytest.raises((ValueError, ValidationError)):
            parse_env(invalid)


def test_effective_union_is_bounded_and_duplicate_input_is_rejected():
    with pytest.raises(ValueError):
        merge_variables([EnvironmentVariable(name=f"V{i}", value="v") for i in range(151)])
    with pytest.raises(ValueError):
        merge_variables([EnvironmentVariable(name=f"V{i}", value="x" * 32768) for i in range(4)])
    with pytest.raises(ValidationError):
        EnvironmentVariable(name="V", value="nul\0value")
    with pytest.raises(ValidationError):
        BoundEnvironment(
            binding_id="b", variables=[{"name": "V", "value": "one"}, {"name": "V", "value": "two"}]
        )


@pytest.mark.parametrize("cwd", ["/tmp", "../other", "nested/../../other", "nul\0path"])
def test_operation_directory_stays_inside_workspace(cwd):
    with pytest.raises(ValidationError):
        EnvironmentAction(id="test", name="Test", command="npm test", cwd=cwd)


def test_environment_cipher_is_not_a_source_control_cipher():
    environment = SourceControlCipher("test-key", domain=ENVIRONMENT_DOMAIN)
    source = SourceControlCipher("test-key")
    encrypted = environment.encrypt("example-private-value")
    assert environment.decrypt(encrypted) == "example-private-value"
    with pytest.raises(SourceControlError):
        source.decrypt(encrypted)


@pytest.mark.parametrize(
    "value",
    [
        "demo-secret-123456",
        "密钥demo-secret-123456",
        "short",
        "a",
        "first-line-secret\nsecond-line-secret",
    ],
)
def test_every_stream_chunk_boundary_redacts_known_values(value):
    for offset in range(1, len(value)):
        redactor = SecretRedactor({"TOKEN": value})
        output = redactor.feed("prefix " + value[:offset])
        output += redactor.feed(value[offset:] + " suffix")
        output += redactor.flush()
        assert value not in output
        assert "[secret:TOKEN]" in output
    redactor = SecretRedactor({"TOKEN": value})
    assert redactor.contains(b"\0binary" + value.encode() + b"\xff")


def test_redaction_preserves_control_fields_and_masks_nested_list_strings():
    redactor = SecretRedactor({"TOKEN": "demo-secret-123456"})
    payload = {
        "job_id": "demo-secret-123456",
        "detail": "demo-secret-123456",
        "nested": ["demo-secret-123456", {"text": "demo-secret-123456"}],
    }
    safe = redactor.payload(payload)
    assert safe["job_id"] == payload["job_id"]
    assert safe["detail"] == "[secret:TOKEN]"
    assert safe["nested"] == ["[secret:TOKEN]", {"text": "[secret:TOKEN]"}]


class _Process:
    def __init__(self, text="", code=0):
        self.text = text
        self.code = code
        self.killed = False

    def lines(self):
        yield self.text

    def wait(self):
        return self.code

    def stderr_text(self):
        return ""

    def kill(self):
        self.killed = True


class _Backend:
    def __init__(self):
        self.calls = []
        self.boot = "boot-one"
        self.fail = False
        self.spec = SimpleNamespace(container_name="container")
        self.manager = SimpleNamespace(
            inspect=lambda _: SimpleNamespace(
                container_id="exact-container", running=True, started_at=self.boot
            )
        )

    def spawn(self, spec):
        self.calls.append(spec)
        return _Process("demo-secret-123456", 1 if self.fail else 0)


def test_preparation_is_fixed_across_member_and_broker_restarts(tmp_path):
    backend = _Backend()
    environment = BoundEnvironment(
        binding_id="bound",
        setup_script="install",
        start_script="start",
        variables=[
            EnvironmentVariable(name="BUILD_TOKEN", value="demo-secret-123456", phase="setup"),
            EnvironmentVariable(name="RUNTIME_TOKEN", value="runtime-only-secret", phase="runtime"),
        ],
    )
    prepare = EnvironmentPreparation(tmp_path)
    first = prepare.prepare(backend, environment)
    assert len(backend.calls) == 2
    assert backend.calls[0].env == {"BUILD_TOKEN": "demo-secret-123456"}
    assert backend.calls[1].env == {"RUNTIME_TOKEN": "runtime-only-secret"}
    assert "demo-secret-123456" not in str(first)
    assert "demo-secret-123456" not in next(tmp_path.glob("*.json")).read_text()
    EnvironmentPreparation(tmp_path).prepare(backend, environment)
    assert len(backend.calls) == 2
    backend.boot = "boot-two"
    prepare.prepare(backend, environment)
    assert len(backend.calls) == 3 and backend.calls[-1].argv[-1] == "start"
    with pytest.raises(SandboxError):
        prepare.prepare(backend, environment.model_copy(update={"binding_id": "another"}))


def test_failed_installation_does_not_start_services_or_record_success(tmp_path):
    backend = _Backend()
    backend.fail = True
    environment = BoundEnvironment(binding_id="bound", setup_script="install", start_script="start")
    prepare = EnvironmentPreparation(tmp_path)
    with pytest.raises(SandboxError):
        prepare.prepare(backend, environment)
    assert len(backend.calls) == 1
    backend.fail = False
    result = prepare.prepare(backend, environment)
    assert len(backend.calls) == 3
    assert all(s["state"] == "succeeded" for s in result["steps"])


def test_masked_output_is_idempotent_and_user_data_ids_are_still_masked():
    redactor = SecretRedactor({"TOKEN": "secret"})
    masked = redactor.text("value: secret")
    assert redactor.text(masked) == masked == "value: [secret:TOKEN]"
    assert redactor.payload(
        {"native_session_id": "secret", "owner": "secret", "args": {"id": "secret"}}
    ) == {"native_session_id": "secret", "owner": "secret", "args": {"id": "[secret:TOKEN]"}}


def test_secret_request_fingerprint_is_keyed_and_default_fields_are_compatible(monkeypatch):
    from cloud_agent.domain.requests import request_fingerprint

    body = {
        "repo": "org/repo",
        "environment_overrides": [{"name": "TOKEN", "value": "simple-password"}],
    }
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "first-test-key")
    first = request_fingerprint("create", body, sensitive=True)
    assert first == request_fingerprint("create", body, sensitive=True)
    assert first != request_fingerprint("create", body)
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "second-test-key")
    assert request_fingerprint("create", body, sensitive=True) != first
    assert request_fingerprint("create", {"repo": "org/repo"}) == request_fingerprint(
        "create",
        {
            "repo": "org/repo",
            "environment_id": None,
            "environment_revision": None,
            "global_revision": None,
            "environment_overrides": [],
        },
    )


def test_trusted_publisher_checks_known_secret_bytes_and_allows_removal(tmp_path, monkeypatch):
    from cloud_agent import publisher

    monkeypatch.setattr(publisher, "_blob_size", lambda _repo, _sha: 64)
    content = {
        "new": b"\x00demo-private-binary\xff",
        "old": b"\x00demo-private-binary\xff",
        "deleted": b"\x00\xff",
    }
    monkeypatch.setattr(publisher, "_read_blob", lambda _repo, sha, _size: content[sha])
    with pytest.raises(publisher.PublishError) as rejected:
        publisher._validate_staged_blobs(
            tmp_path,
            [publisher._StagedBlob("asset.bin", "new", None)],
            {"TOKEN": "demo-private-binary"},
        )
    assert "demo-private-binary" not in str(rejected.value)
    publisher._validate_staged_blobs(
        tmp_path,
        [publisher._StagedBlob("asset.bin", "deleted", "old")],
        {"TOKEN": "demo-private-binary"},
    )


@pytest.mark.asyncio
async def test_environment_repository_reads_without_app_installation_and_never_write_scope(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    import httpx

    from cloud_agent import environment_source
    from cloud_agent.github_app import AppNotInstalled

    credentials = SimpleNamespace(token_for=AsyncMock(side_effect=AppNotInstalled("not installed")))
    requests = []

    def respond(request):
        requests.append(request)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        if "/git/trees/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "uv.lock", "type": "blob"},
                        {"path": "package-lock.json", "type": "blob"},
                        {"path": "sub/yarn.lock", "type": "blob"},
                    ]
                },
            )
        return httpx.Response(200, json={"default_branch": "main"})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        environment_source.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = await environment_source.script_suggestions("owner/public", None, credentials)
    assert result["commit"] == "a" * 40
    assert result["requires_choice"] is True
    assert {s["file"] for s in result["suggestions"]} == {"uv.lock", "package-lock.json"}
    assert all("Authorization" not in r.headers for r in requests)
    assert all(
        call.kwargs == {"permissions": {"contents": "read"}, "repository_scoped": True}
        for call in credentials.token_for.await_args_list
    )


@pytest.mark.asyncio
async def test_environment_metadata_size_limit_is_enforced_before_json(monkeypatch):
    import httpx
    from fastapi import HTTPException

    from cloud_agent import environment_source

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        environment_source.httpx,
        "AsyncClient",
        lambda **kwargs: factory(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
            ),
            **kwargs,
        ),
    )
    with pytest.raises(HTTPException) as caught:
        await environment_source.resolve_test_commit("owner/public", None, None)
    assert caught.value.status_code == 422
    assert "metadata exceeds size limit" in caught.value.detail


def test_wait_reads_foreground_failure_even_inside_poll_throttle():
    import threading
    import time
    from unittest.mock import Mock

    from cloud_agent_host.environment_process import EnvironmentProcess

    process = EnvironmentProcess.__new__(EnvironmentProcess)
    process._code = None
    process._next_poll = time.monotonic() + 60
    process._lock = threading.Lock()
    process.uid = 30000
    process.status_path = "/private/status"
    capture = Mock(return_value=SimpleNamespace(returncode=0, stdout="7"))
    process.backend = SimpleNamespace(
        spec=None, manager=SimpleNamespace(exec_capture=capture, backend=SimpleNamespace(gid=10001))
    )
    process.process = Mock()
    process.process.wait.return_value = 0
    assert process.poll() is None
    assert process.wait() == 7
    capture.assert_called_once()
    process.process.wait.assert_not_called()


def test_missing_completion_receipt_never_reports_success():
    import threading
    from unittest.mock import Mock

    from cloud_agent_host.environment_process import EnvironmentProcess

    process = EnvironmentProcess.__new__(EnvironmentProcess)
    process._code = None
    process._next_poll = 0
    process._lock = threading.Lock()
    process.uid = 30000
    process.status_path = "/private/status"
    process.backend = SimpleNamespace(
        spec=None,
        manager=SimpleNamespace(
            exec_capture=Mock(return_value=SimpleNamespace(returncode=1, stdout="")),
            backend=SimpleNamespace(gid=10001),
        ),
    )
    process.process = Mock()
    process.process.poll.return_value = 0
    with pytest.raises(SandboxError, match="receipt is missing"):
        process.wait()


def test_runtime_refresh_preserves_setup_and_scans_both_secret_generations():
    bound = BoundEnvironment(
        binding_id="fixed",
        setup_script="original",
        variables=[EnvironmentVariable(name="TOKEN", value="original-token")],
        runtime_variables=[
            EnvironmentVariable(name="TOKEN", value="rotated-token", phase="runtime")
        ],
        runtime_revision=1,
        previous_secrets={"TOKEN@previous": "previous-token"},
    )
    assert bound.values("setup") == {"TOKEN": "original-token"}
    assert bound.values("runtime") == {"TOKEN": "rotated-token"}
    redactor = SecretRedactor(bound.secrets())
    assert all(
        redactor.contains(v.encode()) for v in ("original-token", "rotated-token", "previous-token")
    )


def test_start_skip_does_not_mark_start_success_or_skip_future_turns(tmp_path):
    backend = _Backend()
    bound = BoundEnvironment(binding_id="fixed", setup_script="install", start_script="start")
    prepare = EnvironmentPreparation(tmp_path)
    prepare.prepare(backend, bound)
    backend.boot = "new-boot"
    calls = len(backend.calls)
    skipped = prepare.prepare(backend, bound.model_copy(update={"skip_start": True}))
    assert len(backend.calls) == calls
    assert skipped["steps"][-1]["state"] == "skipped"
    assert not prepare.status(backend, bound.binding_id)["ready"]
    backend.fail = True
    with pytest.raises(SandboxError, match="start failed"):
        prepare.prepare(backend, bound)
    assert len(backend.calls) == calls + 1
    assert prepare.status(backend, bound.binding_id)["steps"][-1]["state"] == "failed"


def test_environment_status_uses_bounded_inspection_cache(tmp_path):
    from unittest.mock import Mock

    backend = _Backend()
    original = backend.manager.inspect
    backend.manager.inspect = Mock(side_effect=original)
    prepare = EnvironmentPreparation(tmp_path)
    bound = BoundEnvironment(binding_id="b")
    prepare.prepare(backend, bound)
    for _ in range(20):
        assert prepare.status(backend, "b")["ready"]
    assert backend.manager.inspect.call_count == 1


def test_encryption_publication_failure_is_retryable():
    from cloud_agent.environments import EnvironmentEncryptionError
    from cloud_agent.publish_worker import _is_retryable_publication_failure

    assert _is_retryable_publication_failure(EnvironmentEncryptionError("temporary key failure"))


@pytest.mark.asyncio
async def test_environment_gc_is_hourly_but_lease_reaping_runs_every_tick():
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from cloud_agent.storage.repositories.environments import PostgresEnvironmentRepository

    connection = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(return_value=[]))

    @asynccontextmanager
    async def acquire():
        yield connection

    repository = PostgresEnvironmentRepository(SimpleNamespace(acquire=acquire))
    await repository.maintain_environments()
    await repository.maintain_environments()
    assert connection.execute.await_count == 2
    connection.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_metadata_uses_configured_github_api(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from cloud_agent import environment_source

    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"sha": "a" * 40})

    client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=transport, **kw))
    credentials = SimpleNamespace(
        config=SimpleNamespace(api_base="https://github.example.org/api/v3/"),
        token_for=AsyncMock(return_value="scoped-example"),
    )
    assert await environment_source._get("org/repo", "/commits/dev", credentials) == {
        "sha": "a" * 40
    }
    assert urls == ["https://github.example.org/api/v3/repos/org/repo/commits/dev"]


def test_environment_config_is_hidden_until_deployment_flag_is_enabled(monkeypatch):
    from cloud_agent.schemas import AgentConfigResponse

    monkeypatch.setenv("AGENT_REPOSITORY_ENVIRONMENTS_ENABLED", "false")
    assert AgentConfigResponse(github_load_status="ready").environment_limits is None
    monkeypatch.setenv("AGENT_REPOSITORY_ENVIRONMENTS_ENABLED", "true")
    assert (
        AgentConfigResponse(github_load_status="ready").environment_limits["capability"]
        == "environment-v1"
    )
