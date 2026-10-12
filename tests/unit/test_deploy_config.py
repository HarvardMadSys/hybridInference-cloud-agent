"""Static checks on the self-hosted runner deployment.

A compose file cannot be unit-tested by running it, but the properties that
matter here are structural: a relay network that is not actually internal, or
a flag the runner does not implement, are silent failures — the first weakens
the sandbox boundary without anyone noticing, the second only shows up when a
runner container crash-loops in production.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from cloud_agent.attachments.models import (
    MAX_ATTACHMENT_FILE_BYTES,
    MAX_ATTACHMENT_PATH_LENGTH,
    MAX_CONTENT_TYPE_LENGTH,
)
from cloud_agent.providers.compute.setup_tunnels import _REMOTE_SCRIPT
from cloud_agent_host.runner import build_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = REPO_ROOT / "deploy/host/docker-compose.sandbox.yml"
_CONTROL_PLANE_COMPOSE = REPO_ROOT / "deploy/control-plane/docker-compose.yml"
_CONTROL_PLANE_DEPLOY = REPO_ROOT / "deploy/control-plane/deploy.sh"
_LOCAL_COMPOSE = REPO_ROOT / "deploy/local/docker-compose.yml"
_LOCAL_DEV_SCRIPT = REPO_ROOT / "deploy/local/dev.sh"
_LOCAL_EDGE_DOCKERFILE = REPO_ROOT / "deploy/local/Dockerfile.edge"
_LOCAL_NGINX = REPO_ROOT / "deploy/local/nginx.conf"
_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/sandbox/Dockerfile.agent-sandbox"
_CODEX_REQUIREMENTS = Path(__file__).resolve().parents[2] / "deploy/sandbox/codex-requirements.toml"
#: The hand-provisioning reference for the far machines' sshd tunnel policy.
_SSHD_REFERENCE = REPO_ROOT / "deploy/host/sshd-tunnels.conf.example"


@pytest.fixture(scope="module")
def compose() -> dict:
    """Parse the runner compose overlay."""
    return yaml.safe_load(_COMPOSE.read_text())


@pytest.fixture(scope="module")
def local_compose() -> dict:
    """Parse the Docker-only complete local stack overlay."""
    return yaml.safe_load(_LOCAL_COMPOSE.read_text())


def test_local_dev_script_upgrades_an_existing_env_without_rotating_it(tmp_path: Path) -> None:
    """A checkout update must not require deleting local credentials first."""
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    dotenv = script.parent / ".env.local"
    dotenv.write_text(
        "AGENT_SESSION_SECRET=keep-this-secret\n"
        "AGENT_WORKSPACE_BROKER_TOKEN=keep-this-broker-token\n"
        "LOCAL_EDGE_PORT=23002\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Force the script's documented standalone fallback even on CI hosts that
    # have a real Compose plugin. Otherwise `docker compose version` escapes
    # this hermetic fixture and the real plugin tries to render files that the
    # deliberately minimal temporary checkout does not contain.
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 1\n")
    fake_docker.chmod(0o755)
    fake_compose = fake_bin / "docker-compose"
    compose_env = tmp_path / "compose-env"
    fake_compose.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$GATEWAY_AUTHORIZE_URL" "$AGENT_SESSION_REDIRECT_URI" '
        '"$AGENT_WEB_BASE_URL" >"$COMPOSE_ENV_LOG"\n'
    )
    fake_compose.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), "config"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COMPOSE_ENV_LOG": str(compose_env),
            # A second local stack should not inherit the URLs persisted for
            # the first one's port.
            "LOCAL_EDGE_PORT": "24002",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1)
        for line in dotenv.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert values["AGENT_SESSION_SECRET"] == "keep-this-secret"
    assert values["AGENT_WORKSPACE_BROKER_TOKEN"] == "keep-this-broker-token"
    assert values["LOCAL_PROJECT_NAME"] == "freeinference-local"
    assert values["LOCAL_RELAY_NETWORK"] == "freeinference-local-agent-relays"
    assert values["LOCAL_CHAT_NETWORK_POOL"] == "10.240.0.0/16"
    # This docker answers nothing, so no engine resolver was found.
    assert values["LOCAL_EGRESS_DNS_SERVER"] == "1.1.1.1"
    assert values["GATEWAY_AUTHORIZE_URL"] == "http://127.0.0.1:23002/authorize"
    assert values["AGENT_SESSION_REDIRECT_URI"] == (
        "http://127.0.0.1:23002/api/v1/session/callback"
    )
    assert values["AGENT_WEB_BASE_URL"] == "http://127.0.0.1:23002"
    assert values["AGENT_WEB_GATEWAY_URL"] == ""
    assert compose_env.read_text().splitlines() == [
        "http://127.0.0.1:24002/authorize",
        "http://127.0.0.1:24002/api/v1/session/callback",
        "http://127.0.0.1:24002",
    ]


def _local_dev_with_resolver(tmp_path: Path, resolv_answer: str) -> tuple[Path, dict]:
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    (script.parent / ".env.local").write_text(
        "AGENT_WORKSPACE_BROKER_TOKEN=keep\nLOCAL_EDGE_PORT=13002\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runs = tmp_path / "docker-runs"
    fake_docker = fake_bin / "docker"
    # `compose version` fails so the standalone fallback below is used; a
    # `run` answers as the engine's resolv.conf would, through the real awk.
    fake_docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  run) echo run >>"$DOCKER_RUNS"; shift $(($# - 2)); '
        'printf "%b" "$RESOLV_ANSWER" | awk "$1" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    fake_docker.chmod(0o755)
    fake_compose = fake_bin / "docker-compose"
    fake_compose.write_text("#!/bin/sh\nexit 0\n")
    fake_compose.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_RUNS": str(runs),
        "RESOLV_ANSWER": resolv_answer,
    }
    return script, env


@pytest.mark.parametrize(
    "resolv,expected",
    [
        ("nameserver 192.168.5.1\n", "192.168.5.1"),
        # Loopback is this engine's, not the sandbox's; IPv6 is refused.
        ("nameserver 127.0.0.53\nnameserver 10.0.0.2\n", "10.0.0.2"),
        ("nameserver fd00::1\n", "1.1.1.1"),
        ("", "1.1.1.1"),
    ],
)
def test_local_dev_records_the_resolver_containers_already_use(
    tmp_path: Path, resolv: str, expected: str
) -> None:
    script, env = _local_dev_with_resolver(tmp_path, resolv)

    first = subprocess.run(
        ["sh", str(script), "config"], env=env, capture_output=True, text=True, check=False
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        ["sh", str(script), "config"], env=env, capture_output=True, text=True, check=False
    )
    assert second.returncode == 0, second.stderr

    lines = (script.parent / ".env.local").read_text().splitlines()
    assert [line for line in lines if line.startswith("LOCAL_EGRESS_DNS_SERVER=")] == [
        f"LOCAL_EGRESS_DNS_SERVER={expected}"
    ]
    # Written once: an operator's edit is not overwritten by a later start.
    assert (tmp_path / "docker-runs").read_text().splitlines() == ["run"]


def test_local_dev_refuses_to_replace_lost_credentials_for_an_existing_database(
    tmp_path: Path,
) -> None:
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_compose = fake_bin / "docker-compose"
    fake_compose.write_text("#!/bin/sh\nexit 0\n")
    fake_compose.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  'volume ls --quiet --filter label=com.docker.compose.project=freeinference-local "
        "--filter label=com.docker.compose.volume=agent-postgres')\n"
        "    echo freeinference-local_agent-postgres ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), "config"],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not (script.parent / ".env.local").exists()
    assert "still owns its Postgres volume" in result.stderr
    assert "will not delete the volume or invent a new password" in result.stderr


@pytest.mark.parametrize("prebuilt", [False, True])
def test_local_up_stops_before_full_stack_when_retained_database_auth_fails(
    tmp_path: Path,
    prebuilt: bool,
) -> None:
    script = tmp_path / "repo/deploy/local/dev.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(_LOCAL_DEV_SCRIPT, script)
    (script.parent / ".env.local").write_text(
        "AGENT_POSTGRES_PASSWORD=wrong-retained-password\n"
        "AGENT_DATABASE_URL=postgresql://cloudagent:wrong-retained-password@postgres/cloudagent\n"
        "AGENT_WORKSPACE_BROKER_TOKEN=keep\n"
        "LOCAL_PROJECT_NAME=freeinference-local\n"
        "LOCAL_RELAY_NETWORK=freeinference-local-agent-relays\n"
        "LOCAL_CHAT_NETWORK_POOL=10.240.0.0/16\n"
        "LOCAL_EGRESS_DNS_SERVER=192.168.5.1\n"
        "LOCAL_EDGE_PORT=13002\n"
        "GATEWAY_AUTHORIZE_URL=http://127.0.0.1:13002/authorize\n"
        "AGENT_SESSION_REDIRECT_URI=http://127.0.0.1:13002/api/v1/session/callback\n"
        "AGENT_WEB_BASE_URL=http://127.0.0.1:13002\n"
        "AGENT_WEB_GATEWAY_URL=\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    compose_log = tmp_path / "compose.log"
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n")
    docker.chmod(0o755)
    compose = fake_bin / "docker-compose"
    compose.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >>"$COMPOSE_LOG"\n'
        'case "$*" in\n'
        "  *' exec -T postgres pg_isready') exit 0 ;;\n"
        "  *' run --rm --no-deps migrate python -c '*) exit 17 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    compose.chmod(0o755)

    result = subprocess.run(
        ["sh", str(script), "up", *(["--no-build"] if prebuilt else [])],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "COMPOSE_LOG": str(compose_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = compose_log.read_text()

    assert result.returncode != 0
    assert (" build sandbox-image migrate" in calls) is not prebuilt
    assert " up --detach postgres" in calls
    assert " run --rm --no-deps migrate python -c" in calls
    assert " up --detach --build" not in calls
    assert "cannot authenticate to the existing Postgres volume" in result.stderr


def test_complete_local_stack_exposes_only_its_loopback_edge(local_compose: dict):
    services = local_compose["services"]

    assert services["local-edge"]["ports"] == ["127.0.0.1:${LOCAL_EDGE_PORT:-13002}:8088"]
    assert "edge" not in services
    assert services["local-gateway"]["environment"]["LOCAL_EDGE_PORT"] == (
        "${LOCAL_EDGE_PORT:-13002}"
    )
    assert services["local-gateway"]["environment"]["LOCAL_IDENTITY_ALLOWED_REDIRECTS"] == (
        "${LOCAL_IDENTITY_ALLOWED_REDIRECTS:-}"
    )
    assert "ports" not in services["local-gateway"]
    assert "ports" not in services["local-repo"]


def test_the_local_stack_can_switch_profiling_on_for_every_producer(local_compose: dict):
    """A local experiment must be able to measure more than the control plane.

    The runner, the broker and the control plane are separate processes and each
    reads the switch itself. Only the control-plane compose file passed it on,
    so a local stack with profiling enabled collected the file and terminal
    requests and silently nothing else: no startup record at all, and the four
    startup paths the experiment exists to compare could not be produced.
    """
    services = local_compose["services"]
    for name in ("runner", "workspace-broker"):
        environment = services[name]["environment"]
        assert environment["AGENT_OPERATION_PROFILING_ENABLED"] == (
            "${AGENT_OPERATION_PROFILING_ENABLED:-false}"
        )
        # Passed through rather than defaulted to a literal: the experiment's
        # run descriptor and sink path come from the operator's env file.
        assert environment["AGENT_OPERATION_PROFILING_RUN"] == "${AGENT_OPERATION_PROFILING_RUN:-}"
        assert environment["AGENT_OPERATION_PROFILING_SINK"] == (
            "${AGENT_OPERATION_PROFILING_SINK:-}"
        )
        # The control mode and the deployment namespace travel the same way.
        # Both are empty/default by default, which is what keeps a local stack
        # that sets neither behaving exactly as it did before runtime control
        # existed.
        assert environment["AGENT_OPERATION_PROFILING_MODE"] == (
            "${AGENT_OPERATION_PROFILING_MODE:-}"
        )
        assert environment["AGENT_OPERATION_PROFILING_DEPLOYMENT"] == (
            "${AGENT_OPERATION_PROFILING_DEPLOYMENT:-default}"
        )


def test_complete_local_sandbox_network_has_only_narrow_relays(local_compose: dict):
    on_relays = {
        name
        for name, service in local_compose["services"].items()
        if "agent-relays" in (service.get("networks") or {})
    }

    assert local_compose["networks"]["agent-relays"]["internal"] is True
    assert on_relays == {"local-gateway", "mcp-relay"}


def test_complete_local_stack_uses_the_one_network(local_compose: dict):
    """The local stack runs the production network, not a closed local variant."""
    for name in ("runner", "workspace-broker"):
        env = local_compose["services"][name]["environment"]
        assert (
            env["AGENT_RELAY_NETWORK"] == "${LOCAL_RELAY_NETWORK:-freeinference-local-agent-relays}"
        )
        assert env["AGENT_EGRESS_DNS_SERVER"].startswith("${LOCAL_EGRESS_DNS_SERVER:?")
        assert env["AGENT_EGRESS_CHAT_NETWORK_POOL"] == "${LOCAL_CHAT_NETWORK_POOL:-10.240.0.0/16}"
    for service in local_compose["services"].values():
        for key in service.get("environment") or {}:
            assert "TIER" not in key and "PROXY" not in key, key


def test_mcp_only_listener_receives_the_connection_decryption_key() -> None:
    """A healthy relay must be able to resolve the owner's stored connection."""
    control_compose = yaml.safe_load(_CONTROL_PLANE_COMPOSE.read_text())
    relay_env = control_compose["services"]["mcp-relay"]["environment"]

    assert "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY" in relay_env
    assert "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY" in relay_env["AGENT_SOURCE_CONTROL_ENCRYPTION_KEY"]


