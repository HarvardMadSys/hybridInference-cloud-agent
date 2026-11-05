"""Sandbox MCP servers, from a saved connection to the member's CLI config."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.support.owners import launch_connection

from cloud_agent.mcp import sandbox_servers
from cloud_agent.mcp.launch import LaunchPlanError, build_launch_plan
from cloud_agent.mcp.models import (
    AuthKind,
    ConnectionConfig,
    ConnectionStatus,
    ExecutionLocation,
    LaunchSpec,
    McpConnection,
    Mount,
    ThreadBinding,
    Transport,
)
from cloud_agent.mcp.registry import ResolvedConnection
from cloud_agent.schemas.workers import WorkerClaimResponse
from cloud_agent_host import sandbox_mcp
from cloud_agent_host.chat_sandbox_models import MCP_RUNNER_UID_BASE, MCP_SERVICE_UID
from cloud_agent_host.claimed_job import ClaimedJob
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig, resident_launch
from cloud_agent_host.resident_transport import (
    ResidentLaunch,
    ResidentTransport,
    ResidentTransportFactory,
)
from cloud_agent_host.runtimes.base import AgentRuntime, RuntimeMCPConfig
from cloud_agent_host.runtimes.codex.adapter import CodexRuntime
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_shared.sandbox_mcp import (
    SANDBOX_MCP_URL,
    SandboxMcpServer,
    parse_sandbox_mcp_servers,
)

PLAYWRIGHT = SandboxMcpServer(
    alias="playwright",
    command="npx",
    args=("-y", "@playwright/mcp@0.0.83", "--cdp-endpoint", "${CLOUD_AGENT_BROWSER_CDP}"),
)


# -- the launch policy --------------------------------------------------------------


def _plan(command, *args, env=None, mounts=(), cwd=None):
    spec = LaunchSpec(command=command, args=tuple(args), env=env or {}, mounts=mounts, cwd=cwd)
    return build_launch_plan(spec, connection_id="c", location=ExecutionLocation.SANDBOX)


def test_a_sandbox_server_is_one_exactly_pinned_package():
    assert _plan("npx", "-y", "@playwright/mcp@0.0.83").install.version == "0.0.83"
    assert _plan("uvx", "mcp-server-fetch==2025.4.7").install.package == "mcp-server-fetch"


@pytest.mark.parametrize(
    ("command", "args", "kind"),
    [
        ("npx", ("-y", "@playwright/mcp"), "unpinned_package"),
        ("npx", ("-y", "@playwright/mcp@latest"), "unpinned_package"),
        ("npx", ("pkg@^1.2.0",), "unpinned_package"),
        ("uvx", ("pkg>=1.0",), "unpinned_package"),
        ("node", ("server.js",), "launch_failed"),
        ("/usr/local/bin/npx", ("pkg@1.0.0",), "launch_failed"),
        ("bash", ("-c", "npx pkg@1.0.0"), "launch_failed"),
    ],
)
def test_anything_else_is_refused(command, args, kind):
    with pytest.raises(LaunchPlanError) as refused:
        _plan(command, *args)
    assert refused.value.kind == kind


def test_a_sandbox_server_takes_keys_but_no_mount_or_working_directory():
    keyed = _plan("npx", "pkg@1.0.0", env={"TOKEN": "secret://TOKEN", "HOME_URL": "${SITE}"})
    assert keyed.env == {"TOKEN": "secret://TOKEN", "HOME_URL": "${SITE}"}
    with pytest.raises(LaunchPlanError):
        _plan("npx", "pkg@1.0.0", mounts=(Mount(source="docs", target="/workspace/mcp/docs"),))
    with pytest.raises(LaunchPlanError):
        _plan("npx", "pkg@1.0.0", cwd="/workspace")
    assert _plan("npx", "pkg@1.0.0", env={"MODE": "plain"}).env == {"MODE": "plain"}


@pytest.mark.parametrize("template", sandbox_servers.TEMPLATES, ids=lambda t: t["name"])
def test_each_sandbox_template_passes_the_policy_it_is_held_to(template):
    assert template["location"] == ExecutionLocation.SANDBOX.value
    assert template["auth_kind"] == AuthKind.NONE.value
    plan = _plan(template["launch"]["command"], *template["launch"]["args"])
    assert plan.install is not None
    assert "${CLOUD_AGENT_BROWSER_CDP}" in template["launch"]["args"]


def test_a_program_has_no_sign_in_only_environment_values():
    sandbox = ExecutionLocation.SANDBOX
    assert sandbox_servers.refusal(sandbox, AuthKind.NONE) is None
    assert sandbox_servers.refusal(sandbox, "none") is None
    assert "environment values" in sandbox_servers.refusal(sandbox, AuthKind.STATIC)
    assert sandbox_servers.refusal(sandbox, AuthKind.OAUTH)
    assert sandbox_servers.refusal(ExecutionLocation.REMOTE, AuthKind.STATIC) is None


# -- what a claim carries -------------------------------------------------------------


def _resolved(location, *, launch=None, url=None, status=ConnectionStatus.ACTIVE, error=None):
    transport = (
        Transport.STDIO if location is not ExecutionLocation.REMOTE else Transport.STREAMABLE_HTTP
    )
    connection = McpConnection(
        connection_id="7",
        user_id="u",
        label="x",
        config=ConnectionConfig(transport=transport, location=location, url=url, launch=launch),
        status=status,
    )
    return ResolvedConnection(connection=connection, error=error)


def _binding(alias, *, enabled=True):
    return ThreadBinding(
        thread_id="t",
        connection_id="7",
        alias=alias,
        config_revision=1,
        authorization_epoch=1,
        capability_revision=1,
        enabled=enabled,
    )


def test_a_claim_sends_sandbox_servers_to_the_host_and_the_rest_to_the_relay():
    pinned = LaunchSpec(command="npx", args=("-y", "@playwright/mcp@0.0.83"))
    stale = LaunchSpec(command="npx", args=("-y", "@playwright/mcp@latest"))
    bindings = [
        (_resolved(ExecutionLocation.REMOTE, url="https://example.com/mcp"), _binding("github")),
        (_resolved(ExecutionLocation.SANDBOX, launch=pinned), _binding("browser")),
        (_resolved(ExecutionLocation.SANDBOX, launch=pinned), _binding("off", enabled=False)),
        (_resolved(ExecutionLocation.SANDBOX, launch=stale), _binding("stale")),
        (
            _resolved(ExecutionLocation.SANDBOX, launch=pinned, status=ConnectionStatus.ERROR),
            _binding("failing"),
        ),
    ]

    aliases, servers = sandbox_servers.split_bindings(bindings)

    assert aliases == ["github"]
    assert [server.alias for server in servers] == ["browser"]
    assert servers[0].args == ("-y", "@playwright/mcp@0.0.83")


def test_the_claim_schema_and_the_host_agree_on_the_servers():
    wire = [PLAYWRIGHT.model_dump(mode="json")]
    field = WorkerClaimResponse.model_fields["sandbox_mcp_servers"]
    assert field.default_factory() == []
    assert parse_sandbox_mcp_servers(wire) == (PLAYWRIGHT,)
    assert parse_sandbox_mcp_servers(None) == ()
    with pytest.raises(ValueError):
        parse_sandbox_mcp_servers([*wire, *wire])
    # A key is named in ``env`` and carried, by the claim alone, in ``secret_env``.
    keyed = {**wire[0], "env": {"T": "secret://T"}, "secret_env": {"T": "v"}}
    assert parse_sandbox_mcp_servers([keyed])[0].secret_env == {"T": "v"}
    with pytest.raises(ValueError):
        parse_sandbox_mcp_servers([{**wire[0], "env": {"T": "secret://T"}}])
    with pytest.raises(ValueError):
        parse_sandbox_mcp_servers([{**wire[0], "secret_env": {"T": "v"}}])
    with pytest.raises(ValueError):
        parse_sandbox_mcp_servers([{**wire[0], "command": "bash"}])
    assert hasattr(ClaimedJob, "__dataclass_fields__")
    assert "sandbox_mcp_servers" in ClaimedJob.__dataclass_fields__


def test_a_shared_launch_carries_the_servers_only_with_mcp():
    base = {
        "runtime_id": "claude-code",
        "model": "m",
        "slot_id": 1,
        **launch_connection("claude-code"),
        "sandbox_mcp_servers": [PLAYWRIGHT.model_dump(mode="json")],
    }
    with_mcp = resident_launch("t", SharedResidentLaunchConfig(**base, mcp_server_ids=["memory"]))
    without = resident_launch("t", SharedResidentLaunchConfig(**base))
    assert with_mcp.sandbox_mcp_servers == (PLAYWRIGHT,)
    assert without.sandbox_mcp_servers == ()


# -- the host's admission ----------------------------------------------------------------


class _Manager:
    def __init__(self, fail_at=None, status=1, error=None):
        self.backend = SimpleNamespace(gid=10001)
        self.fail_at = fail_at
        self.status = status
        self.error = error
        self.calls = []

    def exec_capture(self, spec, argv, **kwargs):
        self.calls.append((argv[1:], kwargs))
        if self.fail_at is not None and argv[1] == self.fail_at:
            if self.error is not None:
                raise self.error
            return subprocess.CompletedProcess(argv, self.status, b"", b"refused")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture(autouse=True)
def _fresh_host(monkeypatch):
    """A host that has just started: it knows no slot to be clean."""
    monkeypatch.setattr(sandbox_mcp, "_known_clean", set())


def _spec(name="chat-1"):
    return SimpleNamespace(container_name=name, sandbox_id=f"sandbox-{name}")


def _steps(manager):
    return [(argv, kwargs["user_uid"]) for argv, kwargs in manager.calls]


def test_the_host_remembers_whose_credential_it_admitted(monkeypatch):
    """A failure the runner journals under a digest is that member's, whoever has the slot now."""
    from cloud_agent_host.sandbox_incidents import MemberCredentials

    members = MemberCredentials()
    monkeypatch.setattr(sandbox_mcp, "MEMBER_CREDENTIALS", members)
    digest = hashlib.sha256(b"cred").hexdigest()

    sandbox_mcp.admit_sandbox_mcp(
        _Manager(), _spec(), slot_id=2, credential="cred", servers=(PLAYWRIGHT,), thread_id="t-1"
    )
    assert members.thread("sandbox-chat-1", digest) == "t-1"
    assert members.slots("sandbox-chat-1") == {2}
    # A revoke admits nobody, and a launch with no thread names nobody.
    sandbox_mcp.admit_sandbox_mcp(_Manager(), _spec("other"), slot_id=0, credential="c", servers=())
    sandbox_mcp.admit_sandbox_mcp(
        _Manager(), _spec("third"), slot_id=0, credential="c", servers=(PLAYWRIGHT,)
    )
    assert members.slots("sandbox-other") == set() and members.slots("sandbox-third") == set()


