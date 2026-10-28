"""A translated member's launch: behind the helper, holding a token, never the provider key.

Codex (Responses) or Claude Code (Anthropic Messages) on a Chat Completions
connection: the adapters prefix the CLI with the helper's launcher, point it at
the loopback origin the launcher binds, and hand the provider URL and key only
to the launcher's environment. The token the CLI holds is the one the control
plane issued for its member, carried in the claim and the launch, part of the
process's reuse identity and masked by the runner like the key. A direct
plan's launch is unchanged.
"""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_agent_host.claimed_job import ClaimedJob, claim_secret_values
from cloud_agent_host.compat_helper import (
    HELPER_PATH,
    LAUNCHER,
    compat_token_env,
    helper_archive,
    install_compat_helper,
)
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig, resident_launch
from cloud_agent_host.resident_transport import ResidentTransport
from cloud_agent_host.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    ExactBaseUrl,
    KiloRuntime,
    MessagesBaseUrl,
    MessagesViaChatUrl,
    OpencodeRuntime,
    PiRuntime,
    ResponsesViaChatUrl,
    RuntimeConnectionUnsupportedError,
    connection_base_url,
)
from cloud_agent_shared.model_connections import ModelConnectionAccess
from cloud_agent_shared.protocol_compat import ORIGIN_PLACEHOLDER, TOKEN_PLACEHOLDER
from cloud_agent_shared.secret_redaction import SecretRedactor

_KEY = "fixture-chat-upstream-key-0123456789"
_URL = "https://chat.example/v1"
_CONNECTION = "mconn_" + "e" * 32
#: As the control plane would issue it (``translator_token``).
_TOKEN = "c" * 64


def test_the_plan_types_the_launch_url():
    assert type(connection_base_url(_URL, "chat_completions", "responses")) is ResponsesViaChatUrl
    assert type(connection_base_url(_URL, "chat_completions", "anthropic_messages")) is (
        MessagesViaChatUrl
    )
    # Direct plans are what they were before translation existed.
    assert type(connection_base_url(_URL, "chat_completions")) is ExactBaseUrl
    assert type(connection_base_url(_URL, "responses", "responses")) is ExactBaseUrl
    assert type(connection_base_url(_URL, "anthropic_messages")) is MessagesBaseUrl


def test_translated_codex_runs_behind_the_helper_with_only_a_token():
    argv, env = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="chat-model",
        gateway_base_url=ResponsesViaChatUrl(_URL),
        credential=_KEY,
        session_state_dir="/state/codex",
    )
    assert argv[: len(LAUNCHER)] == list(LAUNCHER) and argv[len(LAUNCHER)] == "codex"
    joined = " ".join(argv)
    assert _KEY not in joined and TOKEN_PLACEHOLDER not in joined
    assert f'base_url="{ORIGIN_PLACEHOLDER}/v1"' in joined
    assert 'web_search="disabled"' in joined
    # Sub-agent tools stay on: the helper carries namespaced functions.
    assert "multi_agent" not in joined
    assert env["CODEX_API_KEY"] == env["OPENAI_API_KEY"] == TOKEN_PLACEHOLDER
    assert env["OPENAI_BASE_URL"] == f"{ORIGIN_PLACEHOLDER}/v1"
    assert env["AGENT_COMPAT_CLIENT"] == "responses"
    assert env["AGENT_COMPAT_UPSTREAM_URL"] == _URL
    assert env["AGENT_COMPAT_UPSTREAM_KEY"] == _KEY
    assert env["AGENT_COMPAT_MODEL"] == "chat-model"


def test_direct_codex_is_unchanged_by_translation():
    argv, env = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="gpt-5.1-codex",
        gateway_base_url=ExactBaseUrl("https://api.example.com/v1"),
        credential=_KEY,
    )
    assert argv[0] == "codex" and "web_search" not in " ".join(argv)
    assert env["CODEX_API_KEY"] == _KEY
    assert not any(name.startswith("AGENT_COMPAT_") for name in env)


def test_translated_claude_code_keeps_its_model_slot_behind_the_launcher():
    argv, env = ClaudeCodeRuntime().prepare_live(
        workdir="/workspace",
        model="",
        gateway_base_url=MessagesViaChatUrl(_URL),
        credential=_KEY,
        config_dir="/state/claude",
    )
    assert argv[: len(LAUNCHER)] == list(LAUNCHER) and argv[len(LAUNCHER)] == "claude"
    # The resident driver pins the model by this slot; the launcher locks to it.
    slot = argv.index("--model") + 1
    argv[slot] = "chat-model"
    assert argv[slot - 1 : slot + 1] == ["--model", "chat-model"]
    assert argv[argv.index("--disallowedTools") + 1] == "WebSearch"
    assert _KEY not in " ".join(argv)
    assert env["ANTHROPIC_BASE_URL"] == ORIGIN_PLACEHOLDER
    assert env["ANTHROPIC_API_KEY"] == TOKEN_PLACEHOLDER
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["AGENT_COMPAT_CLIENT"] == "anthropic_messages"
    assert env["AGENT_COMPAT_UPSTREAM_KEY"] == _KEY
    assert env["AGENT_COMPAT_MODEL"] == ""