def test_complete_local_host_data_is_docker_owned(local_compose: dict):
    """Runner/broker may control Docker but may not map app data from the host."""
    named = set(local_compose["volumes"])
    control = local_compose["services"]["control-plane"]
    assert control["environment"]["AGENT_GITHUB_APP_PRIVATE_KEY_PATH"] == ""
    assert control["volumes"] == ["local-empty-github-key:/etc/cloud-agent/github-app.pem:ro"]
    for service_name in ("runner", "workspace-broker"):
        for mount in local_compose["services"][service_name]["volumes"]:
            source = _split_mount(mount)[0]
            if "docker.sock" in source:
                assert mount.endswith(":ro")
            else:
                assert source in named, f"{service_name} maps host data through {mount}"


def test_complete_local_stack_runs_every_native_session_driver(local_compose: dict):
    from cloud_agent_host.runtimes import registered_runtimes

    expected = {"claude-code", "codex", "pi", "opencode", "kilo"}
    assert set(registered_runtimes()) == expected
    for service_name in ("runner", "workspace-broker"):
        configured = local_compose["services"][service_name]["environment"]
        assert "AGENT_RESTARTABLE_RUNTIMES" not in configured
        assert "AGENT_SHARED_LIVE_RUNTIMES" not in configured


def test_complete_local_edge_reresolves_recreated_services():
    """Rebuilding one service must not strand the still-running edge on its old IP."""
    config = _LOCAL_NGINX.read_text()

    assert "FROM nginx:1.27.5-alpine" in _LOCAL_EDGE_DOCKERFILE.read_text()
    assert "resolver 127.0.0.11" in config
    for upstream in ("control-plane:8000", "web:3000", "local-gateway:8080"):
        assert f"server {upstream} resolve;" in config


def test_the_standalone_edge_is_opt_in_and_loopback():
    """The edge exists only for a deployment that owns its hostname.

    Under the gateway's hostname the console already splits `/agents/*`
    between the two services, so an edge that started unconditionally would
    be a second, unused listener on every deployment — and one that dropped
    its profile by accident would appear on the next deploy of all of them.
    Loopback by default for the same reason every other published port is:
    the tunnel is on the host, and nothing else should reach it.
    """
    # The `compose` fixture is the runner host's stack; the edge lives beside
    # the control plane and the web app, in the other file.
    edge = yaml.safe_load(_CONTROL_PLANE_COMPOSE.read_text())["services"]["edge"]
    assert edge["profiles"] == ["edge"]
    assert edge["ports"] == ["${AGENT_EDGE_BIND:-127.0.0.1}:${AGENT_EDGE_PORT:-3003}:80"]
    assert edge["networks"] == ["control-plane"]


def test_the_standalone_edge_strips_the_api_prefix():
    """`/api/` must reach the control plane as `/`.

    The control plane serves `/v1/...` at its root, exactly as it does behind
    the console (whose rewrite strips `/agents/api`). A `proxy_pass` without
    the trailing slash forwards the prefix, and every API call 404s while the
    HTML still loads — a failure that reads as a client bug.
    """
    text = (REPO_ROOT / "deploy/control-plane/edge.nginx.conf").read_text()
    api = text[text.index("location /api/") : text.index("location / ")]
    assert "proxy_pass http://cloud_agent_api/;" in api
    assert "proxy_buffering off;" in api, "the control plane streams events over SSE"


def test_the_standalone_edge_is_probed_before_a_deploy_succeeds():
    """An enabled edge must prove both routes before recording the deploy.

    Checking the services directly does not catch an nginx crash or a broken
    path split: both upstreams can be healthy while the public hostname is
    returning 502 or sending API requests to the web app.
    """
    text = _CONTROL_PLANE_DEPLOY.read_text()
    before_success = text[: text.index("printf '%s\\n' \"${SHA}\" > .deployed-sha")]
    edge_checks = before_success[before_success.index('if grep -qx edge <<<"${ACTIVE_SERVICES}"') :]

    assert 'ACTIVE_SERVICES="$(compose config --services)"' in before_success
    assert 'wait_for "edge web route" "http://127.0.0.1:${EDGE_PORT}/"' in edge_checks
    assert 'wait_for "edge API route" "http://127.0.0.1:${EDGE_PORT}/api/healthz"' in edge_checks


@pytest.mark.parametrize(
    "file,prefix,upstream",
    [
        ("deploy/control-plane/edge.nginx.conf", "/api", "http://cloud_agent_api"),
        ("deploy/local/nginx.conf", "/api", "http://cloud_agent_api"),
        (
            "deploy/reverse-proxy/agents-under-gateway.conf",
            "/agents/api",
            "http://127.0.0.1:8000",
        ),
    ],
)
def test_edge_allows_the_largest_supported_attachment_json(file, prefix, upstream):
    """A valid base64 upload must fit the proxy limit and retain its API path."""
    config = (REPO_ROOT / file).read_text()
    route = f"{prefix}/v1/agent/uploads"
    match = re.search(r"location " + re.escape(route) + r"\s*\{([^}]+)\}", config)
    assert match is not None, "uploads need a distinct bounded proxy body limit"
    block = match.group(1)
    limit = re.search(r"client_max_body_size\s+(\d+)([km]?)\s*;", block)
    assert limit is not None
    maximum = int(limit.group(1)) * {"": 1, "k": 1024, "m": 1024 * 1024}[limit.group(2)]
    body = json.dumps(
        {
            # Control characters/non-ASCII metadata can consume six JSON bytes
            # per character; include that envelope rather than just base64.
            "path": "\u0100" * MAX_ATTACHMENT_PATH_LENGTH,
            "content_base64": base64.b64encode(bytes(MAX_ATTACHMENT_FILE_BYTES)).decode(),
            "content_type": "\u0100" * MAX_CONTENT_TYPE_LENGTH,
        }
    ).encode()
    assert len(body) <= maximum <= 8 * 1024 * 1024
    assert f"proxy_pass {upstream}/v1/agent/uploads;" in block
    assert config.count("client_max_body_size") == 1, "other routes retain the default"


