"""Local-account host activation keeps every remaining readiness barrier."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from tests.unit.test_retained_deploy import (
    HOST_COMPOSE,
    _add_required_host_paths,
    _host,
    _run,
)


def local_host(tmp_path, **changes):
    env = _host(tmp_path, **changes)
    env["AGENT_DEPLOY_PYTHON"] = sys.executable
    env.pop("AGENT_GATEWAY_URL", None)
    env.pop("AGENT_AUTH_MODE", None)
    _add_required_host_paths(env, tmp_path)
    root = Path(env["APP_DIR"])
    recipe = root / "deploy/host/docker-compose.sandbox.yml"
    recipe.parent.mkdir(parents=True)
    shutil.copyfile(HOST_COMPOSE, recipe)
    dotenv = root / ".env"
    dotenv.write_text(
        "\n".join(
            line for line in dotenv.read_text().splitlines() if not line.startswith("AGENT_TUNNEL_")
        )
        + "\nAGENT_AUTH_MODE=local\n"
    )
    return env


def test_local_host_checks_mcp_and_control_without_an_inference_tunnel(tmp_path):
    env = local_host(tmp_path)
    calls = tmp_path / "docker-calls"
    env["DOCKER_CALL_LOG"] = str(calls)
    result = _run(env, "check")
    assert result.returncode == 0, result.stderr
    assert "MCP-only relay" in result.stdout
    assert "shared-live ready" in result.stdout
    assert "inference gateway" not in result.stdout
    log = calls.read_text()
    assert "agent-gateway" not in log
    assert "AGENT_CONTROL_PLANE_URL" in log
    assert "_shared_live_host_capabilities" in log
    assert "--turn" in log


@pytest.mark.parametrize(
    "failure",
    [
        {"turn_starts": False},
        {"credential_rejected": True},
        {"sandbox_manager_serving": False},
    ],
)
def test_local_mode_does_not_skip_host_readiness_failures(tmp_path, failure):
    result = _run(local_host(tmp_path, **failure), "check")
    assert result.returncode != 0
    assert "shared-live ready" not in result.stdout


@pytest.mark.parametrize("prefix", ["AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL"])
def test_local_mode_still_requires_both_scoped_tunnel_credentials(tmp_path, prefix):
    env = local_host(tmp_path)
    dotenv = Path(env["APP_DIR"]) / ".env"
    dotenv.write_text(
        "\n".join(
            line
            for line in dotenv.read_text().splitlines()
            if not line.startswith(prefix + "_SSH_KEY=")
        )
        + "\n"
    )
    result = _run(env, "prepare")
    assert result.returncode != 0
    assert prefix + "_SSH_KEY" in result.stderr