def test_direct_claude_code_on_messages_is_unchanged():
    argv, env = ClaudeCodeRuntime().prepare_live(
        workdir="/workspace",
        model="",
        gateway_base_url=MessagesBaseUrl("https://api.anthropic.example/v1"),
        credential=_KEY,
    )
    assert argv[0] == "claude" and "--disallowedTools" not in argv
    assert env["ANTHROPIC_API_KEY"] == _KEY


@pytest.mark.parametrize("runtime", [OpencodeRuntime(), KiloRuntime(), PiRuntime()])
@pytest.mark.parametrize("url", [ResponsesViaChatUrl(_URL), MessagesViaChatUrl(_URL)])
def test_no_chat_runtime_accepts_a_translated_url(runtime, url):
    with pytest.raises(RuntimeConnectionUnsupportedError):
        runtime.prepare_live(workdir="/w", model="m", gateway_base_url=url, credential=_KEY)


def test_codex_and_claude_refuse_each_others_translation():
    with pytest.raises(RuntimeConnectionUnsupportedError):
        CodexRuntime().prepare_live(
            workdir="/w", model="m", gateway_base_url=MessagesViaChatUrl(_URL), credential=_KEY
        )
    with pytest.raises(RuntimeConnectionUnsupportedError):
        ClaudeCodeRuntime().prepare_live(
            workdir="/w", model="", gateway_base_url=ResponsesViaChatUrl(_URL), credential=_KEY
        )


def _access(client: str = "responses") -> ModelConnectionAccess:
    return ModelConnectionAccess(
        connection_id=_CONNECTION,
        revision=4,
        base_url=_URL,
        protocol="chat_completions",
        client_protocol=client,
        api_key=_KEY,
        local_token=_TOKEN,
    )


def test_the_claim_masks_this_members_token_with_the_key():
    access = _access()
    job = ClaimedJob.from_response(
        {
            "job_id": "ajob_1",
            "thread_id": "athr_member",
            "attempt_id": 1,
            "attempt_no": 1,
            "repo": "owner/repo",
            "task_prompt": "do it",
            "runtime": "codex",
            "model": "chat-model",
            "worker_token": "w",
            "sandbox_token": "",
            "model_connection": access.model_dump(),
        }
    )
    assert job.model_connection is not None and job.model_connection.plan.translated
    secrets = claim_secret_values(job)
    assert {_KEY, _TOKEN} <= set(secrets.values())
    redacted = SecretRedactor(secrets).text(f"env printed {_TOKEN} and {_KEY}")
    assert _TOKEN not in redacted and _KEY not in redacted
    # The claim's repr shows neither.
    assert _TOKEN not in repr(job.model_connection) and _KEY not in repr(job.model_connection)


def test_the_launch_fields_carry_the_client_protocol_and_token_only_when_translated():
    fields = _access().launch_fields()
    assert (fields["inference_client_protocol"], fields["inference_local_token"]) == (
        "responses",
        _TOKEN,
    )
    direct = ModelConnectionAccess(
        connection_id=_CONNECTION,
        revision=4,
        base_url=_URL,
        protocol="chat_completions",
        api_key=_KEY,
    )
    assert "inference_client_protocol" not in direct.launch_fields()
    assert "inference_local_token" not in direct.launch_fields()
    assert "client_protocol" not in direct.model_dump()
    assert "local_token" not in direct.model_dump()
    for bad in (
        {"client_protocol": "chat_completions", "protocol": "responses"},
        {"client_protocol": "anthropic_messages", "protocol": "responses"},
        # A translated plan always has its member's token, a direct one never.
        {"client_protocol": "responses"},
        {"client_protocol": "responses", "local_token": "not-a-token"},
        {"local_token": _TOKEN},
    ):
        with pytest.raises(ValueError):
            ModelConnectionAccess(**{**direct.model_dump(), **bad})


