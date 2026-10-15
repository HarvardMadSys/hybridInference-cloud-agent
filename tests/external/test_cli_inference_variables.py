"""Each shipped agent CLI keeps the platform credential; its commands get the owner's.

An owner may set OPENAI_API_KEY, ANTHROPIC_API_KEY and their base URLs for
their own code. The CLIs read the platform's grant under those same names, so
a launch keeps the owner's values out of the CLI and hands them to the commands
it runs through ``BASH_ENV`` (``inference_handoff``). That relies on each pinned
CLI running its shell tool through bash; this checks it against the real
binaries in the sandbox image, after a CLI version bump in particular.

Run by hand with ``MCP_TEST_SANDBOX_IMAGE=<image> pytest -m external
tests/external/test_cli_inference_variables.py``. Only the model is fake:
adapters, wrappers and CLIs are the shipped ones, launched as the sandbox
manager launches them (``/bin/sh`` exec, never bash).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess

import pytest

from cloud_agent_host.runtimes import get_runtime
from cloud_agent_shared.environment import inference_handoff

pytestmark = pytest.mark.external
_SUPPORT = pathlib.Path(__file__).resolve().parents[1] / "support"


def _fake(label: str) -> str:
    """A placeholder credential, built so a secret scanner never sees a literal."""
    return f"{label}-{'0123456789' * 2}"


_PLATFORM = _fake("platform-grant")
_OWNER = {
    "OPENAI_API_KEY": _fake("owner-openai"),
    "OPENAI_BASE_URL": "http://127.0.0.1:8901/v1",
    "ANTHROPIC_API_KEY": _fake("owner-anthropic"),
    "ANTHROPIC_AUTH_TOKEN": _fake("owner-anthropic-token"),
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8901",
}
_BASH_ENV = "/tmp/members/inference-env.sh"


def _image() -> str:
    image = os.environ.get("MCP_TEST_SANDBOX_IMAGE")
    if not image:
        pytest.skip("set MCP_TEST_SANDBOX_IMAGE to the sandbox image under test")
    return image


def _turn(runtime: str, owner: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """Run one turn; return the gateway log and what the shell command saw."""
    argv, cli_env = get_runtime(runtime).prepare(
        workdir="/tmp/work",
        task_prompt="Run the probe command.",
        model="test-model",
        gateway_base_url="http://127.0.0.1:8900",
        credential=_PLATFORM,
        provides_isolation=True,
    )
    rest, script = inference_handoff(owner)
    env = {"HOME": "/tmp/home", **rest, **cli_env, "BASH_ENV": _BASH_ENV}
    setup = (
        "mkdir -p /tmp/home /tmp/work /tmp/members && cd /tmp/work && git init -q . && "
        f"printf %s {shlex.quote(script)} > {_BASH_ENV} && "
        "{ python3 /support/shell_probe_gateway.py /tmp/gateway.log & } && sleep 1 && "
        "timeout 120 /bin/sh -c 'exec \"$@\" </dev/null' sh "
        + " ".join(shlex.quote(part) for part in argv)
        + " >/tmp/cli.log 2>&1; status=$?; cat /tmp/gateway.log; "
        "[ $status = 0 ] || { tail -c 2000 /tmp/cli.log >&2; exit $status; }"
    )
    command = ["docker", "run", "--rm", "--network", "none"]
    command += ["-v", f"{_SUPPORT}:/support:ro"]
    for name, value in env.items():
        command += ["-e", f"{name}={value}"]
    command += ["--entrypoint", "bash", _image(), "-c", setup]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    assert result.returncode == 0, result.stderr
    log = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    probes = [
        match.group(1)
        for entry in log
        if "tool_result" in entry
        for match in [re.search(r"PROBE ([^\"\\]*)", entry["tool_result"])]
        if match
    ]
    assert probes, result.stdout
    seen = dict(pair.split("=", 1) for pair in probes[-1].split())
    return log, seen


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "opencode", "kilo", "pi"])
def test_the_cli_uses_the_platform_grant_and_its_commands_the_owners_values(runtime):
    log, seen = _turn(runtime, _OWNER)
    requests = [entry for entry in log if "port" in entry]
    assert requests and all(entry["port"] == 8900 for entry in requests), requests
    for entry in requests:
        credentials = {entry["authorization"], entry["x-api-key"]} - {None}
        assert credentials in ({f"Bearer {_PLATFORM}"}, {_PLATFORM}), entry
    assert seen == _OWNER


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "opencode", "kilo", "pi"])
def test_an_owner_key_alone_is_never_paired_with_the_gateway_address(runtime):
    _log, seen = _turn(runtime, {"OPENAI_API_KEY": _OWNER["OPENAI_API_KEY"]})
    assert seen["OPENAI_API_KEY"] == _OWNER["OPENAI_API_KEY"]
    assert seen["OPENAI_BASE_URL"] == "unset"
