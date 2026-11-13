"""Retained Codex containers admit this deployment's MCP relay and the sandbox gateway, only.

The guest script runs here against a private root: the four constants that
name the guest's paths and their owner are pointed at a temporary directory,
and every other line is the script the host sends.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from cloud_agent_host.runtimes.codex.mcp import INSTALL_POLICY, SANDBOX_MCP_URL, relay_endpoint

REPO = Path(__file__).resolve().parents[2]
IMAGE_DEFAULT = (REPO / "deploy/sandbox/codex-requirements.toml").read_text()
OLD = relay_endpoint("http://mcp-old.internal:18093")
NEW = relay_endpoint("http://mcp.internal:18094")


def _relay_policy(url: str) -> str:
    """The policy text the script wrote for ``url`` before the sandbox gateway."""
    return "[mcp_servers.session]\nidentity = { url = " + json.dumps(url) + " }\n"


def _policy(url: str) -> str:
    """The policy text the script writes for ``url``."""
    return (
        _relay_policy(url)
        + "\n[mcp_servers.sandbox]\nidentity = { url = "
        + json.dumps(SANDBOX_MCP_URL)
        + " }\n"
    )


ADMITTED = {
    "mcp_servers": {
        "session": {"identity": {"url": NEW}},
        "sandbox": {"identity": {"url": SANDBOX_MCP_URL}},
    }
}


def _install(
    tmp_path: Path,
    existing: str,
    *,
    processes: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the guest script for ``NEW``; return its result and the policy after it."""
    etc = tmp_path / "etc"
    codex = etc / "codex"
    codex.mkdir(parents=True)
    etc.chmod(0o755)
    codex.chmod(0o755)
    policy = codex / "requirements.toml"
    policy.write_text(existing)
    policy.chmod(0o644)
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, (comm, argv) in enumerate(processes, start=100):
        entry = proc / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(comm + "\n")
        (entry / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
    script = INSTALL_POLICY
    for constant, value in {
        'POLICY_DIRECTORIES = ("/etc", "/etc/codex")': (
            f"POLICY_DIRECTORIES = ({str(etc)!r}, {str(codex)!r})"
        ),
        'POLICY = "/etc/codex/requirements.toml"': f"POLICY = {str(policy)!r}",
        'PROCESSES = "/proc"': f"PROCESSES = {str(proc)!r}",
        "OWNER_UID = 0": f"OWNER_UID = {os.getuid()}",
    }.items():
        assert script.count(constant) == 1, constant
        script = script.replace(constant, value)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, NEW, SANDBOX_MCP_URL],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, policy.read_text()


def test_the_image_default_admits_exactly_the_relay_and_the_gateway(tmp_path):
    result, policy = _install(tmp_path, IMAGE_DEFAULT)

    assert result.returncode == 0, result.stderr
    assert tomllib.loads(policy) == ADMITTED


def test_the_current_policy_is_left_as_it_is(tmp_path):
    current = _policy(NEW)
    result, policy = _install(tmp_path, current, processes=(("codex", ("codex", "app-server")),))

    assert result.returncode == 0, result.stderr
    assert policy == current


def test_the_gateway_is_added_to_the_same_relay_under_a_running_sibling(tmp_path):
    """Adding the fixed gateway entry changes nothing a running member relies on."""
    result, policy = _install(
        tmp_path, _relay_policy(NEW), processes=(("codex", ("codex", "app-server")),)
    )

    assert result.returncode == 0, result.stderr
    assert tomllib.loads(policy) == ADMITTED


def test_a_moved_relay_replaces_the_platforms_own_policy(tmp_path):
    """Retained Codex containers follow the deployment's relay when it moves.

    The policy names the relay URL, which is host configuration a retained
    container outlives. Refusing every policy that differed left each Codex
    group that had ever used MCP unable to use it again after the move.
    """
    result, policy = _install(tmp_path, _policy(OLD))

    assert result.returncode == 0, result.stderr
    assert tomllib.loads(policy) == ADMITTED


@pytest.mark.parametrize(
    "sibling",
    [
        ("codex", ("/opt/runtimes/codex/0.145.0/vendor/codex", "app-server")),
        ("node", ("node", "/opt/runtimes/codex/0.145.0/bin/codex", "app-server")),
    ],
)
def test_a_moved_relay_is_not_replaced_under_a_running_sibling(tmp_path, sibling):
    old = _policy(OLD)
    result, policy = _install(tmp_path, old, processes=(("bash", ("bash",)), sibling))

    assert result.returncode != 0
    assert "belongs to another relay" in result.stderr
    assert policy == old


@pytest.mark.parametrize(
    "existing",
    [
        '[mcp_servers.other]\nidentity = { url = "http://elsewhere/v1/agent/mcp" }\n',
        _relay_policy(OLD) + '[mcp_servers.extra]\ncommand = "/bin/sh"\n',
        '[mcp_servers.session]\ncommand = "/bin/sh"\n',
        '[mcp_servers.session]\nidentity = { url = "http://x", headers = { a = "b" } }\n',
        'allowed_approval_policies = ["never"]\n' + _relay_policy(OLD),
        # Only the fixed gateway address is admitted under the name `sandbox`.
        _relay_policy(OLD) + '[mcp_servers.sandbox]\nidentity = { url = "http://evil/mcp" }\n',
        '[mcp_servers.sandbox]\nidentity = { url = "http://127.0.0.1:47801/mcp" }\n',
    ],
)
def test_a_policy_that_is_not_the_platforms_own_is_never_replaced(tmp_path, existing):
    result, policy = _install(tmp_path, existing)

    assert result.returncode != 0
    assert policy == existing
