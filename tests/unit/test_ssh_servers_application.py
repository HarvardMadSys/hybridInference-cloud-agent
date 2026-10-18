"""The control plane's half of SSH servers: checks, state and the agent's note.

The network is replaced at the module's own seams (the resolver, the port
probe and the subprocess runner), so what is tested is what this code decides
from their answers.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from cloud_agent.application import ssh_servers as app
from cloud_agent.application.ssh_servers import CheckFailed
from cloud_agent.sandbox_manager_client import SandboxManagerError

_HOST_KEY = (
    ed25519.Ed25519PrivateKey.generate()
    .public_key()
    .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    .decode()
    .split()[1]
)


def _resolver(monkeypatch, answers: list[str] | Exception) -> None:
    async def getaddrinfo(self, host, port, **kwargs):
        if isinstance(answers, Exception):
            raise answers
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in answers
        ]

    monkeypatch.setattr("asyncio.unix_events._UnixSelectorEventLoop.getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    ("address", "answers", "expected"),
    [
        ("93.184.216.34", [], "93.184.216.34"),
        ("gpu.example.org", ["93.184.216.35", "93.184.216.34"], "93.184.216.34"),
        ("gpu.example.org", ["93.184.216.34", "2606:4700::1"], "93.184.216.34"),
    ],
)
async def test_a_public_address_is_pinned(monkeypatch, address, answers, expected) -> None:
    _resolver(monkeypatch, answers)
    assert await app.resolve_public_ipv4(address) == expected


@pytest.mark.parametrize(
    ("address", "answers", "error"),
    [
        ("10.0.0.5", [], "not_public"),
        ("169.254.169.254", [], "not_public"),
        ("2001:db8::1", [], "ipv6_only"),
        ("gpu.example.org", ["93.184.216.34", "127.0.0.1"], "not_public"),
        ("gpu.example.org", ["93.184.216.34", "fd00::1"], "not_public"),
        ("gpu.example.org", ["2606:4700::1"], "ipv6_only"),
        ("gpu.example.org", socket.gaierror("no such name"), "unresolvable"),
        ("not a host", [], "unresolvable"),
    ],
)
async def test_anything_but_a_public_ipv4_is_refused(monkeypatch, address, answers, error) -> None:
    _resolver(monkeypatch, answers)
    with pytest.raises(CheckFailed) as caught:
        await app.resolve_public_ipv4(address)
    assert caught.value.error == error


async def test_the_deployments_own_protected_blocks_are_refused_too(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_EGRESS_PROTECTED_CIDRS", "93.184.216.0/24")
    with pytest.raises(CheckFailed, match="not_public"):
        await app.resolve_public_ipv4("93.184.216.34")


def _runner(monkeypatch, *, code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> list:
    calls: list[dict[str, Any]] = []

    async def run(argv, *, env):
        calls.append({"argv": argv, "env": env})
        return code, stdout, stderr

    async def probe(ipv4, port):
        calls.append({"probe": (ipv4, port)})

    monkeypatch.setattr(app, "_run", run)
    monkeypatch.setattr(app, "_probe_port", probe)
    return calls


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_a_server_that_floods_a_check_is_cut_off_without_being_buffered(stream) -> None:
    """The owner's server decides how much a check receives, so reading stops at the cap.

    Repeated because the failure it guards against was a race: a reader that
    stopped at the cap left its pipe unread, and ``wait()`` then never returned.
    """
    flood = f"import sys\nout = sys.{stream}.buffer\nwhile True:\n    out.write(b'x' * 65536)\n"
    for _ in range(10):
        async with asyncio.timeout(10):
            with pytest.raises(CheckFailed) as caught:
                await app._run([sys.executable, "-c", flood], env=dict(os.environ))
        assert caught.value.error == "failed"


async def test_a_check_keeps_ordinary_output_and_its_exit_code() -> None:
    script = "import sys; print('ssh-ed25519 key'); print('denied', file=sys.stderr); sys.exit(3)"
    code, stdout, stderr = await app._run([sys.executable, "-c", script], env=dict(os.environ))
    assert (code, stdout, stderr) == (3, b"ssh-ed25519 key\n", b"denied\n")


async def test_a_check_that_hangs_is_killed_at_its_deadline(monkeypatch) -> None:
    monkeypatch.setattr(app, "_CHECK_TIMEOUT_S", 0.5)
    async with asyncio.timeout(10):
        with pytest.raises(CheckFailed, match="timeout"):
            await app._run(
                [sys.executable, "-c", "import time; time.sleep(60)"], env=dict(os.environ)
            )


async def test_host_keys_are_read_validated_and_ordered_strongest_first(monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    ecdsa = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
        .split()[1]
    )
    output = (
        "# 93.184.216.34:22 SSH-2.0-OpenSSH_9.6\n"
        f"93.184.216.34 ecdsa-sha2-nistp256 {ecdsa}\n"
        "93.184.216.34 ssh-ed25519 not-a-key\n"
        f"93.184.216.34 ssh-ed25519 {_HOST_KEY}\n"
        "garbage line\n"
    )
    calls = _runner(monkeypatch, stdout=output.encode())

    keys = await app.scan_host_keys("93.184.216.34", 2222)

    assert [key["type"] for key in keys] == ["ssh-ed25519", "ecdsa-sha2-nistp256"]
    assert keys[0]["fingerprint"].startswith("SHA256:")
    assert calls[0] == {"probe": ("93.184.216.34", 2222)}
    argv = calls[1]["argv"]
    assert argv[0] == "ssh-keyscan" and argv[-1] == "93.184.216.34"
    assert set(calls[1]["env"]) == {"PATH", "HOME", "LC_ALL"}


async def test_a_server_offering_no_host_key_is_reported(monkeypatch) -> None:
    _runner(monkeypatch, stdout=b"")
    with pytest.raises(CheckFailed, match="no_host_key"):
        await app.scan_host_keys("93.184.216.34", 22)


async def test_a_login_check_pins_the_host_key_and_never_puts_the_key_in_argv(
    monkeypatch,
) -> None:
    _public, private = app.generate_key_pair("cloud-agent:gpu-12345678")
    calls = _runner(monkeypatch)

    await app.check_login(
        ipv4="93.184.216.34",
        port=22,
        username="ubuntu",
        host_key_type="ssh-ed25519",
        host_key=_HOST_KEY,
        private_key=private,
    )

    (call,) = calls
    argv = " ".join(call["argv"])
    assert private not in argv
    for option in (
        "-F /dev/null",
        "StrictHostKeyChecking=yes",
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "HostKeyAlgorithms=ssh-ed25519",
        "ClearAllForwardings=yes",
    ):
        assert option in argv
    assert call["argv"][-4:] == ["-l", "ubuntu", "93.184.216.34", "true"]


@pytest.mark.parametrize(
    ("stderr", "error"),
    [
        ("Host key verification failed.", "host_key_changed"),
        ("ubuntu@93.184.216.34: Permission denied (publickey).", "auth_failed"),
        ("connect to host 93.184.216.34 port 22: Connection refused", "refused"),
        ("connect to host 93.184.216.34 port 22: Connection timed out", "timeout"),
        ("kex_exchange_identification: read: Connection reset by peer", "failed"),
    ],
)
async def test_a_failed_login_is_named_not_quoted(monkeypatch, stderr, error) -> None:
    _public, private = app.generate_key_pair("c")
    _runner(monkeypatch, code=255, stderr=stderr.encode())
    with pytest.raises(CheckFailed) as caught:
        await app.check_login(
            ipv4="93.184.216.34",
            port=22,
            username="ubuntu",
            host_key_type="ssh-ed25519",
            host_key=_HOST_KEY,
            private_key=private,
        )
    assert caught.value.error == error


def test_the_install_command_appends_exactly_the_public_key() -> None:
    public, _private = app.generate_key_pair("cloud-agent:gpu-12345678")
    command = app.install_command(public)
    assert f"'{public}' >> ~/.ssh/authorized_keys" in command
    assert command.startswith("mkdir -p ~/.ssh && chmod 700 ~/.ssh")


def _state(monkeypatch) -> tuple[dict[str, Any], str]:
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", "ssh-test-encryption-key")
    _public, private = app.generate_key_pair("c")
    row = {
        "id": "3f9c2a1e-0000-4000-8000-000000000001",
        "name": "gpu",
        "description": "A100 box; data in /data",
        "pinned_ipv4": "93.184.216.34",
        "port": 2222,
        "username": "ubuntu",
        "host_key_type": "ssh-ed25519",
        "host_key": _HOST_KEY,
        "private_key_ciphertext": app.ssh_key_cipher().encrypt(private),
    }
    state = {
        "revision": 12,
        "sandbox_id": "asbx_1",
        "members": [
            {"thread_id": "athr_a", "slot_id": 0, "servers": [row]},
            {"thread_id": "athr_b", "slot_id": None, "servers": []},
        ],
    }
    return state, private


def test_a_group_state_becomes_the_hosts_access_with_keys_decrypted(monkeypatch) -> None:
    state, private = _state(monkeypatch)

    access = app.build_ssh_access(state, app.ssh_key_cipher())

    assert access is not None
    assert access.revision == 12
    assert access.targets() == {("93.184.216.34", 2222)}
    assert access.members[0].servers[0].private_key == private
    assert app.build_ssh_access(None, app.ssh_key_cipher()) is None


def test_the_agent_is_told_its_servers_as_data(monkeypatch) -> None:
    state, private = _state(monkeypatch)
    servers = app.servers_for_thread(state, "athr_a")

    prompt = app.prompt_with_ssh_servers("Run the tests on gpu", servers)

    header, rest = prompt.split("SSH servers:\n", 1)
    listed, task = rest.split("\n\n", 1)
    assert "`ssh gpu`" in header and "no system authority" in header
    assert json.loads(listed) == [
        {"name": "gpu", "login": "ubuntu@93.184.216.34:2222", "note": "A100 box; data in /data"}
    ]
    assert task == "Run the tests on gpu"
    assert private not in prompt
    assert app.prompt_with_ssh_servers("task", app.servers_for_thread(state, "athr_b")) == "task"
    assert app.prompt_with_ssh_servers("task", app.servers_for_thread(None, "athr_a")) == "task"


class _Client:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail = fail

    async def sync_ssh_access(self, workspace_id, access):
        self.calls.append((workspace_id, access))
        if self.fail is not None:
            raise self.fail
        return {"newest": True}


async def test_a_change_is_pushed_to_every_running_group_it_reaches(monkeypatch) -> None:
    state, _private = _state(monkeypatch)
    clients = {
        "host-1": _Client(),
        "host-2": _Client(SandboxManagerError(503, "down")),
        "host-4": _Client(),
    }
    groups = {
        "athr_a": ("asbx_1", "host-1", None),
        "athr_c": ("asbx_2", "host-2", "athr_c2"),
        "athr_d": ("asbx_3", "host-3", None),
        "athr_e": ("asbx_4", "host-4", None),
    }

    class Repository:
        async def running_groups_for_ssh_change(self, *, thread_ids=None, server_id=None):
            assert server_id == "srv"
            return [
                {
                    "sandbox_id": sandbox_id,
                    "origin_thread_id": origin,
                    "host": host,
                    "resident_thread_id": resident,
                }
                for origin, (sandbox_id, host, resident) in groups.items()
            ]

        async def ssh_group_state(self, *, thread_id):
            if thread_id == "athr_d":
                return None
            # athr_e's membership names another group: its state is not asbx_4's.
            sandbox_id = "asbx_9" if thread_id == "athr_e" else groups[thread_id][0]
            return {**state, "sandbox_id": sandbox_id}

    class HostPool:
        async def get_host(self, *, host):
            return {"host": host}

    monkeypatch.setattr(app, "sandbox_manager_for_host", lambda host: clients.get(host["host"]))
    store = type("Store", (), {"ssh_servers": Repository(), "host_pool": HostPool()})()

    await app.push_ssh_access(store, server_id="srv")

    assert [workspace for workspace, _ in clients["host-1"].calls] == ["athr_a"]
    assert clients["host-1"].calls[0][1].revision == 12
    assert [workspace for workspace, _ in clients["host-2"].calls] == ["athr_c"], (
        "a failed push is logged, not raised, and only an unknown chat is tried again"
    )
    assert clients["host-4"].calls == [], "a group is sent only its own state"