def test_the_host_admits_as_root_then_starts_the_gateway_then_the_slots_runner():
    manager = _Manager()

    assert sandbox_mcp.admit_sandbox_mcp(
        manager, _spec(), slot_id=2, credential="cred", servers=(PLAYWRIGHT,)
    )

    assert _steps(manager) == [
        (["admit", "2"], 0),
        # The gateway holds the fixed address before any server can start.
        (["service", "start"], MCP_SERVICE_UID),
        (["runner", "start", "2"], MCP_RUNNER_UID_BASE + 2),
    ]
    admit_kwargs = manager.calls[0][1]
    assert admit_kwargs["user_gid"] == 0
    assert json.loads(admit_kwargs["input_bytes"]) == {
        "token_sha256": hashlib.sha256(b"cred").hexdigest(),
        "servers": [PLAYWRIGHT.admission()],
    }
    assert b"cred" not in admit_kwargs["input_bytes"]
    assert json.loads(manager.calls[2][1]["input_bytes"]) == {"secrets": {"playwright": {}}}


def test_a_programs_keys_reach_its_runner_and_never_the_admission():
    """Every slot can read the admission; only the slot's runner gets a value."""
    keyed = SandboxMcpServer(
        alias="search",
        command="uvx",
        args=("search-mcp==1.0.0", "--site", "${SITE_URL}"),
        env={"API_KEY": "secret://API_KEY", "REGION": "${REGION}"},
        secret_env={"API_KEY": "sealed-key"},
    )
    manager = _Manager()

    assert sandbox_mcp.admit_sandbox_mcp(
        manager,
        _spec(),
        slot_id=1,
        credential="cred",
        servers=(keyed,),
        environment={"SITE_URL": "https://a.example", "REGION": "eu", "OTHER": "unrelated"},
        mcp_base_url="https://cp.example/v1",
    )

    admission = manager.calls[0][1]["input_bytes"]
    assert b"sealed-key" not in admission and b"https://a.example" not in admission
    assert json.loads(admission)["report_url"] == "https://cp.example/v1/agent/mcp"
    assert json.loads(admission)["servers"][0]["env"] == {
        "API_KEY": "secret://API_KEY",
        "REGION": "${REGION}",
    }
    assert manager.calls[1][1]["input_bytes"] is None  # the gateway gets nothing
    assert json.loads(manager.calls[2][1]["input_bytes"]) == {
        "secrets": {
            "search": {"API_KEY": "sealed-key", "REGION": "eu", "SITE_URL": "https://a.example"}
        }
    }
    assert "sealed-key" not in repr(keyed)


