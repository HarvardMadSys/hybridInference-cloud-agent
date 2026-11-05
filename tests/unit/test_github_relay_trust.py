"""Every process in a sandbox trusts the GitHub relay, credential or not.

The group bridge resolves ``github.com`` to the relay for every process on it,
so a setup script or a temporary workspace's member must accept the relay's
certificate as much as a member holding a placeholder does.
"""

from __future__ import annotations

import os
import stat
import subprocess
from types import SimpleNamespace

import pytest
from tests.unit.test_chat_sandbox import _inspect_payload

from cloud_agent_host.chat_sandbox import (
    MEMBER_UID_BASE,
    ChatSandboxManager,
    ChatSandboxSpec,
)
from cloud_agent_host.exact_container import ProvenContainers
from cloud_agent_host.github_relay_trust import (
    _INSTALL,
    CA_BUNDLE_PATH,
    RELAY_CA_PATH,
    TRUST_DIR,
    ensure_github_relay_trust,
    install_github_relay_trust,
    trust_env,
)
from cloud_agent_host.sandbox import ContainerBackend
from cloud_agent_shared.errors import SandboxError

_CA = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_trust",
        sandbox_id="asbx_trust",
        generation=1,
        runtime_id="claude-code",
        shared_live=True,
        resident_slot_capacity=2,
    )


def _fake_manager(exec_results):
    """The parts of a manager the trust install touches, recording each exec."""
    execs = []

    def exec_capture(spec, argv, **kwargs):
        execs.append({"argv": argv, **kwargs})
        returncode, stderr = exec_results.pop(0) if exec_results else (0, b"")
        return subprocess.CompletedProcess(argv, returncode, b"", stderr)

    proven = ProvenContainers()
    manager = SimpleNamespace(exact=SimpleNamespace(proven=proven), exec_capture=exec_capture)
    return manager, proven, execs


def _relay(monkeypatch, tmp_path, pem=_CA):
    ca = tmp_path / "ca.pem"
    ca.write_text(pem)
    monkeypatch.setenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", str(ca))
    return ca


def test_without_a_relay_nothing_is_installed_or_set(monkeypatch):
    monkeypatch.delenv("AGENT_SANDBOX_GITHUB_RELAY_CA_FILE", raising=False)
    manager, _, execs = _fake_manager([])

    assert ensure_github_relay_trust(manager, _spec()) == {}
    assert execs == []


def test_the_trust_is_installed_once_per_container_and_ca_as_root(monkeypatch, tmp_path):
    ca = _relay(monkeypatch, tmp_path)
    spec = _spec()
    manager, proven, execs = _fake_manager([])
    proven.record(spec, SimpleNamespace(container_id="sha256:one"))

    assert ensure_github_relay_trust(manager, spec) == trust_env()
    assert ensure_github_relay_trust(manager, spec) == trust_env()

    assert len(execs) == 1
    install = execs[0]
    assert install["user_uid"] == 0 and install["user_gid"] == 0
    assert install["input_bytes"] == _CA.encode()
    assert install["argv"][-3:] == [TRUST_DIR, RELAY_CA_PATH, CA_BUNDLE_PATH]

    # A rotated CA is written again rather than taken as current.
    ca.write_text(_CA.replace("AAAA", "BBBB"))
    assert ensure_github_relay_trust(manager, spec) == trust_env()
    assert len(execs) == 2


def test_rolling_the_ca_back_installs_it_again(monkeypatch, tmp_path):
    # A, then B, then A again in one sandbox manager process: the container
    # holds B after the second install, so the third must write A, not take
    # A's earlier record as current.
    first, second = _CA, _CA.replace("AAAA", "BBBB")
    ca = _relay(monkeypatch, tmp_path, first)
    spec = _spec()
    manager, proven, execs = _fake_manager([])
    proven.record(spec, SimpleNamespace(container_id="sha256:one"))

    for pem in (first, second, first):
        ca.write_text(pem)
        assert ensure_github_relay_trust(manager, spec) == trust_env()

    assert [install["input_bytes"] for install in execs] == [
        first.encode(),
        second.encode(),
        first.encode(),
    ]
    # And the CA now in place is remembered: no fourth install.
    assert ensure_github_relay_trust(manager, spec) == trust_env()
    assert len(execs) == 3


