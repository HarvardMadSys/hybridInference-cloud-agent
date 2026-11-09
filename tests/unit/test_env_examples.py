"""Every variable a stack refuses to start without has something that supplies it.

Nobody writes a production env file by hand any more: a new control plane
needs none (its keys come from the `keys` service, everything else from
Admin), and a host's is written by Admin → Servers → Add server. So the
compose files are the source of truth, and each required variable is checked
against what supplies it. A new required variable fails here rather than at an
operator's first `docker compose up`.

Written as a test and not a checklist deliberately. A checklist is a thing
somebody remembers to update.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "stack_files",
    [
        ("deploy/host/docker-compose.sandbox.yml",),
        ("deploy/control-plane/docker-compose.yml", "deploy/local/docker-compose.yml"),
    ],
    ids=["host", "local"],
)
def test_compose_accepts_the_new_token_alone_and_still_rejects_missing_credentials(
    tmp_path, stack_files
):
    docker = shutil.which("docker")
    if (
        docker is None
        or subprocess.run(
            [docker, "compose", "version"], capture_output=True, check=False
        ).returncode
    ):
        pytest.skip("Docker Compose is needed to validate required interpolation")
    paths = [REPO / name for name in stack_files]
    values = dict.fromkeys(
        (
            name
            for path in paths
            for name in re.findall(r"\$\{([A-Z_][A-Z0-9_]*):?\?", path.read_text())
        ),
        "test-placeholder",
    )
    for name in values:
        if name.endswith(("_SSH_KEY", "_SSH_KNOWN_HOSTS")):
            values[name] = "/dev/null"
    values.update(
        COMPOSE_PROJECT="sandbox-manager-rename-test",
        AGENT_SANDBOX_MANAGER_TOKEN="test-manager-credential",
        AGENT_EGRESS_CHAT_NETWORK_POOL="10.240.0.0/16",
        AGENT_EGRESS_DNS_SERVER="192.0.2.53",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AGENT_", "COMPOSE_"))
    }
    env_file = tmp_path / ".env"

    def render():
        env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        return subprocess.run(
            [
                docker,
                "compose",
                *(arg for path in paths for arg in ("-f", str(path))),
                "--env-file",
                str(env_file),
                "config",
                "--format",
                "json",
            ],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    rendered = render()
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    for name in ("sandbox-manager", "runner"):
        assert (
            services[name]["environment"]["AGENT_SANDBOX_MANAGER_TOKEN"]
            == "test-manager-credential"
        )
    values.pop("AGENT_SANDBOX_MANAGER_TOKEN")
    refused = render()
    assert refused.returncode != 0
    assert "AGENT_SANDBOX_MANAGER_TOKEN" in refused.stderr


CONTROL_PLANE = REPO / "deploy/control-plane/docker-compose.yml"
HOST = REPO / "deploy/host/docker-compose.sandbox.yml"

#: `${VAR:?message}` — the form that refuses to render when unset.
_REQUIRED = re.compile(r"\$\{([A-Z_][A-Z0-9_]*):\?")

#: Required by a compose file, but exported by the script that runs it rather
#: than written by an operator: deploy.sh exports the commit it deploys, and
#: the release deployer the one its manifest names. A Compose command run by
#: hand still fails on it with its own message.
EXPORTED_BY_DEPLOYER = {"AGENT_DEPLOYMENT_SHA"}

#: The two relays a host reaches, each spelled as a prefix in server setup.
_TUNNELS = ("AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL")


def test_a_new_control_plane_needs_no_env_file() -> None:
    """Its keys come from the `keys` service and everything else from Admin, so
    the only thing a first start needs is the commit its deployer exports."""
    required = set(_REQUIRED.findall(CONTROL_PLANE.read_text()))
    assert required, "no required variables found — regex drift?"
    assert required <= EXPORTED_BY_DEPLOYER, (
        f"{CONTROL_PLANE.name} now refuses to start without {sorted(required - EXPORTED_BY_DEPLOYER)}; "
        "a new installation has no env file to set it in"
    )


def test_the_deployer_exports_what_the_examples_leave_out() -> None:
    script = (REPO / "deploy/control-plane/deploy.sh").read_text()
    for name in EXPORTED_BY_DEPLOYER:
        assert f"export {name}" in script, name


def _written_by_server_setup() -> set[str]:
    """Every name automatic server setup or the host bootstrap writes into a host's .env."""
    setup = (REPO / "apps/backend/cloud_agent/application/compute_setup.py").read_text()
    bootstrap = (REPO / "apps/backend/cloud_agent/providers/compute/host_bootstrap.py").read_text()
    names = set(re.findall(r'"([A-Z][A-Z0-9_]+)":', setup + bootstrap))
    suffixes = set(re.findall(r'prefix\s*\+\s*"(_[A-Z_]+)"', setup))
    assert suffixes, (
        "server setup no longer spells the tunnel names as prefix + suffix — regex drift?"
    )
    return names | {prefix + suffix for prefix in _TUNNELS for suffix in suffixes}


def test_server_setup_writes_every_variable_a_host_cannot_start_without() -> None:
    """A host's .env is written by Admin → Servers → Add server, never by hand,
    so a new required variable must be one it writes."""
    required = set(_REQUIRED.findall(HOST.read_text()))
    assert required, "no required variables found — regex drift?"
    missing = required - _written_by_server_setup() - EXPORTED_BY_DEPLOYER
    assert not missing, (
        f"{HOST.name} refuses to start without {sorted(missing)}, which server setup never writes"
    )