def test_turning_servers_off_revokes_even_after_the_host_restarts(monkeypatch):
    """Codex's P2: a restarted host forgot what it admitted and skipped the revoke."""
    manager = _Manager()
    sandbox_mcp.admit_sandbox_mcp(
        manager, _spec(), slot_id=0, credential="c", servers=(PLAYWRIGHT,)
    )
    monkeypatch.setattr(sandbox_mcp, "_known_clean", set())  # the host restarts
    manager.calls.clear()

    assert not sandbox_mcp.admit_sandbox_mcp(
        manager, _spec(), slot_id=0, credential="c", servers=()
    )

    [(argv, uid)] = _steps(manager)
    assert (argv, uid) == (["admit", "0"], 0)
    assert json.loads(manager.calls[0][1]["input_bytes"])["servers"] == []


def test_a_slot_known_clean_costs_no_exec_until_it_is_admitted_again():
    manager = _Manager()
    sandbox_mcp.admit_sandbox_mcp(manager, _spec(), slot_id=0, credential="c", servers=())
    sandbox_mcp.admit_sandbox_mcp(manager, _spec(), slot_id=0, credential="c", servers=())
    assert len(manager.calls) == 1  # revoked once, then known clean

    sandbox_mcp.admit_sandbox_mcp(
        manager, _spec(), slot_id=0, credential="c", servers=(PLAYWRIGHT,)
    )
    manager.calls.clear()
    sandbox_mcp.admit_sandbox_mcp(manager, _spec(), slot_id=0, credential="c", servers=())
    assert _steps(manager) == [(["admit", "0"], 0)]