def test_a_failed_install_leaves_the_process_on_the_images_roots(monkeypatch, tmp_path):
    _relay(monkeypatch, tmp_path)
    spec = _spec()
    manager, proven, execs = _fake_manager([(1, b"read-only file system")])
    proven.record(spec, SimpleNamespace(container_id="sha256:one"))

    assert ensure_github_relay_trust(manager, spec) == {}
    # Not remembered as installed: the next process tries again.
    assert ensure_github_relay_trust(manager, spec) == trust_env()
    assert len(execs) == 2


def test_the_install_script_writes_a_read_only_bundle_of_the_roots_and_the_ca(tmp_path):
    roots = next(
        (
            path
            for path in (
                "/etc/ssl/certs/ca-certificates.crt",
                "/etc/pki/tls/certs/ca-bundle.crt",
                "/etc/ssl/cert.pem",
            )
            if os.access(path, os.R_OK)
        ),
        None,
    )
    if roots is None:
        pytest.skip("this machine has no system CA bundle")
    directory = tmp_path / "etc-freeinference"
    ca, bundle = directory / "github-relay-ca.pem", directory / "ca-bundle.pem"

    result = subprocess.run(
        ["/bin/sh", "-c", _INSTALL, "sh", str(directory), str(ca), str(bundle)],
        input=_CA.encode(),
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert ca.read_text() == _CA
    with open(roots, "rb") as handle:
        assert bundle.read_bytes() == handle.read() + b"\n" + _CA.encode()
    for path in (ca, bundle):
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert not [name for name in os.listdir(directory) if ".tmp." in name]


def _spawn_harness(monkeypatch):
    spec = _spec()
    manager = ChatSandboxManager(ContainerBackend(image="img"))
    payload = _inspect_payload(spec, status="running", running=True)
    popens: list[tuple[list[str], dict]] = []

    class Process:
        stdin = stdout = stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=""),
    )
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox.subprocess.Popen",
        lambda argv, **kwargs: popens.append((list(argv), kwargs)) or Process(),
    )
    return spec, manager, popens


def _exec_env(command: list[str], client_env: dict[str, str]) -> dict[str, str]:
    """The environment ``docker exec`` hands the process: by value or by name."""
    env = {}
    for at, part in enumerate(command):
        if part == "--env":
            entry = command[at + 1]
            key, _, value = entry.partition("=")
            env[key] = value if "=" in entry else client_env[key]
    return env


def test_every_spawned_process_trusts_the_relay_and_its_own_value_wins(monkeypatch):
    spec, manager, popens = _spawn_harness(monkeypatch)
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox_exec.ensure_github_relay_trust",
        lambda _manager, _spec: trust_env(),
    )

    # A setup script's process: no placeholder, no GitHub credential at all.
    manager.spawn(spec, ["/bin/sh", "-c", "git clone https://github.com/a/b"], user_uid=30000)
    # A member whose owner set a TLS bundle of their own.
    manager.spawn(
        spec,
        ["claude"],
        env={"REQUESTS_CA_BUNDLE": "/workspace/corp.pem"},
        user_uid=MEMBER_UID_BASE,
    )

    script, member = (_exec_env(argv, kwargs.get("env") or {}) for argv, kwargs in popens)
    assert {key: script[key] for key in trust_env()} == trust_env()
    assert member["REQUESTS_CA_BUNDLE"] == "/workspace/corp.pem"
    assert member["SSL_CERT_FILE"] == CA_BUNDLE_PATH


def test_a_terminal_trusts_the_relay(monkeypatch):
    spec, manager, popens = _spawn_harness(monkeypatch)
    monkeypatch.setattr(
        "cloud_agent_host.chat_sandbox_exec.ensure_github_relay_trust",
        lambda _manager, _spec: trust_env(),
    )

    manager.spawn_terminal(spec, rows=24, cols=80)

    argv, kwargs = popens[0]
    terminal = _exec_env(argv, kwargs.get("env") or {})
    assert {key: terminal[key] for key in trust_env()} == trust_env()


def test_install_failure_message_names_the_relay():
    manager, _, _ = _fake_manager([(1, b"no space left")])
    with pytest.raises(SandboxError, match="GitHub relay's trust could not be installed"):
        install_github_relay_trust(manager, _spec(), _CA)
