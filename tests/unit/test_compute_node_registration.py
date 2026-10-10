"""A new node's SSH host keys reach the control plane only through its one-time token.

Covers the pieces that run without a database or a real node: key validation,
the command's answers, where nodes are told to register, the CloudLab profile
that delivers the token, and the boot script itself against fake tools.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import registration_url
from cloud_agent.domain.compute import ComputeRefusal, normalize_host_keys
from cloud_agent.host_pool import hash_secret

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "deploy" / "cloudlab" / "cloud-agent-node.py"
TOKEN = "t" * 43


def openssh(key: object) -> str:
    return (
        key.public_key()  # type: ignore[attr-defined]
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


ED25519 = openssh(ed25519.Ed25519PrivateKey.generate())
ECDSA = openssh(ec.generate_private_key(ec.SECP256R1()))
RSA = openssh(rsa.generate_private_key(public_exponent=65537, key_size=3072))


def test_host_keys_are_parsed_deduplicated_and_stripped_of_comments():
    keys = normalize_host_keys([RSA + " root@node0", ED25519, ECDSA, ED25519 + " again"])
    assert keys == sorted({ED25519, ECDSA, RSA})


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["not a key"],
        ["ssh-ed25519 !!!"],
        # The algorithm label must match the key inside the blob.
        ["ssh-rsa " + ED25519.split()[1]],
        [openssh(rsa.generate_private_key(public_exponent=65537, key_size=1024))],
    ],
)
def test_unparseable_mislabelled_or_weak_host_keys_are_refused(lines):
    with pytest.raises(ComputeRefusal) as refused:
        normalize_host_keys(lines)
    assert refused.value.status == 422


def commands(outcome: str) -> tuple[ComputeCommands, AsyncMock]:
    repository = SimpleNamespace(complete_node_registration=AsyncMock(return_value=outcome))
    return (
        ComputeCommands(
            repository=repository,  # type: ignore[arg-type]
            registry=SimpleNamespace(),  # type: ignore[arg-type]
            secrets=SimpleNamespace(),  # type: ignore[arg-type]
            configuration=SimpleNamespace(observation_seconds=120),  # type: ignore[arg-type]
        ),
        repository.complete_node_registration,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["registered", "replayed"])
async def test_registration_stores_only_the_token_hash_and_accepts_a_repeat(outcome):
    command, complete = commands(outcome)
    await command.register_node(TOKEN, [ED25519])
    complete.assert_awaited_once_with(token_hash=hash_secret(TOKEN), host_keys=[ED25519])
    assert TOKEN not in json.dumps(complete.await_args.kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status,code",
    [
        ("conflict", 409, "registration_conflict"),
        ("invalid", 404, "registration_not_found"),
    ],
)
async def test_registration_refusals_reveal_nothing_about_other_tokens(outcome, status, code):
    command, _ = commands(outcome)
    with pytest.raises(ComputeRefusal) as refused:
        await command.register_node(TOKEN, [ED25519])
    assert (refused.value.status, refused.value.code) == (status, code)


@pytest.mark.asyncio
async def test_a_malformed_token_is_refused_like_an_unknown_one_without_a_lookup():
    command, complete = commands("registered")
    with pytest.raises(ComputeRefusal) as refused:
        await command.register_node("short", [ED25519])
    assert refused.value.code == "registration_not_found"
    complete.assert_not_awaited()


@pytest.mark.parametrize(
    "environment,expected",
    [
        (
            {"AGENT_SESSION_REDIRECT_URI": "https://example.org/agents/api/v1/session/callback"},
            "https://example.org/agents/api/v1/compute/node-registrations",
        ),
        # A node checks the control plane's certificate before sending its token.
        ({"AGENT_SESSION_REDIRECT_URI": "http://example.org/v1/session/callback"}, ""),
        ({"AGENT_SESSION_REDIRECT_URI": "https://example.org/elsewhere"}, ""),
        (
            {
                "AGENT_SESSION_REDIRECT_URI": "https://example.org/v1/session/callback",
                "AGENT_COMPUTE_REGISTRATION_URL": "https://nodes.example.org/register",
            },
            "https://nodes.example.org/register",
        ),
        ({"AGENT_COMPUTE_REGISTRATION_URL": "http://nodes.example.org/register"}, ""),
    ],
)
def test_nodes_register_only_with_an_https_public_address(monkeypatch, environment, expected):
    for name in ("AGENT_SESSION_REDIRECT_URI", "AGENT_COMPUTE_REGISTRATION_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert registration_url() == expected


class _ParameterError(Exception):
    def __init__(self, message: str, names: list[str]) -> None:
        super().__init__(message)
        self.names = names


def run_profile(bindings: dict[str, object]) -> SimpleNamespace:
    """Execute the profile script against a minimal stand-in for geni-lib."""
    captured = SimpleNamespace(errors=[], services=[], node=None)

    class Context:
        def __init__(self) -> None:
            self.defaults: dict[str, object] = {}

        def defineParameter(self, name, _description, _kind, default, **_options):
            self.defaults[name] = default

        def bindParameters(self):
            return SimpleNamespace(**{**self.defaults, **bindings})

        def reportError(self, error):
            captured.errors.extend(error.names)

        def verifyParameters(self):
            if captured.errors:
                raise SystemExit("invalid parameters")

        def makeRequestRSpec(self):
            def raw_pc(name):
                node = SimpleNamespace(name=name, disks=[])
                node.Blockstore = lambda label, mount: (
                    node.disks.append(SimpleNamespace(label=label, mount=mount)) or node.disks[-1]
                )
                node.addService = captured.services.append
                captured.node = node
                return node

            return SimpleNamespace(RawPC=raw_pc)

        def printRequestRSpec(self, _request):
            return None

    portal = types.ModuleType("geni.portal")
    portal.Context = Context
    portal.ParameterType = SimpleNamespace(NODETYPE="n", IMAGE="i", STRING="s", INTEGER="d")
    portal.ParameterError = _ParameterError
    pg = types.ModuleType("geni.rspec.pg")
    pg.Execute = lambda shell, command: SimpleNamespace(shell=shell, command=command)
    geni = types.ModuleType("geni")
    rspec = types.ModuleType("geni.rspec")
    saved = {
        name: sys.modules.get(name)
        for name in ("geni", "geni.portal", "geni.rspec", "geni.rspec.pg")
    }
    sys.modules.update(
        {"geni": geni, "geni.portal": portal, "geni.rspec": rspec, "geni.rspec.pg": pg}
    )
    try:
        spec = importlib.util.spec_from_file_location("cloud_agent_node_profile", PROFILE)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    captured.register = module.REGISTER
    return captured


URL = "https://example.org/agents/api/v1/compute/node-registrations"


def test_profile_pipes_the_boot_script_to_root_with_quoted_plain_arguments():
    profile = run_profile({"registrationUrl": URL, "registrationToken": TOKEN, "phystype": "r6615"})
    assert profile.node.hardware_type == "r6615"
    assert [disk.mount for disk in profile.node.disks] == ["/mydata"]
    (service,) = profile.services
    prefix, rest = service.command.split(" | base64 -d | sudo bash -s -- ")
    assert base64.b64decode(prefix.removeprefix("echo ")).decode() == profile.register
    assert rest == f"'{URL}' '{TOKEN}'"


@pytest.mark.parametrize(
    "bindings,field",
    [
        (
            {"registrationUrl": "http://example.org/x", "registrationToken": TOKEN},
            "registrationUrl",
        ),
        ({"registrationUrl": URL + "';reboot;'", "registrationToken": TOKEN}, "registrationUrl"),
        ({"registrationUrl": URL, "registrationToken": "short"}, "registrationToken"),
        ({"registrationUrl": URL, "registrationToken": TOKEN + "' x"}, "registrationToken"),
    ],
)
def test_profile_refuses_values_that_could_escape_the_command_line(bindings, field):
    with pytest.raises(SystemExit):
        run_profile(bindings)


def fake_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def boot_once(tmp_path: Path, status: str) -> subprocess.CompletedProcess[str]:
    profile = run_profile({"registrationUrl": URL, "registrationToken": TOKEN})
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    root = tmp_path / "root"
    ssh_dir = root / "etc" / "ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    # ssh-keygen -A -f <root>: write one key of each type under <root>/etc/ssh.
    fake_tool(
        tools,
        "ssh-keygen",
        'echo "$@" >> "$TOOL_LOG"\n'
        'prefix="${@: -1}/etc/ssh"\n'
        f'printf "%s\\n" "{ED25519} root@x" > "$prefix/ssh_host_ed25519_key.pub"\n'
        f'printf "%s\\n" "{RSA} root@x" > "$prefix/ssh_host_rsa_key.pub"\n',
    )
    fake_tool(tools, "systemctl", 'echo systemctl "$@" >> "$TOOL_LOG"\n')
    fake_tool(
        tools,
        "curl",
        f'echo curl "$@" >> "$TOOL_LOG"\ncat > "$BODY_FILE"\nprintf "{status}"\n',
    )
    (ssh_dir / "ssh_host_ed25519_key.pub").write_text("ssh-ed25519 STALE image-key\n")
    environment = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "CLOUD_AGENT_SSH_DIR": str(ssh_dir),
        "CLOUD_AGENT_STATE_DIR": str(tmp_path / "state"),
        "CLOUD_AGENT_KEYGEN_ROOT": str(root),
        "TOOL_LOG": str(tmp_path / "tools.log"),
        "BODY_FILE": str(tmp_path / "body.json"),
    }
    return subprocess.run(
        ["bash", "-s", "--", URL, TOKEN],
        input=profile.register,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_boot_script_registers_fresh_keys_once_over_https(tmp_path):
    first = boot_once(tmp_path, "200")
    assert first.returncode == 0, first.stderr
    body = json.loads((tmp_path / "body.json").read_text())
    assert body == {"token": TOKEN, "host_keys": [ED25519, RSA]}
    log = (tmp_path / "tools.log").read_text()
    assert "--proto =https" in log and "--tlsv1.2" in log and URL in log
    # The token travels in the request body, never on curl's command line.
    assert TOKEN not in log
    assert log.count("-A -f") == 1

    again = boot_once(tmp_path, "200")
    assert again.returncode == 0
    assert (tmp_path / "tools.log").read_text().count("-A -f") == 1  # keys stay the same


@pytest.mark.parametrize("status", ["404", "409", "422"])
def test_boot_script_stops_at_a_definite_refusal(tmp_path, status):
    result = boot_once(tmp_path, status)
    assert result.returncode == 1
    assert "refused" in (tmp_path / "state" / "registration").read_text()


def test_profile_saved_or_used_by_hand_builds_a_node_that_does_not_register():
    # The Portal runs the script with the empty defaults when the profile is saved.
    profile = run_profile({})
    assert profile.node is not None and profile.services == []


@pytest.mark.parametrize(
    "bindings",
    [{"registrationUrl": URL}, {"registrationToken": TOKEN}],
)
def test_profile_refuses_half_a_registration(bindings):
    with pytest.raises(SystemExit):
        run_profile(bindings)