@pytest.mark.parametrize(
    "file",
    ["deploy/control-plane/edge.nginx.conf", "deploy/reverse-proxy/agents-under-gateway.conf"],
)
def test_shipped_proxies_overwrite_the_forwarded_address(file):
    """The audit trail must not record an address the caller wrote.

    With ``AGENT_TRUST_PROXY_HEADERS`` on, ``get_client_ip`` takes the first
    ``X-Forwarded-For`` entry. ``$proxy_add_x_forwarded_for`` appends to the
    header the caller sent, so that first entry would be the caller's choice.
    Each shipped proxy replaces it with the one peer address nginx knows.
    """
    config = (REPO_ROOT / file).read_text()
    live = [line.split("#", 1)[0] for line in config.splitlines()]
    assert not any("$proxy_add_x_forwarded_for" in line for line in live)
    forwarded = [
        line.strip() for line in live if re.search(r"proxy_set_header\s+X-Forwarded-For", line)
    ]
    assert forwarded, f"{file} forwards no client address at all"
    assert set(forwarded) == {"proxy_set_header X-Forwarded-For $remote_addr;"}, forwarded


def test_control_and_runner_install_locked_production_dependencies():
    """Both images consume uv.lock without an unlocked pip project install."""
    for file in ("deploy/control-plane/Dockerfile", "deploy/host/Dockerfile.agent-runner"):
        recipe = (REPO_ROOT / file).read_text()
        assert "uv.lock" in recipe
        assert "uv sync --frozen --no-dev --no-install-project" in recipe
        assert '"uv==${UV_VERSION}"' in recipe
        assert "pip install --no-cache-dir ." not in recipe
    control = (REPO_ROOT / "deploy/control-plane/Dockerfile").read_text()
    assert "uv sync --frozen --no-dev --no-editable" in control


def test_root_mounted_web_build_accepts_an_explicit_empty_base_path():
    """Compose must distinguish unset from explicitly empty for root hosting."""
    text = _CONTROL_PLANE_COMPOSE.read_text()

    assert "${AGENT_WEB_BASE_PATH?" in text
    assert "${AGENT_WEB_BASE_PATH:?" not in text


def test_docker_build_context_excludes_local_dependency_trees():
    """Local installs must not turn every image build into a 700 MB upload."""
    ignored = (REPO_ROOT / ".dockerignore").read_text().splitlines()

    assert ".venv" in ignored
    assert "apps/frontend/node_modules" in ignored
    assert "apps/frontend/.next" in ignored


def test_the_relay_network_is_internal(compose: dict):
    """The relay discovery network needs no route, and holds no sandbox.

    Group lifecycle reads the two relays' identities there and attaches them
    to each group's own bridge; the runner refuses to start if it is routable.
    """
    assert compose["networks"]["agent-relays"]["internal"] is True


def test_default_backend_is_vm_isolated(compose: dict):
    """Self-hosted defaults to a kernel boundary per job, not a shared one."""
    env = compose["services"]["runner"]["environment"]
    assert "kata" in env["AGENT_SANDBOX_BACKEND"]


def test_runner_uses_enrollment_not_a_shared_claim_secret(compose: dict):
    env = compose["services"]["runner"]["environment"]
    assert "AGENT_DISPATCHER_TOKEN" not in env
    assert "AGENT_ENROLL_TOKEN" in env
    assert "AGENT_HOST_CREDENTIAL_FILE" in env


def test_compose_only_uses_flags_the_runner_implements(compose: dict):
    """Every flag in the compose command must exist, or the container crash-loops."""
    command = compose["services"]["runner"]["command"]
    flags = {arg for arg in command if isinstance(arg, str) and arg.startswith("--")}
    known = {
        action_option
        for action in build_parser()._actions
        for action_option in action.option_strings
    }
    assert flags <= known, f"compose passes unknown flags: {flags - known}"


def test_sandbox_image_carries_no_credentials():
    """No secret may be baked into the image the agent runs in."""
    text = _DOCKERFILE.read_text()
    for forbidden in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AGENT_DISPATCHER_TOKEN", "hyi-"):
        assert forbidden not in text, f"{forbidden} must never be baked into the sandbox image"


def test_sandbox_image_runs_as_non_root():
    """A kernel escape should have to start from an unprivileged user."""
    text = _DOCKERFILE.read_text()
    assert "USER agent" in text
    assert text.index("USER agent") < text.index("WORKDIR /workspace")


