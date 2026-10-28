"""The local host recipe removes inference and retains both control boundaries."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from deploy.host.inference_compose import local_recipe
from deploy.release.artifact_compose import recipe_template
from deploy.release.build_host_bundle import HOST_FILES as HOST_BUNDLE_FILES
from deploy.release.build_recipe import RECIPE_FILES

REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "deploy/host/docker-compose.sandbox.yml"
RENDERER = REPO / "deploy/host/inference_compose.py"


def test_local_transformation_changes_only_the_unused_inference_route():
    original = yaml.safe_load(SOURCE.read_text())
    rendered = local_recipe(original)
    expected = copy.deepcopy(original)
    del expected["services"]["gateway-tunnel"]
    del expected["services"]["runner"]["depends_on"]
    for name in ("runner", "sandbox-manager"):
        expected["services"][name]["environment"].update(
            AGENT_AUTH_MODE="local", AGENT_GATEWAY_URL=""
        )
    assert rendered == expected
    assert "gateway-tunnel" in original["services"]
    assert "agent-relays" not in rendered["services"]["control-plane-tunnel"]["networks"]
    assert rendered["services"]["mcp-tunnel"] == original["services"]["mcp-tunnel"]


def test_both_distributed_host_recipes_include_the_local_renderer():
    assert "deploy/host/inference_compose.py" in RECIPE_FILES
    assert "deploy/host/inference_compose.py" in HOST_BUNDLE_FILES


def _compose_environment(tmp_path):
    # No credentials from the invoking environment enter Compose. Gateway's
    # required substitutions deliberately remain absent in both branches.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for name in re.findall(r"\$\{([A-Z_]+):\?", SOURCE.read_text()):
        if not name.startswith("AGENT_TUNNEL_"):
            env[name] = "fixture"
            if name.endswith(("_SSH_KEY", "_SSH_KNOWN_HOSTS")):
                path = tmp_path / name.lower()
                path.write_text("test-only tunnel credential fixture\n")
                env[name] = str(path.resolve())
    env.update(
        COMPOSE_PROJECT="local-inference-test",
        AGENT_SANDBOX_IMAGE="sandbox:test",
        AGENT_EGRESS_CHAT_NETWORK_POOL="10.249.0.0/16",
        AGENT_EGRESS_DNS_SERVER="1.1.1.1",
        AGENT_RUNNER_IMAGE="example/runner@sha256:" + "a" * 64,
        AGENT_TUNNEL_IMAGE="example/tunnel@sha256:" + "b" * 64,
    )
    return env


@pytest.mark.parametrize("artifact", [False, True])
def test_real_compose_accepts_local_without_gateway_inputs_but_default_refuses(tmp_path, artifact):
    docker = shutil.which("docker")
    if (
        docker is None
        or subprocess.run(
            [docker, "compose", "version"], capture_output=True, check=False
        ).returncode
    ):
        pytest.skip("Docker Compose CLI is required; no daemon is used")
    source = tmp_path / "deploy/host/docker-compose.sandbox.yml"
    source.parent.mkdir(parents=True)
    document = yaml.safe_load(SOURCE.read_text())
    if artifact:
        document = recipe_template(document, "host")
    source.write_text(json.dumps(document))
    output = source.parent / ".local-inference.json"
    subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--compose",
            str(source),
            "--auth-mode",
            "local",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    env = _compose_environment(tmp_path)
    command = [docker, "compose", "-p", "local-inference-test", "--env-file", "/dev/null", "-f"]
    local = subprocess.run(
        [*command, str(output), "config", "--format", "json"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert local.returncode == 0, local.stderr
    config = json.loads(local.stdout)
    assert set(config["services"]) == {
        "runner",
        "sandbox-manager",
        "mcp-tunnel",
        "control-plane-tunnel",
    }
    for name in ("runner", "sandbox-manager"):
        assert config["services"][name]["environment"]["AGENT_GATEWAY_URL"] == ""
        assert config["services"][name]["environment"]["AGENT_AUTH_MODE"] == "local"
    control_networks = config["services"]["control-plane-tunnel"]["networks"]
    assert set(control_networks) == {"internal"}
    assert control_networks["internal"]["aliases"] == ["agent-control-plane"]
    gateway = subprocess.run(
        [*command, str(source), "config", "--format", "json"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert gateway.returncode != 0
    assert "AGENT_TUNNEL_" in gateway.stderr


def test_renderer_refuses_to_move_relative_paths_to_a_different_directory(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--compose",
            str(SOURCE),
            "--auth-mode",
            "local",
            "--output",
            str(tmp_path / "local.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must stay beside" in result.stderr