def test_a_failed_revoke_is_tried_again_at_the_next_launch():
    manager = _Manager(fail_at="admit")
    sandbox_mcp.admit_sandbox_mcp(manager, _spec(), slot_id=0, credential="c", servers=())
    sandbox_mcp.admit_sandbox_mcp(manager, _spec(), slot_id=0, credential="c", servers=())
    assert len(manager.calls) == 2


def test_an_image_without_the_gateway_is_left_alone_once_found(caplog):
    manager = _Manager(fail_at="admit", status=127)
    with caplog.at_level(logging.WARNING):
        assert not sandbox_mcp.admit_sandbox_mcp(
            manager, _spec(), slot_id=0, credential="c", servers=(PLAYWRIGHT,)
        )
    assert "has no MCP gateway" in caplog.text
    sandbox_mcp.admit_sandbox_mcp(manager, _spec(), slot_id=0, credential="c", servers=())
    assert len(manager.calls) == 1


@pytest.mark.parametrize(
    ("manager", "logged"),
    [
        (_Manager(fail_at="admit"), "admission failed (status 1): refused"),
        (_Manager(fail_at="service"), "gateway failed (status 1): refused"),
        (_Manager(fail_at="runner"), "server runner failed (status 1): refused"),
        (_Manager(fail_at="admit", error=SandboxError("exec failed")), "exec failed"),
    ],
)
def test_an_admission_that_fails_never_blocks_the_member(manager, logged, caplog):
    with caplog.at_level(logging.WARNING):
        assert not sandbox_mcp.admit_sandbox_mcp(
            manager, _spec(), slot_id=0, credential="c", servers=(PLAYWRIGHT,)
        )
    assert logged in caplog.text


def test_servers_without_an_mcp_credential_are_revoked_not_admitted(caplog):
    manager = _Manager()
    with caplog.at_level(logging.WARNING):
        assert not sandbox_mcp.admit_sandbox_mcp(
            manager, _spec(), slot_id=0, credential="", servers=(PLAYWRIGHT,)
        )
    assert "need an MCP credential" in caplog.text
    assert _steps(manager) == [(["admit", "0"], 0)]
    assert json.loads(manager.calls[0][1]["input_bytes"])["servers"] == []


# -- the member's CLI -----------------------------------------------------------------------


def _launch(runtime_id="claude-code", **changes):
    return replace(
        ResidentLaunch(
            thread_id="athr",
            runtime_id=runtime_id,
            runtime_version=None,
            native_session_id=None,
            model="m",
            reasoning_effort=None,
            credential="grant",
            mcp_server_ids=("memory",),
            mcp_base_url="http://relay:18093",
            mcp_credential="mcp-cred",
            **launch_connection(runtime_id),
        ),
        **changes,
    )