def test_sandbox_image_disables_agent_phone_home():
    """Telemetry and auto-update are off, so a blocked request is not a mystery timeout."""
    text = _DOCKERFILE.read_text()
    for flag in (
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        assert flag in text


def test_codex_mcp_is_constrained_to_an_empty_requirements_allowlist():
    """User, project, and plugin MCP servers are disabled after config merge.

    Codex 0.145 treats the presence of top-level ``mcp_servers`` requirements
    as the server allowlist. The same empty allowlist is applied a second time
    to plugin-provided MCP servers, so an empty table is stronger than
    ``--ignore-user-config`` alone.
    """
    requirements = tomllib.loads(_CODEX_REQUIREMENTS.read_text())
    assert requirements == {"mcp_servers": {}}
    assert "codex-requirements.toml /etc/codex/requirements.toml" in _DOCKERFILE.read_text()


# ── the chain from compose file to a running sandbox ───────────────────
#
# Each of these encodes a way the self-hosted deployment failed while every
# individual file looked correct in isolation. They are static because the
# alternative is discovering them one at a time on a remote host.

_RUNNER_DOCKERFILE = Path(__file__).resolve().parents[2] / "deploy/host/Dockerfile.agent-runner"


def _split_mount(mount: str) -> list[str]:
    """Split a compose volume on its separators, not on the ones inside ``${}``.

    ``${VAR:-/default}`` contains a colon of its own, so a naive split reports
    a mismatch that is not there.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for char in mount:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == ":" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def _expand(value: str) -> str:
    """Resolve ``${VAR:-default}`` to its default, as an unset environment would."""
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", value)


def test_the_relay_network_name_is_pinned(compose: dict):
    """Compose prefixes generated network names with the project name.

    The runner looks the relays up by this exact name, so without an explicit
    one it asks for `agent-relays` while the network that exists is
    `<project>_agent-relays`, and every group fails to find its relays. The
    name is derived from the project, so two stacks on one machine never
    discover each other's relays.
    """
    name = compose["networks"]["agent-relays"]["name"]
    assert "COMPOSE_PROJECT" in name
    for service in ("runner", "workspace-broker"):
        assert compose["services"][service]["environment"]["AGENT_RELAY_NETWORK"] == name


def test_exactly_the_two_relays_are_discoverable(compose: dict):
    """A relay found here is attached to every group bridge, so the set is narrow.

    Model calls go to the gateway tunnel and tool calls to the MCP tunnel; if
    nothing here can carry them, the sandbox fails at its first request.
    """
    on_relays = {
        name
        for name, svc in compose["services"].items()
        if "agent-relays" in (svc.get("networks") or {})
    }
    # **Exactly these two, named.** Loosening it to "at least one" or "a
    # non-empty set" is the natural way to make it pass after adding a
    # service, and it would let the next addition be attached to every group
    # bridge without a word.
    assert on_relays == {"gateway-tunnel", "mcp-tunnel"}


def test_the_gateway_name_resolves_on_every_network_that_is_given_it(compose: dict):
    """**The name is handed to two services on two different networks.**

    The runner sits on `internal`, and group lifecycle finds the relay by the
    same name on `agent-relays` before attaching it, under that name, to a
    group bridge. Both are handed the same `AGENT_GATEWAY_URL`. A Docker
    network alias is per-network, so declaring it on one resolves for one of
    them and NXDOMAINs for the other — the runner claims a job and then cannot
    reach the gateway to report a single event.

    Caught in review: the alias was on the sandbox side only, while the
    comment above it already claimed both. Nothing here compared the name a
    service is *given* against the networks it can resolve it on, so both
    halves looked right in isolation.
    """
    tunnel = compose["services"]["gateway-tunnel"]
    hostname = "${AGENT_GATEWAY_HOSTNAME:-agent-gateway}"

    # Which networks can resolve the name.
    resolves_on = {
        network
        for network, config in (tunnel.get("networks") or {}).items()
        if hostname in ((config or {}).get("aliases") or [])
    }

    # Which networks a service that was handed the name actually sits on.
    needs_it: set[str] = set()
    for svc in compose["services"].values():
        env = svc.get("environment") or {}
        if not any(hostname in str(value) for value in env.values()):
            continue
        networks = svc.get("networks")
        needs_it.update(networks if isinstance(networks, list) else (networks or {}).keys())

    # The sandbox is not a compose service. Lifecycle discovers the relay on
    # the relay network by this alias, so that network counts too.
    needs_it.add("agent-relays")

    unresolvable = sorted(needs_it - resolves_on)
    assert not unresolvable, (
        f"services on {unresolvable} are given {hostname} but cannot resolve it; "
        f"the alias is declared only on {sorted(resolves_on)}"
    )


def test_the_tunnel_key_is_allowed_the_one_thing_it_exists_to_do(compose: dict):
    """`restrict` disables port forwarding, and `permitopen` does not re-enable it.

    The documented authorized_keys line is the whole security model for this
    credential: one forwarded port, no shell. Written as `restrict,permitopen=`
    it is not a tight rule — it is a *broken* one, and the tunnel simply never
    comes up, which reads as a network problem rather than a typo in a doc.
    """
    # Whichever of these the branch has. `docs/operations.md` arrives later in
    # the migration; a test that assumed the whole stack was present would fail
    # on the very branch that owns the script.
    candidates = [
        REPO_ROOT / "deploy/host/sandbox_host.sh",
        REPO_ROOT / "docs/operations.md",
        _SSHD_REFERENCE,
    ]
    checked = 0
    for path in (candidate for candidate in candidates if candidate.exists()):
        rel = path.relative_to(REPO_ROOT)
        for line in path.read_text().splitlines():
            if "permitopen" not in line or "restrict" not in line:
                continue
            checked += 1
            assert "port-forwarding" in line, (
                f"{rel} documents `restrict` with `permitopen` and no "
                "`port-forwarding`; ssh will refuse the forward this key exists for"
            )
    assert checked, "no authorized_keys example was found to check"


#: Where a documented authorized_keys line for a tunnel key can appear.
_KEY_LINE_DOCS = (
    REPO_ROOT / "deploy/host/sandbox_host.sh",
    REPO_ROOT / "docs/operations.md",
    _SSHD_REFERENCE,
)
_KEY_OPTIONS = re.compile(r'command=""(?:,[a-z-]+(?:="[^"]*")?)*')


def _key_lines(path: Path) -> list[dict[str, str]]:
    """The options of every authorized_keys line documented in ``path``."""
    found = []
    for match in _KEY_OPTIONS.finditer(path.read_text()):
        options = {}
        for option in match.group(0).split(","):
            name, _, value = option.partition("=")
            options[name] = value.strip('"')
        found.append(options)
    return found


def _tunnel_remote_port(compose: dict, service: str) -> int:
    """The far-side port a tunnel forwards to when nothing overrides it."""
    return int(_expand(compose["services"][service]["environment"]["TUNNEL_REMOTE_PORT"]))


def _sshd_match_blocks(text: str) -> dict[str, dict[str, str]]:
    """Each ``Match User`` block's settings, keyed by account, as sshd -T names them."""
    blocks: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, value = line.partition(" ")
        if keyword.lower() == "match":
            criterion, _, account = value.strip().partition(" ")
            current = blocks.setdefault(account, {}) if criterion.lower() == "user" else None
            continue
        assert current is not None, f"{line!r} is outside any Match User block"
        current[keyword.lower()] = value.strip()
    return blocks


def test_only_the_control_tunnel_key_may_listen_and_only_where_it_must(compose: dict):
    """`port-forwarding` re-enables `-R` too; `permitopen` limits only `-L`.

    So a documented key line without `permitlisten` lets its key open
    listeners on the far machine. The control-plane key needs exactly one, for
    the broker's way back, and must name it; the two relay keys need none and
    are refused remote forwarding by their accounts' Match blocks instead
    (`permitlisten="none"` is not something OpenSSH accepts on a key line).
    """
    control = _tunnel_remote_port(compose, "control-plane-tunnel")
    relays = {
        _tunnel_remote_port(compose, "gateway-tunnel"),
        _tunnel_remote_port(compose, "mcp-tunnel"),
    }
    documented_controls = set()
    for path in _KEY_LINE_DOCS:
        rel = path.relative_to(REPO_ROOT)
        for options in _key_lines(path):
            port = int(options["permitopen"].rsplit(":", 1)[1])
            if port in relays:
                assert "permitlisten" not in options, (
                    f"{rel} documents a relay key with a reverse listener: {options}"
                )
                continue
            assert port == control, f"{rel} documents a key for no tunnel: {options}"
            documented_controls.add(path)
            assert "permitlisten" in options, (
                f"{rel} documents the control-plane key without `permitlisten`; "
                "it could then open a listener on any port of the control-plane machine"
            )
            host, _, listen_port = options["permitlisten"].rpartition(":")
            address = ipaddress.ip_address(host)
            assert address.is_private and not address.is_loopback, (
                f"{rel}: the reverse listener must be the Docker bridge gateway, not {host}"
            )
            assert listen_port.isdigit(), f"{rel}: permitlisten needs one exact port"
    assert documented_controls == set(_KEY_LINE_DOCS), (
        "the control-plane key line is no longer documented in "
        f"{sorted(str(p.relative_to(REPO_ROOT)) for p in set(_KEY_LINE_DOCS) - documented_controls)}"
    )


def test_the_reference_sshd_policy_is_what_automatic_setup_writes(compose: dict):
    """A hand-provisioned tunnel account gets exactly the generated policy.

    Automatic setup writes one ``Match User`` block per tunnel account and
    reads it back. The reference an operator copies instead must not drift
    from it: relay accounts forward locally only and listen nowhere, and the
    control account's single listener is the one its key line also names.
    """
    namespace: dict = {"__name__": "reference_sshd_policy"}
    exec(compile(_REMOTE_SCRIPT, "setup_tunnels", "exec"), namespace)
    blocks = _sshd_match_blocks(_SSHD_REFERENCE.read_text())

    relay_ports, reverse = set(), {}
    for account, settings in blocks.items():
        port = int(settings["permitopen"].rsplit(":", 1)[1])
        listen = settings.get("permitlisten")
        expected, _ = namespace["desired_policy"](
            account, port, None if listen == "none" else listen
        )
        assert settings == expected, (
            f"{_SSHD_REFERENCE.name}: {account} differs from the generated policy"
        )
        if listen == "none":
            assert settings["allowtcpforwarding"] == "local"
            relay_ports.add(port)
        else:
            reverse[port] = listen

    assert relay_ports == {
        _tunnel_remote_port(compose, "gateway-tunnel"),
        _tunnel_remote_port(compose, "mcp-tunnel"),
    }, "each relay account needs its own block refusing remote forwards"
    control = _tunnel_remote_port(compose, "control-plane-tunnel")
    assert list(reverse) == [control], "exactly one account, the control one, may listen"
    key_listens = {
        options["permitlisten"]
        for options in _key_lines(_SSHD_REFERENCE)
        if "permitlisten" in options
    }
    assert key_listens == {reverse[control]}, (
        "the control key line and its account must name the same single listener"
    )
    assert "deploy/host/sshd-tunnels.conf.example" in (REPO_ROOT / "docs/operations.md").read_text()


def test_no_shipped_doc_or_config_offers_gateway_ports_yes():
    """`GatewayPorts yes` moves every remote forward to the wildcard address.

    sshd overrides the requested bind and still reports success, so
    `ExitOnForwardFailure` never fires and each host's broker is published on
    every interface of the control-plane machine behind one shared token.
    The broker route needs `clientspecified`, held to one address and port by
    the account's PermitListen and the key's permitlisten.
    """
    offered = re.compile(r"GatewayPorts[\s=`'\"]+yes\b", re.IGNORECASE)
    sources = [
        *sorted((REPO_ROOT / "docs").rglob("*.md")),
        *sorted(REPO_ROOT.glob("*.md")),
        *sorted(p for p in (REPO_ROOT / "deploy").rglob("*") if p.is_file()),
    ]
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in sources
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        )
        if offered.search(line)
    ]
    assert not offenders, f"`GatewayPorts yes` is offered at {offenders}"

    operations = (REPO_ROOT / "docs/operations.md").read_text()
    assert "`GatewayPorts clientspecified`" in operations
    assert "GatewayPorts clientspecified" in _SSHD_REFERENCE.read_text()
    # The bind address alone does not keep the LAN out (weak host model).
    assert re.search(r"iptables -I INPUT -p tcp --dport \d+ ! -i br-\S+ -j DROP", operations)


def test_a_failed_tunnel_probe_is_not_reported_as_a_successful_start() -> None:
    """`up` must not print "done" after the probe said a sandbox cannot reach
    the gateway.

    The subshell is deliberate — `die` exits, and the runner logs below it are
    what explain the failure — but discarding its status meant the diagnosis
    scrolled past and the last line said the deploy worked.
    """
    script = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
    assert "( cmd_check ) || true" not in script, (
        "the tunnel probe's verdict is discarded; a failed check still reports success"
    )
    assert "probe_ok" in script


def test_job_worktrees_are_not_host_mounted(compose: dict):
    """Docker writable layers are the only local chat storage."""
    volumes = compose["services"]["runner"]["volumes"]
    workdir_mounts = [v for v in volumes if "agent-jobs" in v]
    assert workdir_mounts == []


def test_workspace_broker_and_runner_share_no_host_worktree(compose: dict):
    """Files, Terminal, and Git resolve the shared container through Docker."""
    services = compose["services"]
    runner_mounts = {mount for mount in services["runner"]["volumes"] if "agent-jobs" in mount}
    broker_mounts = {
        mount for mount in services["workspace-broker"]["volumes"] if "agent-jobs" in mount
    }

    assert broker_mounts == runner_mounts
    assert broker_mounts == set()
    assert "tmpfs" not in compose["services"]["workspace-broker"]
    # The source also asserted the gateway's own `backend` service pointed at
    # the broker on :8092. Here the gateway is not a service — it is reached
    # through the tunnel — so the caller-side half of that pairing lives in the
    # control plane's configuration, not in this file.