def test_the_sandbox_manager_carries_the_plan_into_the_member_identity(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://10.0.0.1:8080")
    body = SharedResidentLaunchConfig(
        runtime_id="codex",
        model="chat-model",
        credential=_KEY,
        slot_id=2,
        **_access().launch_fields(),
    )
    launch = resident_launch("athr_member", body)
    assert launch.inference_client_protocol == "responses"
    assert launch.inference_local_token == _TOKEN and _TOKEN not in repr(launch)
    assert _TOKEN not in repr(body)
    with pytest.raises(ValueError):
        SharedResidentLaunchConfig(
            runtime_id="codex", model="m", slot_id=2, inference_client_protocol="chat_completions"
        )
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())
    assert transport.compatible_with(replace(launch, credential=None))
    # A plan change (here: the owner added Responses, so Codex goes direct) restarts it.
    assert not transport.compatible_with(
        replace(launch, inference_client_protocol="", inference_local_token="")
    )
    # A token the process does not hold (a rotated platform key, say) restarts
    # it, so a live process never emits a token this turn does not mask.
    assert not transport.compatible_with(replace(launch, inference_local_token="d" * 64))
    with pytest.raises(ValueError, match="translated launch"):
        replace(launch, inference_source="", inference_base_url="", inference_protocol="")
    with pytest.raises(ValueError, match="token"):
        replace(launch, inference_local_token="")


def test_the_token_reaches_only_a_translated_launch():
    assert compat_token_env(_TOKEN) == {"AGENT_COMPAT_TOKEN": _TOKEN}
    assert compat_token_env("") == {}


def test_the_helper_archive_is_deterministic_and_runs_on_the_image_interpreter(tmp_path):
    archive = helper_archive()
    assert archive == helper_archive.__wrapped__()
    names = zipfile.ZipFile(io.BytesIO(archive)).namelist()
    assert names[0] == "__main__.py"
    assert "cloud_agent_host/protocol_translator.py" in names
    assert "cloud_agent_shared/protocol_compat/LICENSE.hybridinference" in names
    assert "cloud_agent_shared/protocol_compat/NOTICE.md" in names
    # Of the host package only the translator, which imports nothing of it.
    assert [n for n in names if n.startswith("cloud_agent_host/")] == [
        "cloud_agent_host/__init__.py",
        "cloud_agent_host/protocol_translator.py",
    ]
    archive_zip = zipfile.ZipFile(io.BytesIO(archive))
    for name in names:
        if name.endswith(".py") and name != "__main__.py":
            for module in _imported(archive_zip.read(name), name):
                top = module.split(".")[0]
                assert top in sys.stdlib_module_names or module.startswith(
                    "cloud_agent_shared.protocol_compat"
                ), (name, module)
    assert HELPER_PATH == "/usr/local/libexec/model-protocol-translator.pyz"
    path = tmp_path / "helper.pyz"
    path.write_bytes(archive)
    run = subprocess.run(
        ["/usr/bin/python3", "-I", str(path)], capture_output=True, text=True, timeout=30
    )
    # No CLI and no launch: it refuses, naming nothing secret.
    assert run.returncode == 70 and "no CLI" in run.stderr


def _imported(source: bytes, name: str) -> list[str]:
    """Every absolute module a source file imports; relative imports stay in its package."""
    modules: list[str] = []
    for node in ast.walk(ast.parse(source, filename=name)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            modules.append(node.module)
    return modules


class _Manager:
    """Just the calls ``install_trusted_file`` makes."""

    def __init__(self, probe_ok: bool) -> None:
        self.probe_ok = probe_ok
        self.copied: list[bytes] = []
        self.commands: list[list[str]] = []
        self.backend = SimpleNamespace(docker_binary="docker")

    def _require(self, spec):
        return SimpleNamespace(container_id="container-1")

    def exec_capture(self, spec, argv, **kwargs):
        self.commands.append(argv)
        assert kwargs["user_uid"] == 0
        ok = self.probe_ok or "mv" in argv[2]
        return SimpleNamespace(returncode=0 if ok else 1)

    def _run_bytes(self, argv, *, timeout, input_bytes):
        self.copied.append(input_bytes)
        return SimpleNamespace(returncode=0)


def test_a_retained_container_gets_this_hosts_helper_only_when_it_differs():
    spec = SimpleNamespace(container_name="sandbox-1")
    current = _Manager(probe_ok=True)
    install_compat_helper(current, spec)
    assert current.copied == []
    stale = _Manager(probe_ok=False)
    install_compat_helper(stale, spec)
    assert len(stale.copied) == 1
    # The probe compares this host's digest; the copy is renamed into place as root.
    probe, activate = stale.commands
    assert HELPER_PATH in probe and "0:0:444" not in probe
    assert activate[-2] == HELPER_PATH and activate[-1] == "444"