def test_a_changed_server_list_restarts_the_member():
    held = ResidentTransport.__new__(ResidentTransport)
    held.launch = _launch(sandbox_mcp_servers=(PLAYWRIGHT,))
    assert "mcp" not in held.incompatible_fields(_launch(sandbox_mcp_servers=(PLAYWRIGHT,)))
    assert "mcp" in held.incompatible_fields(_launch())
    pinned_again = PLAYWRIGHT.model_copy(update={"args": ("-y", "@playwright/mcp@0.0.84")})
    assert "mcp" in held.incompatible_fields(_launch(sandbox_mcp_servers=(pinned_again,)))


@pytest.mark.parametrize("servers", [(), (PLAYWRIGHT,)])
def test_the_cli_is_told_of_the_gateway_only_with_servers(servers):
    manager = SimpleNamespace(backend=SimpleNamespace(gid=10001, base_env=dict))
    spec = SimpleNamespace(container_name="c", runtime_id="claude-code")
    context = ResidentTransportFactory(manager)._launch_context(
        spec, _launch(sandbox_mcp_servers=servers), credential="grant"
    )

    assert context.mcp_config.sandbox is bool(servers)
    endpoints = AgentRuntime.relay_urls(context.mcp_config, model_base_url="")
    assert ("sandbox" in endpoints) is bool(servers)
    if servers:
        assert endpoints["sandbox"] == SANDBOX_MCP_URL


def test_every_dialect_reaches_the_gateway_with_the_relays_one_header():
    config = RuntimeMCPConfig(
        server_ids=("memory",), base_url="http://relay:1", credential="c", sandbox=True
    )

    claude = AgentRuntime._mcp_endpoints(config, model_base_url="")
    assert claude["sandbox"]["url"] == SANDBOX_MCP_URL
    assert claude["sandbox"]["headers"] == claude["session"]["headers"]

    codex = CodexRuntime()._mcp_args(config, model_base_url="")
    assert codex[0] == "-c"
    assert (
        f'sandbox = {{ url = "{SANDBOX_MCP_URL}", bearer_token_env_var = "AGENT_MCP_TOKEN", required = false, startup_timeout_sec = 180'
        in codex[1]
    )
    assert "session = {" in codex[1] and "required = true, startup_timeout_sec = 30" in codex[1]

    env: dict[str, str] = {}
    AgentRuntime._wrapper_mcp_env(CodexRuntime(), config, model_base_url="", env=env)
    assert json.loads(env["AGENT_MCP_SERVERS"])["sandbox"] == SANDBOX_MCP_URL


# -- a program's keys, and package extras --------------------------------------------


@pytest.mark.parametrize(
    ("args", "package"),
    [
        (("mcp-server-git[cli]==1.2.3",), "mcp-server-git[cli]"),
        (("--from", "mcp-server-git[cli,http]==1.2.3", "mcp-git"), "mcp-server-git[cli,http]"),
    ],
)
def test_a_pinned_package_may_name_its_extras(args, package):
    """``pkg[extra]==1.2.3`` is exactly pinned; it was refused as unpinned."""
    plan = _plan("uvx", *args)
    assert (plan.install.package, plan.install.version) == (package, "1.2.3")


def test_a_claim_carries_a_programs_keys_opened_and_only_its_own(monkeypatch):
    from cloud_agent.mcp.registry import mcp_cipher

    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "sandbox-mcp-test-key")
    cipher = mcp_cipher()
    launch = replace(
        LaunchSpec(command="npx", args=("-y", "search-mcp@1.0.0"), env={"KEY": "secret://KEY"}),
        sealed_env={"KEY": cipher.encrypt("the-key"), "UNUSED": cipher.encrypt("other")},
    )
    server = sandbox_servers.sandbox_mcp_server(
        _resolved(ExecutionLocation.SANDBOX, launch=launch).connection, "search"
    )
    assert server is not None
    assert server.secret_env == {"KEY": "the-key"}
    assert "the-key" not in json.dumps(server.admission())

    # A key named but never stored leaves the program out, rather than empty.
    missing = replace(launch, sealed_env={})
    assert (
        sandbox_servers.sandbox_mcp_server(
            _resolved(ExecutionLocation.SANDBOX, launch=missing).connection, "search"
        )
        is None
    )