def test_shared_chat_timeout_matches_and_no_legacy_gate_remains(compose: dict):
    services = compose["services"]
    runner_env = services["runner"]["environment"]
    broker_env = services["workspace-broker"]["environment"]

    for name in ("AGENT_RESTARTABLE_SANDBOX", "AGENT_SHARED_LIVE_SANDBOX"):
        assert name not in runner_env
        assert name not in broker_env
    assert runner_env["AGENT_CHAT_IDLE_TIMEOUT_S"] == broker_env["AGENT_CHAT_IDLE_TIMEOUT_S"]


@pytest.mark.parametrize("stack_fixture", ["compose", "local_compose"])
def test_s3_archive_policy_is_not_configured_through_environment(request, stack_fixture: str):
    """Archive policy comes from Postgres; Compose must not add an env fallback."""
    stack = request.getfixturevalue(stack_fixture)
    archive_settings = {
        "AGENT_SANDBOX_ARCHIVE_S3_URI",
        "AGENT_SANDBOX_ARCHIVE_REGION",
        "AGENT_SANDBOX_ARCHIVE_KMS_KEY_ARN",
        "AGENT_SANDBOX_ARCHIVE_EXPECTED_BUCKET_OWNER",
        "AGENT_SANDBOX_ARCHIVE_MAX_BYTES",
        "AGENT_SANDBOX_ARCHIVE_MULTIPART_THRESHOLD_BYTES",
        "AGENT_SANDBOX_ARCHIVE_MULTIPART_PART_BYTES",
    }

    for service in stack["services"].values():
        assert archive_settings.isdisjoint((service.get("environment") or {}).keys())


def test_runner_receives_exact_sha_rollout_metadata(compose: dict) -> None:
    """Runners read deployment metadata without owning its host directory."""
    runner = compose["services"]["runner"]
    env = runner["environment"]

    assert env["AGENT_DEPLOYMENT_SHA"] == "${AGENT_DEPLOYMENT_SHA:-}"
    assert env["AGENT_SHARED_LIVE_READY_FILE"] == "/run/cloud-agent/shared-live-ready"
    marker_mount = next(
        mount
        for mount in runner["volumes"]
        if isinstance(mount, dict) and mount.get("target") == env["AGENT_SHARED_LIVE_READY_FILE"]
    )
    assert marker_mount["type"] == "bind"
    assert marker_mount["read_only"] is True
    assert marker_mount["bind"]["create_host_path"] is False
    compose_dir = REPO_ROOT / "deploy/host"
    assert (compose_dir / marker_mount["source"]).resolve() == REPO_ROOT / ".shared-live-ready"
    assert any("AGENT_HOST_STATE_ROOT" in str(mount) for mount in runner["volumes"])


def test_runner_mounts_the_storage_verdict_read_only(compose: dict) -> None:
    """Mount only the dedicated directory, so file replacement remains visible.

    The deployment creates the bind source before up. Runners cannot write it
    or read unrelated checkout files, and a missing verdict is handled by the
    probe rather than Docker silently creating a directory in its place.
    """
    runner = compose["services"]["runner"]
    env = runner["environment"]

    assert env["AGENT_HOST_STORAGE_PROBE"] == "${AGENT_HOST_STORAGE_PROBE:-overlay_root}"
    mount = next(
        mount
        for mount in runner["volumes"]
        if isinstance(mount, dict) and mount.get("target") == "/run/cloud-agent/storage-probe"
    )
    assert mount["type"] == "bind"
    assert mount["read_only"] is True
    assert mount["bind"]["create_host_path"] is False
    compose_dir = REPO_ROOT / "deploy/host"
    assert (compose_dir / mount["source"]).resolve() == REPO_ROOT / ".storage-probe"


def test_the_storage_probe_is_documented_and_kept_out_of_git() -> None:
    """The verdict is deployment metadata, like the readiness marker beside it."""
    assert (
        "AGENT_HOST_STORAGE_PROBE=overlay_root"
        in (REPO_ROOT / "deploy/host/.env.example").read_text()
    )
    assert ".storage-probe/" in (REPO_ROOT / ".gitignore").read_text()


def test_target_commit_readiness_cannot_be_bypassed() -> None:
    """Readiness is unconditional and no stale feature flag can bypass it."""
    for relative in (
        "deploy/control-plane/deploy.sh",
        "deploy/host/sandbox_host.sh",
    ):
        script = (REPO_ROOT / relative).read_text()
        assert "STAGING_SHARED_LIVE_SANDBOX" not in script
        assert "AGENT_SHARED_LIVE_SANDBOX" not in script

    control_script = (REPO_ROOT / "deploy/control-plane/deploy.sh").read_text()
    assert "cloud_agent.deploy_readiness" in control_script
    assert "--expected-deployment-sha" in control_script


def test_workspace_broker_keeps_execution_off_the_public_gateway(compose: dict):
    """Only the private broker may spawn an isolated workspace command."""
    services = compose["services"]
    broker = services["workspace-broker"]
    socket_mounts = [mount for mount in broker["volumes"] if "docker.sock" in mount]

    assert socket_mounts and all(mount.endswith(":ro") for mount in socket_mounts)
    # The source additionally proved the *gateway* held no socket. There is no
    # gateway service here, so the equivalent guarantee is that the socket
    # holders are exactly the two that need it — asserted as a set in
    # test_deploy_security.py, which fails if a third service acquires one.
    assert broker["networks"] == ["internal"]


def test_chat_container_idle_timeout_is_configured(compose: dict):
    """A settled container remains inspectable until the one chat idle clock."""
    env = compose["services"]["runner"]["environment"]

    assert int(_expand(env["AGENT_CHAT_IDLE_TIMEOUT_S"])) > 0
    assert "AGENT_WORKSPACE_TTL_S" not in env


def test_the_runner_image_can_actually_start_a_sandbox():
    """The container backend shells out to `docker`; the backend image has none."""
    text = _RUNNER_DOCKERFILE.read_text()
    assert "docker:" in text and "/usr/local/bin/docker" in text


def test_sandbox_image_prepares_docker_owned_restartable_state_as_the_agent_user():
    """Retained workspace/session roots must be writable without a host bind."""
    text = _DOCKERFILE.read_text()

    create = "install -d -m 2770 -o 10001 -g 10001 /workspace /agent-state"
    assert create in text
    assert text.index(create) < text.index("USER agent")


def test_the_runner_image_can_check_a_repository_out():
    """No git in the runner means no worktree, and an agent with nothing to read."""
    assert "git" in _RUNNER_DOCKERFILE.read_text()


def test_the_runner_service_uses_the_runner_image(compose: dict):
    """Built from the backend image, the runner has neither docker nor git."""
    dockerfile = compose["services"]["runner"]["build"]["dockerfile"]
    assert dockerfile.endswith("Dockerfile.agent-runner")


def test_the_sandbox_uid_matches_what_the_runner_chowns_to():
    """The runner gives each worktree to this exact id before mounting it.

    Drift here does not fail loudly: the agent simply cannot write to its own
    working tree, and git refuses to run at all with "dubious ownership".
    """
    from cloud_agent_host.sandbox import SANDBOX_GID, SANDBOX_UID

    text = _DOCKERFILE.read_text()
    assert f"--uid {SANDBOX_UID}" in text
    assert f"--gid {SANDBOX_GID}" in text


def test_agent_runtimes_are_pinned_not_floating():
    """`latest` in the image is a dependency that changes under you between builds.

    It already cost a job: `latest` resolved to Claude Code 2.1.220, whose
    request shape the gateway rejected, and every run died at its first model
    call with an error that pointed at the model rather than the CLI version.
    """
    text = _DOCKERFILE.read_text()
    versions = dict(re.findall(r"^ARG (\w+_VERSION)=(\S+)$", text, re.MULTILINE))
    assert versions, "the image must declare runtime versions"
    for name, value in versions.items():
        assert value != "latest", f"{name} must be pinned, not 'latest'"
        assert re.fullmatch(r"\d+\.\d+\.\d+", value), f"{name}={value} is not an exact version"


_RETIRED_NETWORK_SETTINGS = (
    "AGENT_EGRESS_SETUP_TIER",
    "AGENT_EGRESS_AGENT_TIER",
    "AGENT_EGRESS_NETWORK_",
    "AGENT_EGRESS_PROXY",
    "AGENT_EGRESS_ALLOWLIST",
    "AGENT_EGRESS_STOP_GRACE",
    "AGENT_SANDBOX_ALLOW_OPEN_NETWORK",
    "AGENT_SANDBOX_NETWORK",
)


@pytest.mark.parametrize("path", [_COMPOSE, _LOCAL_COMPOSE, _CONTROL_PLANE_COMPOSE])
def test_there_is_no_network_mode_to_select(path: Path):
    """One network, so no setting may choose another one.

    Each of these once picked a tier, a proxy or an open network. A deployment
    file that still passes one along reads as a choice that no longer exists,
    and one that the runner would silently ignore.
    """
    text = path.read_text()
    for name in _RETIRED_NETWORK_SETTINGS:
        assert name not in text, f"{path.name} still passes {name}"
    services = yaml.safe_load(text)["services"]
    assert not [name for name in services if "proxy" in name], "no proxy service remains"


