"""The remote apply script, run for real against a temporary root and a stub ``docker``."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.providers.compute.host_apply_scripts import APPLY_ENVIRONMENT
from cloud_agent.providers.compute.host_bootstrap import SshHostBootstrap

if TYPE_CHECKING:
    from pathlib import Path

IDENTITY = {"allocation_id": "a", "node_id": "n", "host": "gpu-1", "deployment_sha": "0" * 40}
STUB = """#!/bin/sh
echo "$@" >> "$DOCKER_LOG"
case "$*" in
  *" ps -q "*) echo "$(echo "$*" | sed 's/.* //')-id" ;;
  *"ps --format json"*)
    for service in sandbox-manager runner gateway-tunnel mcp-tunnel control-plane-tunnel; do
      state=running
      grep -q "bad-token" .env && state=exited
      echo "{\\"Service\\":\\"$service\\",\\"State\\":\\"$state\\",\\"Health\\":\\"\\"}"
    done ;;
esac
"""


@pytest.fixture()
def host_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / ".compute-secrets").mkdir(parents=True)
    (root / ".compute-identity.json").write_text(json.dumps(IDENTITY))
    (root / ".env").write_text("AGENT_SANDBOX_MANAGER_TOKEN='old-token'\nOTHER='kept'\n")
    (root / ".compute-secrets/mcp.key").write_text("old key\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(STUB)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("DOCKER_LOG", str(tmp_path / "docker.log"))
    return root


def _run(root: Path, **payload) -> dict:
    body = {
        "root": str(root),
        "identity": IDENTITY,
        "project": "ca-node",
        "stable_seconds": 0,
        **payload,
    }
    done = subprocess.run(
        [sys.executable, "-c", APPLY_ENVIRONMENT],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_it_changes_the_listed_key_and_drains_before_it_restarts(host_root, tmp_path) -> None:
    answer = _run(
        host_root,
        changes={"AGENT_SANDBOX_MANAGER_TOKEN_NEXT": "next-token"},
        services=["sandbox-manager", "runner"],
    )
    assert answer == {"applied": True}
    env = (host_root / ".env").read_text()
    assert "AGENT_SANDBOX_MANAGER_TOKEN_NEXT='next-token'" in env and "OTHER='kept'" in env
    assert stat.S_IMODE((host_root / ".env").stat().st_mode) == 0o600
    calls = (tmp_path / "docker.log").read_text().splitlines()
    stops = [i for i, c in enumerate(calls) if c.startswith("stop --time=-1")]
    assert [calls[i] for i in stops] == [
        "stop --time=-1 runner-id",
        "stop --time=-1 sandbox-manager-id",
    ]
    assert stops[-1] < next(i for i, c in enumerate(calls) if "--force-recreate" in c)


def test_it_restores_what_it_replaced_when_the_services_do_not_stay_up(host_root) -> None:
    answer = _run(
        host_root,
        changes={"AGENT_SANDBOX_MANAGER_TOKEN": "bad-token"},
        files={".compute-secrets/mcp.key": "new key\n"},
        services=["runner"],
    )
    assert answer["applied"] is False and answer["reason"] == "services_unhealthy"
    assert answer["restored"] is True
    assert "old-token" in (host_root / ".env").read_text()
    assert (host_root / ".compute-secrets/mcp.key").read_text() == "old key\n"


@pytest.mark.parametrize(
    "payload",
    [
        {"changes": {"AGENT_EGRESS_DNS_SERVER": "1.1.1.1"}},
        {"changes": {"AGENT_SANDBOX_MANAGER_TOKEN": "x'; rm -rf /"}},
        {"remove": ["AGENT_RUNNER_HOST"]},
        {"files": {"../outside": "x"}},
        {"files": {".compute-secrets/mcp.known_hosts": "x"}},
        {"services": ["edge"]},
    ],
)
def test_it_refuses_anything_but_its_short_lists(host_root, payload) -> None:
    before = (host_root / ".env").read_text()
    assert _run(host_root, **payload) == {"applied": False, "reason": "apply_refused"}
    assert (host_root / ".env").read_text() == before


def test_it_acts_only_on_the_deployment_it_was_told_about(host_root) -> None:
    answer = _run(host_root, identity={**IDENTITY, "node_id": "someone-else"})
    assert answer == {"applied": False, "reason": "deployment_identity_mismatch"}


async def test_a_credential_travels_on_stdin_never_in_the_command_line() -> None:
    from cloud_agent.ports.external.compute_provider import NodeObservation, SshConnection
    from cloud_agent.ports.external.host_bootstrap import BootstrapIntent, DeploymentProfile

    transport = AsyncMock()
    transport.run.return_value = json.dumps({"applied": True})
    bootstrap = SshHostBootstrap(transport=transport, resolver=AsyncMock())
    node = NodeObservation(
        provider_node_id="p",
        instance_identity="i",
        display_name="n",
        connection=SshConnection(
            address="10.0.0.1", username="u", known_hosts_ref="r", credential_ref="k"
        ),
    )
    profile = DeploymentProfile(
        id="auto-1",
        label="l",
        source_bundle="stored-profile:auto-1:bundle",
        source_sha256="0" * 64,
        deployment_sha="0" * 40,
        environment_ref="stored-profile:auto-1:environment",
    )
    intent = BootstrapIntent(operation_id="o", allocation_id="a", node_id="n", host="gpu-1")
    await bootstrap.apply_environment(
        node,
        profile,
        intent,
        changes={"AGENT_SANDBOX_MANAGER_TOKEN_NEXT": "a-very-secret-next-token"},
        services=("sandbox-manager",),
    )
    command = transport.run.await_args.args[1]
    assert "a-very-secret-next-token" not in command
    assert b"a-very-secret-next-token" in transport.run.await_args.kwargs["stdin"]
    with pytest.raises(ProviderError):
        await bootstrap.apply_environment(
            node, profile, intent, changes={"AGENT_EGRESS_DNS_SERVER": "1.1.1.1"}
        )