def test_runner_and_broker_build_group_networks_from_the_same_settings(compose: dict):
    """Either process may create or wake a group, so they must agree exactly.

    A broker with another pool, resolver or relay network would build the
    group bridge for a Fork that the runner then refuses, or install a host
    table the runner's verification reports as tampered.
    """
    runner = compose["services"]["runner"]["environment"]
    broker = compose["services"]["workspace-broker"]["environment"]
    for name in (
        "AGENT_SANDBOX_NAMESPACE",
        "AGENT_RELAY_NETWORK",
        "AGENT_EGRESS_CHAT_NETWORK_POOL",
        "AGENT_EGRESS_DNS_SERVER",
        "AGENT_EGRESS_PROTECTED_CIDRS",
        "AGENT_GATEWAY_URL",
        "AGENT_SANDBOX_MCP_URL",
    ):
        assert runner[name] == broker[name], name
    assert "COMPOSE_PROJECT" in runner["AGENT_SANDBOX_NAMESPACE"]


def test_the_pool_and_resolver_have_no_default(compose: dict):
    """Both are deployment facts. A default would be a guess about this host.

    Two deployments on one machine sharing a defaulted pool are refused by the
    host rules only after the second has drained its predecessor's runners;
    a defaulted resolver may be one this site does not let out at all.
    """
    env = compose["services"]["runner"]["environment"]
    assert env["AGENT_EGRESS_CHAT_NETWORK_POOL"].startswith("${AGENT_EGRESS_CHAT_NETWORK_POOL:?")
    assert env["AGENT_EGRESS_DNS_SERVER"].startswith("${AGENT_EGRESS_DNS_SERVER:?")
    assert env["AGENT_EGRESS_PROTECTED_CIDRS"] == "${AGENT_EGRESS_PROTECTED_CIDRS:-}"


def test_the_runner_image_carries_the_host_rule_helper():
    """The guard runs `nft` from the runner's own image, never from the sandbox's."""
    runner = (REPO_ROOT / "deploy/host/Dockerfile.agent-runner").read_text()
    assert re.search(r"^\s+nftables \\$", runner, re.MULTILINE)
    assert "nftables" not in _DOCKERFILE.read_text()


def test_complete_local_stack_namespaces_daemon_wide_chat_discovery(local_compose: dict):
    broker_namespace = local_compose["services"]["workspace-broker"]["environment"][
        "AGENT_SANDBOX_NAMESPACE"
    ]
    assert "LOCAL_PROJECT_NAME" in broker_namespace
    # Runner inherits the exact same anchored environment.
    assert (
        local_compose["services"]["runner"]["environment"]["AGENT_SANDBOX_NAMESPACE"]
        == broker_namespace
    )


def test_the_runner_reads_the_bounds_the_overlay_configures(monkeypatch, compose: dict):
    """A configured lease and timeout must actually reach the runner.

    The overlay set both and the runner read neither, so every self-hosted job
    ran on the built-in defaults no matter what the operator wrote — a setting
    that looks applied and is not.
    """
    env = compose["services"]["runner"]["environment"]
    assert "AGENT_LEASE_TTL" in env and "AGENT_TIMEOUT_S" in env

    monkeypatch.setenv("AGENT_LEASE_TTL", "45")
    monkeypatch.setenv("AGENT_TIMEOUT_S", "600")
    args = build_parser().parse_args([])

    assert args.lease_ttl == 45.0
    assert args.agent_timeout == 600.0


def test_the_runner_reads_the_neutral_gateway_variable(monkeypatch):
    """The self-hosted runner uses the same gateway variable as compose."""
    monkeypatch.setenv("AGENT_GATEWAY_URL", "https://gateway.example.com")

    args = build_parser().parse_args([])

    assert args.base_url == "https://gateway.example.com"


def test_the_runner_keeps_the_deployment_gateway_alias(monkeypatch):
    """Existing hosts keep working while they migrate to the neutral name."""
    monkeypatch.delenv("AGENT_GATEWAY_URL", raising=False)
    monkeypatch.setenv("FREEINFERENCE_BASE_URL", "https://legacy-gateway.example.com")

    args = build_parser().parse_args([])

    assert args.base_url == "https://legacy-gateway.example.com"


def test_an_unparsable_bound_falls_back_rather_than_crash_looping(monkeypatch):
    """A typo in an env var must not take the runner down on every restart."""
    monkeypatch.setenv("AGENT_LEASE_TTL", "two minutes")
    args = build_parser().parse_args([])
    assert args.lease_ttl > 0


# `test_staging_deploy_owns_the_runner_when_the_host_opts_in` is not ported.
# It drives `ops/deploy/deploy_staging.sh`, which deploys the gateway and stays
# in that repository; there is nothing here for it to assert against. Its
# subject — that a host opting into running agents gets the runner started —
# becomes this repository's own deploy story, not a check on the gateway's.


def test_compose_file_has_no_duplicate_keys():
    """Duplicate mapping keys make docker compose reject the whole file.

    `yaml.safe_load` keeps the last of a repeated key, so every test in this
    module passed while `docker compose` refused to parse the overlay at all
    ("mapping key already defined") — the runner could not have started from
    it on any host. Python's tolerance is what hid it; this loader is as
    strict as the Go parser that actually reads the file.
    """

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _no_duplicates(loader, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(
                    f"duplicate key {key!r} in {_COMPOSE.name}: docker compose "
                    "rejects the entire file, so the runner never starts"
                )
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)
    yaml.load(_COMPOSE.read_text(), Loader=_StrictLoader)


_PI_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/sandbox/pi-freeinference"


def test_pi_wrapper_writes_the_provider_config_and_execs_pi(tmp_path):
    """The wrapper turns its environment into pi's models.json, argv untouched.

    Executed for real rather than read: a stub ``pi`` on PATH records the argv
    it receives, HOME is a temp dir, and the assertions read the exact file the
    real pi would read. pi ignores OPENAI_BASE_URL, so this file is the ONLY
    thing standing between a job and api.openai.com.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "argv.json"
    stub = stub_dir / "pi"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    activation = tmp_path / "activation" / "pi"

    prompt = 'prompt; with $(dangerous) `chars` "quoted"'
    result = _subprocess.run(
        [_sys.executable, str(_PI_WRAPPER), "--provider", "freeinference", "-p", prompt],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "PI_GATEWAY_MODEL": "glm-5.1",
            "PI_CODING_AGENT_DIR": str(activation),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    provider = _json.loads((activation / "models.json").read_text())["providers"]["freeinference"]
    assert provider["baseUrl"] == "http://backend:8080/v1"
    assert provider["apiKey"] == "ajt.attempt.key"
    assert provider["api"] == "openai-completions"
    assert provider["models"] == [{"id": "glm-5.1"}]
    assert not (home / ".pi/agent/models.json").exists()
    # argv passed through byte-for-byte: the shell-hostile prompt survived.
    assert _json.loads(record.read_text()) == [
        "--provider",
        "freeinference",
        "-p",
        prompt,
    ]


def test_pi_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a job that dials OpenAI."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_PI_WRAPPER), "-p", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and PI_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


_OPENCODE_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/sandbox/opencode-freeinference"


def test_opencode_wrapper_writes_config_sets_offline_flags_and_execs(tmp_path):
    """The wrapper produces OpenCode's config and kills its phone-home paths.

    A stub ``opencode`` on PATH records argv and the environment it received.
    The offline flags are the load-bearing part: without
    OPENCODE_DISABLE_MODELS_FETCH the CLI fetches models.dev on every start,
    a third-party dependency no job should have.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "seen.json"
    stub = stub_dir / "opencode"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "seen = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()"
        " if k.startswith('OPENCODE_')}}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(seen))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    activation = tmp_path / "activation" / "opencode"

    prompt = 'fix; the $(bug) "carefully"'
    result = _subprocess.run(
        [
            _sys.executable,
            str(_OPENCODE_WRAPPER),
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            "freeinference/glm-5.1",
            prompt,
        ],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "OPENCODE_GATEWAY_MODEL": "glm-5.1",
            "OPENCODE_FREEINFERENCE_CONFIG_DIR": str(activation),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    seen = _json.loads(record.read_text())
    assert seen["argv"] == [
        "run",
        "--format",
        "json",
        "--auto",
        "-m",
        "freeinference/glm-5.1",
        prompt,
    ]
    assert seen["env"]["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert seen["env"]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # OpenCode v1.18.9's supported switch prevents both project opencode.json
    # and project .opencode/ directories from entering the merged config.
    assert seen["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"

    config = _json.loads(Path(seen["env"]["OPENCODE_CONFIG"]).read_text())
    provider = config["provider"]["freeinference"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://backend:8080/v1"
    assert provider["options"]["apiKey"] == "ajt.attempt.key"
    assert provider["models"] == {"glm-5.1": {"name": "glm-5.1"}}
    # Credential-bearing config follows the runtime's activation-only path.
    assert seen["env"]["OPENCODE_CONFIG"].startswith(str(activation))


def test_opencode_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a run that dials out."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_OPENCODE_WRAPPER), "run", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and OPENCODE_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


_KILO_WRAPPER = Path(__file__).resolve().parents[2] / "deploy/sandbox/kilo-freeinference"


def test_kilo_wrapper_writes_config_sets_offline_flags_and_execs(tmp_path):
    """The wrapper produces Kilo's config and kills its phone-home paths.

    A stub ``kilo`` on PATH records argv and the environment it received.
    Kilo is an OpenCode fork that renamed the kill switches to ``KILO_*`` and
    added its own phone-home paths (PostHog telemetry, session ingest and
    share links to app.kilo.ai); without KILO_DISABLE_MODELS_FETCH the CLI
    fetches models.dev on every start, a third-party dependency no job should
    have.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    record = tmp_path / "seen.json"
    stub = stub_dir / "kilo"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "seen = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()"
        " if k.startswith('KILO_') or k == 'DO_NOT_TRACK'}}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(seen))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    activation = tmp_path / "activation" / "kilo"

    prompt = 'fix; the $(bug) "carefully"'
    result = _subprocess.run(
        [
            _sys.executable,
            str(_KILO_WRAPPER),
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            "freeinference/glm-5.1",
            prompt,
        ],
        env={
            "PATH": f"{stub_dir}:{_os.environ['PATH']}",
            "HOME": str(home),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "KILO_GATEWAY_MODEL": "glm-5.1",
            "KILO_FREEINFERENCE_CONFIG_DIR": str(activation),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    seen = _json.loads(record.read_text())
    assert seen["argv"] == [
        "run",
        "--format",
        "json",
        "--auto",
        "-m",
        "freeinference/glm-5.1",
        prompt,
    ]
    # The OpenCode switches, under the fork's renamed prefix.
    assert seen["env"]["KILO_DISABLE_MODELS_FETCH"] == "1"
    assert seen["env"]["KILO_DISABLE_DEFAULT_PLUGINS"] == "1"
    assert seen["env"]["KILO_DISABLE_AUTOUPDATE"] == "1"
    assert seen["env"]["KILO_DISABLE_PROJECT_CONFIG"] == "1"
    # The fork's own phone-home paths, each with its supported switch.
    assert seen["env"]["KILO_DISABLE_SESSION_INGEST"] == "1"
    assert seen["env"]["KILO_DISABLE_SHARE"] == "1"
    assert seen["env"]["KILO_DISABLE_PRESENCE"] == "1"
    assert seen["env"]["KILO_DISABLE_LSP_DOWNLOAD"] == "1"
    assert seen["env"]["KILO_TELEMETRY_LEVEL"] == "off"
    assert seen["env"]["DO_NOT_TRACK"] == "1"

    config = _json.loads(Path(seen["env"]["KILO_CONFIG"]).read_text())
    provider = config["provider"]["freeinference"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://backend:8080/v1"
    assert provider["options"]["apiKey"] == "ajt.attempt.key"
    assert provider["models"] == {"glm-5.1": {"name": "glm-5.1"}}
    # Credential-bearing config follows the runtime's activation-only path.
    assert seen["env"]["KILO_CONFIG"].startswith(str(activation))


#: One entry per wrapper that can publish the platform's MCP relay, with the
#: environment variable its CLI reads the merged config from.
#:
#: Both adapters publish the same authenticated relay and exclude project
#: configuration, including Kilo's reviewed legacy-loader patch.
_MCP_WRAPPERS = (
    ("opencode", _OPENCODE_WRAPPER, "OPENCODE", "OPENCODE_GATEWAY_MODEL", "OPENCODE_CONFIG"),
    ("kilo", _KILO_WRAPPER, "KILO", "KILO_GATEWAY_MODEL", "KILO_CONFIG"),
)


def _run_mcp_wrapper(tmp_path, wrapper, prefix: str, model_var: str, config_var: str, **extra):
    """Run one wrapper with a stub CLI, returning ``(result, seen)``.

    The stub records the environment the real CLI would have started with, so a
    test can read both the flags and the config file the wrapper wrote without
    a CLI being installed.
    """
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    record = tmp_path / f"seen-{prefix}.json"
    stub = stub_dir / prefix.lower()
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "seen = {'argv': sys.argv[1:], 'env': dict(os.environ)}\n"
        f"open({str(record)!r}, 'w').write(json.dumps(seen))\n"
    )
    stub.chmod(0o755)
    home = tmp_path / f"home-{prefix}"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{stub_dir}:{_os.environ['PATH']}",
        "HOME": str(home),
        "OPENAI_BASE_URL": "http://backend:8080/v1",
        "OPENAI_API_KEY": "ajt.attempt.key",
        model_var: "glm-5.1",
        f"{prefix}_FREEINFERENCE_CONFIG_DIR": str(tmp_path / "activation" / prefix),
    }
    env.update(extra)
    result = _subprocess.run(
        [_sys.executable, str(wrapper), "run", "hi"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    seen = _json.loads(record.read_text()) if record.exists() else None
    del config_var
    return result, seen


@pytest.mark.parametrize("name,wrapper,prefix,model_var,config_var", _MCP_WRAPPERS)
def test_the_wrapper_declares_the_relay_and_references_the_token(
    tmp_path, name, wrapper, prefix, model_var, config_var
):
    """**The relay endpoint and a reference, never the credential itself.**

    The reference syntax is the load-bearing detail and it is not portable: this
    CLI family substitutes ``{env:VAR}`` as a text pass over the raw config file
    before parsing it, where Claude Code expands ``${VAR}``. The wrong one
    produces a literal placeholder as the bearer token -- every call 401s and
    nothing says why -- so the exact string is pinned here, and the json module
    is what quotes a URL or a model id into the file.
    """
    import json as _json
    from pathlib import Path as _Path

    result, seen = _run_mcp_wrapper(
        tmp_path,
        wrapper,
        prefix,
        model_var,
        config_var,
        AGENT_MCP_SERVERS=_json.dumps({"github": "http://agent-mcp:8000/v1/agent/mcp/github"}),
        AGENT_MCP_TOKEN="ajt.mcp.attempt.token",
    )
    assert result.returncode == 0, result.stderr

    config = _json.loads(_Path(seen["env"][config_var]).read_text())
    assert config["mcp"] == {
        "github": {
            "type": "remote",
            "url": "http://agent-mcp:8000/v1/agent/mcp/github",
            "enabled": True,
            "headers": {
                "Authorization": "Bearer {env:AGENT_MCP_TOKEN}",
                **({"Connection": "close"} if name == "kilo" else {}),
            },
            # Without this the CLI probes the platform for an authorization
            # server and marks the connection unauthenticated before the first
            # call; the relay speaks MCP with a job token, not OAuth.
            "oauth": False,
            # Ten minutes for approval plus up to fifteen for execution.
            "timeout": 1_560_000,
        }
    }
    # The credential itself must never reach the file the CLI reads.
    assert "ajt.mcp.attempt.token" not in _Path(seen["env"][config_var]).read_text()
    # The project config is shut out, so this is the only MCP source.
    assert seen["env"][f"{prefix}_DISABLE_PROJECT_CONFIG"] == "1"


@pytest.mark.parametrize("name,wrapper,prefix,model_var,config_var", _MCP_WRAPPERS)
def test_the_wrapper_declares_an_empty_map_when_the_run_has_no_connections(
    tmp_path, name, wrapper, prefix, model_var, config_var
):
    """Written either way, so there is no turn on which the key is absent.

    An MCP key that appeared only sometimes would leave the CLI free to find
    its own sources on the turns where it did not -- which is exactly the hole
    the project-config switch closes.
    """
    import json as _json
    from pathlib import Path as _Path

    result, seen = _run_mcp_wrapper(
        tmp_path,
        wrapper,
        prefix,
        model_var,
        config_var,
        AGENT_MCP_SERVERS="{}",
        AGENT_MCP_TOKEN="",
    )
    assert result.returncode == 0, result.stderr
    config = _json.loads(_Path(seen["env"][config_var]).read_text())
    assert config["mcp"] == {}


@pytest.mark.parametrize("name,wrapper,prefix,model_var,config_var", _MCP_WRAPPERS)
def test_the_wrapper_refuses_connections_it_cannot_authenticate(
    tmp_path, name, wrapper, prefix, model_var, config_var
):
    """A named refusal beats a run whose every tool call is a 401."""
    import json as _json

    result, _seen = _run_mcp_wrapper(
        tmp_path,
        wrapper,
        prefix,
        model_var,
        config_var,
        AGENT_MCP_SERVERS=_json.dumps({"github": "http://agent-mcp:8000/v1/agent/mcp/github"}),
        AGENT_MCP_TOKEN="",
    )
    assert result.returncode == 64
    assert "AGENT_MCP_TOKEN" in result.stderr


@pytest.mark.parametrize("name,wrapper,prefix,model_var,config_var", _MCP_WRAPPERS)
def test_the_wrapper_refuses_an_endpoint_map_it_cannot_read(
    tmp_path, name, wrapper, prefix, model_var, config_var
):
    """Unreadable input is a refusal, not an empty map.

    Degrading to "no servers" here would silently drop the tools the job was
    created with, and the agent would work around their absence rather than
    failing.
    """
    result, _seen = _run_mcp_wrapper(
        tmp_path,
        wrapper,
        prefix,
        model_var,
        config_var,
        AGENT_MCP_SERVERS="not json",
        AGENT_MCP_TOKEN="ajt.token",
    )
    assert result.returncode == 64
    assert "AGENT_MCP_SERVERS" in result.stderr


def test_the_pi_bridge_is_shipped_where_the_adapter_loads_it_from():
    """A path that drifts between the image and the adapter is a silent no-op.

    pi is given `-e <this path>` on every run. If the image copied the bridge
    somewhere else, pi would start with an extension path that does not exist:
    with `--no-extensions` the MCP tools would simply be absent, and the agent
    would work around them rather than fail. Read from both files so the two
    cannot disagree without a test failing.
    """
    from cloud_agent_host import runtimes as _runtimes

    dockerfile = (
        Path(__file__).resolve().parents[2] / "deploy/sandbox/Dockerfile.agent-sandbox"
    ).read_text()
    source = Path(__file__).resolve().parents[2] / "deploy/sandbox/pi-mcp-bridge.ts"
    assert source.is_file(), "the bridge is not in the build context"

    destination = f"COPY deploy/sandbox/pi-mcp-bridge.ts {_runtimes.PI_MCP_BRIDGE}"
    assert destination in dockerfile, (
        f"the image does not copy the bridge to {_runtimes.PI_MCP_BRIDGE}, which is "
        "where PiRuntime loads it from"
    )
    # Read-only, and outside any writable path: the extension talks to the relay
    # with the job token, so nothing in the sandbox may replace it.
    assert f"chmod 0444 {_runtimes.PI_MCP_BRIDGE}" in dockerfile


def test_the_pi_bridge_only_ever_reads_its_endpoints_from_the_environment():
    """The bridge must not be able to name a server of its own.

    Its whole authority is the environment the control plane writes: the relay
    endpoints and the `mcp`-scoped job token. A hardcoded URL or a fallback
    would be a second source, and one the platform's binding check does not
    cover.
    """
    source = (Path(__file__).resolve().parents[2] / "deploy/sandbox/pi-mcp-bridge.ts").read_text()
    assert 'MCP_SERVERS_ENV = "AGENT_MCP_SERVERS"' in source
    assert 'MCP_TOKEN_ENV = "AGENT_MCP_TOKEN"' in source
    assert "process.env[MCP_SERVERS_ENV]" in source
    assert "process.env[MCP_TOKEN_ENV]" in source
    # No literal endpoint of any kind: every URL the bridge dials comes from
    # the environment the control plane wrote.
    assert "http://" not in source and "https://" not in source


def test_kilo_wrapper_refuses_to_run_half_configured(tmp_path):
    """Missing environment is a named refusal, not a run that dials out."""
    import os as _os
    import subprocess as _subprocess
    import sys as _sys

    result = _subprocess.run(
        [_sys.executable, str(_KILO_WRAPPER), "run", "hi"],
        env={
            "PATH": _os.environ["PATH"],
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            # OPENAI_API_KEY and KILO_GATEWAY_MODEL deliberately absent.
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 64
    assert "OPENAI_API_KEY" in result.stderr


def test_concurrency_is_sized_at_runtime_not_by_replicas(compose: dict):
    """One container supervises an executor pool sized from the admin page.

    A replica count would be a second answer to the same question, fixed at
    deploy time and stale the moment an operator resized the pool; and two
    containers would enroll the same host twice. What the file may still
    carry is the pool's *default*, so a fresh host has a size before anyone
    has set one.
    """
    runner = compose["services"]["runner"]
    assert "replicas" not in (runner.get("deploy") or {}), (
        "the executor count is not a replica count"
    )
    environment = runner["environment"]
    assert "AGENT_RUNNER_REPLICAS" not in environment
    assert "AGENT_HOST_SLOTS" in str(environment["AGENT_HOST_SLOTS"]), (
        "the default pool size is not operator-tunable"
    )


def test_replicas_are_distinguishable_in_the_lease_ledger(compose: dict):
    """Replicas must not all claim jobs under the same lease_owner.

    Fencing never reads this string, so a collision is not a correctness bug —
    it just makes "which runner is stuck" unanswerable exactly when a second
    replica makes it worth asking.
    """
    command = " ".join(compose["services"]["runner"].get("command") or [])
    assert "--worker-id" not in command, (
        "a literal --worker-id pins every replica to the same lease_owner; "
        "let the runner derive one that includes its hostname"
    )


def test_runner_derives_a_distinct_worker_id_per_container(monkeypatch):
    """The derived id groups by configuration and separates by host."""
    from cloud_agent_host.runner import default_worker_id

    monkeypatch.setattr("socket.gethostname", lambda: "abc123")
    monkeypatch.delenv("AGENT_WORKER_ID", raising=False)
    assert default_worker_id() == "runner-abc123"

    monkeypatch.setenv("AGENT_WORKER_ID", "staging")
    assert default_worker_id() == "staging-abc123"


# ── Remote runner host (no gateway on this machine) ────────────────────

_REMOTE_COMPOSE = REPO_ROOT / "deploy/host/docker-compose.sandbox.yml"
_TUNNEL_ENTRYPOINT = Path(__file__).resolve().parents[2] / "deploy/host/agent-gateway-tunnel.sh"


@pytest.fixture(scope="module")
def remote_compose() -> dict:
    """Parse the standalone remote-runner compose file."""
    return yaml.safe_load(_REMOTE_COMPOSE.read_text())


def test_remote_compose_starts_no_gateway(remote_compose: dict):
    """The whole reason this file exists separately from the overlay.

    `docker-compose.agent-runner.yml` layers on the main stack and its runner
    declares `depends_on: backend`, so aiming it at a machine with no gateway
    starts one there — on a box meant only to run jobs.
    """
    assert set(remote_compose["services"]) == {
        # Three tunnels, one per destination: the gateway for model calls, the
        # MCP relay for tools, the control plane for claims. Splitting them is
        # what keeps "may call models" and "may create jobs" separate
        # permissions; see `test_deploy_security.py` for which reaches what.
        "gateway-tunnel",
        "mcp-tunnel",
        "control-plane-tunnel",
        "workspace-broker",
        "runner",
    }
    for name, service in remote_compose["services"].items():
        assert "backend" not in (service.get("depends_on") or []), name


def test_runner_and_sandbox_are_pointed_at_the_same_name(remote_compose: dict):
    """They share one AGENT_GATEWAY_URL, so one name has to serve both.

    The runner claims over it and the sandbox is handed the same value; a URL
    that only the runner can resolve means jobs are claimed and then die at
    their first model call.
    """
    alias = remote_compose["services"]["gateway-tunnel"]["networks"]["agent-relays"]["aliases"]
    assert alias == ["${AGENT_GATEWAY_HOSTNAME:-agent-gateway}"]
    url = remote_compose["services"]["runner"]["environment"]["AGENT_GATEWAY_URL"]
    assert "${AGENT_GATEWAY_HOSTNAME:-agent-gateway}" in url


def test_the_tunnel_credential_is_mounted_read_only(remote_compose: dict):
    volumes = remote_compose["services"]["gateway-tunnel"]["volumes"]
    assert all(volume.endswith(":ro") for volume in volumes), volumes


def test_the_tunnel_refuses_to_skip_host_verification():
    """Encrypting the hop is pointless if anything may answer for the gateway.

    The credentials this tunnel exists to protect — the enrolled-host token and
    every job capability — would go straight to an impostor.
    """
    script = _TUNNEL_ENTRYPOINT.read_text()
    assert "StrictHostKeyChecking=yes" in script
    assert "StrictHostKeyChecking=no" not in script
    assert "UserKnownHostsFile" in script
    # And it must refuse to start without one, rather than quietly trusting
    # whatever answers the first time.
    assert re.search(r'\[ -s "\$KNOWN_HOSTS" \] \|\| die', script)


def test_the_tunnel_dies_rather_than_listening_on_nothing():
    """Without this, ssh stays up having bound no port and every sandbox gets
    connection-refused against a container that looks healthy."""
    assert "ExitOnForwardFailure=yes" in _TUNNEL_ENTRYPOINT.read_text()


def test_the_tunnel_forwards_one_fixed_destination():
    """A relay, not a proxy: nothing the caller sends chooses where it goes."""
    script = _TUNNEL_ENTRYPOINT.read_text()
    assert "-L " in script
    for proxy_flag in ("-D ", "DynamicForward", "ProxyCommand"):
        assert proxy_flag not in script, proxy_flag


def test_deployment_builds_disable_provenance_attestations() -> None:
    """A regenerated attestation makes an unchanged rebuild a new image.

    Buildx attaches provenance by default and rewrites it on every build, so an
    image's manifest index moves while the image itself does not. A daemon with
    the containerd image store identifies an image by that index, and Compose
    then treats every image it just rebuilt as changed: it replaces all eight
    containers of the sandbox stack on every deploy — 10.3s of that the egress
    proxy sitting out Docker's stop timeout — and the digest a retained group
    pinned stops resolving. Measured on the real runner image with every layer
    CACHED: two builds with the default give two ids, two with the flag give one.

    Asserted on the command lines because the flag changes nothing until it is
    missing on a host with that store, and because the compose file's
    `build.provenance` is the obvious place someone will move it to — a place
    `docker compose config` accepts and `docker compose build` ignores.
    """
    host = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
    control = _CONTROL_PLANE_DEPLOY.read_text()

    # `in`, not `startswith`: the host passes its image and replica count as a
    # command prefix, so the line can legitimately read
    # `AGENT_SANDBOX_IMAGE=… compose build …`. It happens to be a continuation
    # line today, which is the only reason `startswith` finds it — joining those
    # two lines, an ordinary tidy-up, would retire this assertion in silence.
    # Comments are skipped because three of them name these commands while
    # explaining the flag.
    compose_builds = [
        line.strip()
        for line in (host + control).splitlines()
        if "compose build" in line and not line.strip().startswith("#")
    ]
    assert compose_builds, "no compose build command found — has it moved?"
    for line in compose_builds:
        assert "--provenance=false" in line, f"compose build without the flag: {line!r}"

    sandbox_builds = [
        line.strip()
        for line in host.splitlines()
        if "docker build" in line and not line.strip().startswith("#")
    ]
    assert sandbox_builds, "no sandbox image build found — has it moved?"
    for line in sandbox_builds:
        assert "--provenance=false" in line, f"sandbox image build without the flag: {line!r}"

    for compose in (_COMPOSE, _CONTROL_PLANE_COMPOSE):
        # The *key*, not the word: a comment recording why `build.provenance`
        # cannot be used here is exactly what the next reader needs, and a
        # substring check would fail the file for carrying it.
        assert not re.search(r"^\s*provenance\s*:", compose.read_text(), re.MULTILINE), (
            f"{compose.name} sets build.provenance, which compose accepts and then ignores; "
            "the flag has to be on the build command"
        )
