"""What the host service hands each turn's child, and what the child does with it.

The child starts from the host's runtime verdict and the host's own verification
of the network rules, passed on its stdin after the claim's underscore keys are
stripped, so a claim can never supply either. The child then checks only what
is its own -- Docker's version and the relay network -- and starts no helper
container; it declines a turn whose verification is missing, malformed or
failed. One older than the window an activation trusts only means the child
was slow to start, and the child checks the rules itself.
"""

# ruff: noqa: F811 -- imported pytest fixtures remain parameters
from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from tests.unit.test_runner_host_service import (
    FakeHandle,
    FakeQualification,
    _delivered_job,
    _job,
    service,  # noqa: F401 - the pytest fixture
)

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.host_runtime import LAUNCH_STARTED, LaunchResult
from cloud_agent_host.network_guard import VERIFICATION_KEY
from cloud_agent_shared.egress import network_from_env

#: The network a turn's child is built with, in the default sandbox namespace.
_NETWORK = network_from_env(
    {
        "AGENT_SANDBOX_NAMESPACE": "default",
        "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
        "AGENT_EGRESS_DNS_SERVER": "1.1.1.1",
        "AGENT_RELAY_NETWORK": "agent-relays",
        "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
    }
)


_HELPER_IMAGE = "sha256:" + "a" * 64


def _verified_rules(*, age_s: float = 1.0) -> dict[str, Any]:
    """What the host hands its child once it verified the network rules ``age_s`` ago."""
    return {"verified_at": time.monotonic() - age_s, "helper_image": _HELPER_IMAGE}


@pytest.mark.parametrize("verdict", ["adopted", "unusable", "updating", "network_failure"])
def test_a_child_with_the_hosts_verdict_checks_only_the_network(monkeypatch, verdict):
    import io
    import os

    from cloud_agent_host.sandbox import ContainerBackend, SandboxError

    events = []
    read_fd, write_fd = os.pipe()
    backend = ContainerBackend(
        image="sandbox", runtime="io.containerd.kata.v2", sandbox_network=_NETWORK
    )
    handed = {"guest_kernel": "guest-kernel", "epoch": {"generation": "g"}}
    # Beside the verdict, every turn now carries the host's verification of the
    # network rules; without one the child declines before adopting anything.
    payload = {
        **_delivered_job(),
        runner_mod.VERDICT_KEY: handed,
        VERIFICATION_KEY: _verified_rules(),
    }
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    monkeypatch.setattr(backend, "preflight", lambda: events.append("full"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    def adopt(actual, received):
        assert actual is backend
        assert received == handed
        events.append("adopt")
        if verdict == "updating":
            raise SandboxError("Kata runtime is being updated; refusing to accept a turn")
        return verdict != "unusable"

    def network():
        events.append("network")
        if verdict == "network_failure":
            raise SandboxError("network changed")

    def run(_job, **_kwargs):
        events.append("run")
        assert os.read(read_fd, 256).decode().startswith("accepted")
        return 0

    monkeypatch.setattr(runner_mod, "adopt_verdict", adopt)
    monkeypatch.setattr(backend, "check_network", network)
    monkeypatch.setattr(runner_mod, "run_turn", run)
    try:
        code = runner_mod._serve_turn(
            handover_fd=write_fd,
            base_url="http://control",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
        )
        if verdict in {"updating", "network_failure"}:
            assert code == 2
            assert os.read(read_fd, 256).decode().startswith("declined")
        else:
            assert code == 0
        assert (
            events
            == {
                "adopted": ["adopt", "network", "run"],
                "unusable": ["adopt", "full", "run"],
                "updating": ["adopt"],
                "network_failure": ["adopt", "network"],
            }[verdict]
        )
    finally:
        os.close(read_fd)


def _child_docker(monkeypatch, kernel=None) -> list[list[str]]:
    """Answer a turn child's Docker CLI as a healthy host would, recording each process.

    A helper container, or the inspect that finds the helper's image, fails
    unless ``kernel`` answers them: a child on a recent verification must not
    need either.
    """
    from types import SimpleNamespace

    from cloud_agent_host import sandbox as sandbox_mod

    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[1:3] == ["network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if argv[1] == "version":
            versions = "28.4.0 1.51" if "{{.Client.Version}}" in argv[-1] else "28.4.0"
            return SimpleNamespace(returncode=0, stdout=versions + "\n", stderr="")
        if kernel is not None:
            return kernel(list(argv), kwargs.get("input"))
        return SimpleNamespace(returncode=1, stdout="", stderr="not in this test")

    monkeypatch.setattr(sandbox_mod.subprocess, "run", run)
    return calls


def _serve_child(monkeypatch, backend, payload: dict[str, Any]) -> tuple[int, str]:
    """Run one turn's child on ``payload``; its exit code and its handover answer."""
    import io
    import os

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    try:
        code = runner_mod._serve_turn(
            handover_fd=write_fd,
            base_url="http://control",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
        )
        return code, os.read(read_fd, 4096).decode()
    finally:
        os.close(read_fd)


@pytest.mark.parametrize(
    ("rules", "reason"),
    [
        pytest.param(None, "without verifying", id="missing"),
        pytest.param(
            {"verified_at": "recently", "helper_image": _HELPER_IMAGE}, "unreadable", id="malformed"
        ),
        pytest.param(
            {"verified_at": float("inf"), "helper_image": _HELPER_IMAGE},
            "unreadable",
            id="not-finite",
        ),
        pytest.param(
            {"verified_at": 1e12, "helper_image": _HELPER_IMAGE}, "unreadable", id="future"
        ),
        # However old, one the child cannot read is refused, never checked around.
        pytest.param({"verified_at": 1.0, "helper_image": "runner:1"}, "unreadable", id="tag"),
        pytest.param(
            {"error": "nft failed: Error: Could not process rule"},
            "could not verify the sandbox network rules: nft failed",
            id="failing",
        ),
    ],
)
def test_a_child_declines_a_turn_without_a_sound_verification_of_the_rules(
    monkeypatch, rules, reason
):
    """Before it accepts, and without a helper container of its own.

    Only a child that declines hands its turn back unspent. One that accepted
    and then found the rules unverified would strand the turn behind its lease.
    """
    from cloud_agent_host.sandbox import ContainerBackend

    backend = ContainerBackend(image="sandbox", sandbox_network=_NETWORK)
    calls = _child_docker(monkeypatch)
    payload = _delivered_job()
    if rules is not None:
        payload[VERIFICATION_KEY] = rules
    monkeypatch.setattr(runner_mod, "run_turn", lambda *_a, **_k: pytest.fail("declined turn ran"))

    code, answer = _serve_child(monkeypatch, backend, payload)

    assert code == 2
    assert answer.startswith("declined ")
    assert reason in answer
    assert calls == [], "declined before running any Docker process"


@pytest.mark.parametrize("check", ["passes", "fails"])
def test_a_child_slow_to_start_checks_the_rules_itself_rather_than_decline(
    monkeypatch, caplog, check
):
    """A sound verification that aged out before the child took it (#1006).

    Nothing is wrong with the rules, so the child neither declines on it nor
    trusts it. It makes the check every child made before the host handed its
    own over: the inspect that finds the helpers' image, then the install and
    read-back and the route read, five Docker processes in all. The turn is
    accepted only if that check passes.
    """
    from tests.unit.test_network_guard import FakeNft

    from cloud_agent_host.network_guard import GUARD_RECHECK_S
    from cloud_agent_host.sandbox import ContainerBackend

    backend = ContainerBackend(image="sandbox", sandbox_network=_NETWORK)
    kernel = FakeNft()
    if check == "fails":
        kernel.routes.append({"dst": "10.240.0.0/16", "dev": "vpn0"})
    monkeypatch.setenv("HOSTNAME", "runner-1")
    calls = _child_docker(monkeypatch, kernel)
    payload = {**_delivered_job(), VERIFICATION_KEY: _verified_rules(age_s=GUARD_RECHECK_S + 1)}
    ran: list[str] = []

    def run(job, **_kwargs):
        ran.append(job.job_id)
        return 0

    monkeypatch.setattr(runner_mod, "run_turn", run)

    with caplog.at_level(logging.WARNING):
        code, answer = _serve_child(monkeypatch, backend, payload)

    assert [record.getMessage() for record in caplog.records].count(
        "network_guard_verification_aged_out"
    ) == 1
    assert calls[:3] == [
        ["docker", "version", "--format", "{{.Client.Version}} {{.Server.APIVersion}}"],
        ["docker", "network", "inspect", "agent-relays", "--format", "{{.Internal}}"],
        ["docker", "container", "inspect", "--format", "{{.Image}}", "runner-1"],
    ]
    assert [argv[argv.index("--entrypoint") + 1] for argv in calls[3:]] == ["python3", "ip"]
    if check == "passes":
        assert (code, ran) == (0, ["ajob_1"])
        assert answer.startswith("accepted ajob_1")
    else:
        assert (code, ran) == (2, [])
        assert answer.startswith("declined ")
        assert "overlaps the host route 10.240.0.0/16" in answer


@pytest.mark.parametrize("runtime", ["runc", "kata"])
def test_a_child_with_a_recent_verification_accepts_without_a_helper_container(
    monkeypatch, runtime
):
    """The Docker version and the relay network are still the child's to check.

    The rules' install and read-back, two helper containers, and the inspect
    that finds the helpers' image are its host's: a runc child, like a Kata
    child holding the host's runtime verdict, runs two Docker processes
    before it accepts, where they used to run six and five. A runc child's
    preflight asks Docker once both whether it answers and its versions.
    """
    from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend

    backend = ContainerBackend(
        image="sandbox",
        runtime=KATA_RUNTIME if runtime == "kata" else None,
        sandbox_network=_NETWORK,
    )
    calls = _child_docker(monkeypatch)
    payload = {**_delivered_job(), VERIFICATION_KEY: _verified_rules()}
    if runtime == "kata":
        payload[runner_mod.VERDICT_KEY] = {"guest_kernel": "guest", "epoch": {"generation": "g"}}
        monkeypatch.setattr(runner_mod, "adopt_verdict", lambda _backend, _verdict: True)
    ran: list[Any] = []

    def run(job, **kwargs):
        ran.append((job.job_id, kwargs["backend"]))
        return 0

    monkeypatch.setattr(runner_mod, "run_turn", run)

    code, answer = _serve_child(monkeypatch, backend, payload)

    assert code == 0
    assert answer.startswith("accepted ajob_1")
    assert ran == [("ajob_1", backend)]
    version = ["docker", "version", "--format", "{{.Client.Version}} {{.Server.APIVersion}}"]
    relays = ["docker", "network", "inspect", "agent-relays", "--format", "{{.Internal}}"]
    assert calls == [version, relays]


def test_only_this_host_can_hand_a_child_a_runtime_verdict(monkeypatch):
    """A claim's own underscore keys never reach the child; the host's verdict does."""
    import io
    import os
    from types import SimpleNamespace

    delivered: list[dict[str, Any]] = []

    class Stdin(io.BytesIO):
        def close(self) -> None:
            delivered.append(json.loads(self.getvalue()))
            super().close()

    class Qualification:
        def __init__(self, verdict):
            self._verdict = verdict

        def verdict(self):
            return self._verdict

        def handover(self):
            # As HostQualification.handover makes it on a host without network rules.
            return {} if self._verdict is None else {runner_mod.VERDICT_KEY: self._verdict}

    def popen(argv, **_kwargs):
        assert "ajt.1" not in argv and "grant" not in argv
        return SimpleNamespace(stdin=Stdin(), wait=lambda timeout=None: 0, kill=lambda: None)

    def read(fd):
        os.close(fd)
        return "declined for the test"

    monkeypatch.setattr(runner_mod.subprocess, "Popen", popen)
    monkeypatch.setattr(runner_mod, "read_handover", read)
    forged = {**_delivered_job(), runner_mod.VERDICT_KEY: {"guest_kernel": "forged"}}
    for qualification in (None, Qualification(None), Qualification({"guest_kernel": "proved"})):
        runner_mod._spawn_turn(
            forged,
            base_url="http://control",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
            qualification=qualification,
        )
    assert [payload.get(runner_mod.VERDICT_KEY) for payload in delivered] == [
        None,
        None,
        {"guest_kernel": "proved"},
    ]
    assert all(payload["worker_token"] == "ajt.1" for payload in delivered)


def test_only_this_host_can_hand_a_child_its_verification_of_the_rules(monkeypatch):
    """A claim's own ``_network`` never reaches the child; this host's guard's does."""
    import io
    import os
    from types import SimpleNamespace

    delivered: list[dict[str, Any]] = []

    class Stdin(io.BytesIO):
        def close(self) -> None:
            delivered.append(json.loads(self.getvalue()))
            super().close()

    class Qualification:
        """The host's own hand-off, as ``HostQualification.handover`` makes it."""

        def handover(self):
            return {VERIFICATION_KEY: {"verified_at": 1.0, "helper_image": "sha256:proved"}}

    def read(fd):
        os.close(fd)
        return "declined for the test"

    monkeypatch.setattr(
        runner_mod.subprocess,
        "Popen",
        lambda argv, **_k: SimpleNamespace(stdin=Stdin(), wait=lambda timeout=None: 0),
    )
    monkeypatch.setattr(runner_mod, "read_handover", read)
    forged = {
        **_delivered_job(),
        VERIFICATION_KEY: {"verified_at": 2.0, "helper_image": "sha256:forged"},
    }
    for qualification in (None, Qualification()):
        runner_mod._spawn_turn(
            forged,
            base_url="http://control",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
            qualification=qualification,
        )
    assert [payload.get(VERIFICATION_KEY) for payload in delivered] == [
        None,
        {"verified_at": 1.0, "helper_image": "sha256:proved"},
    ]


def test_the_host_hands_each_turn_the_guard_its_startup_check_verified(service, monkeypatch):
    """One guard for the life of the service, so its verification is renewed, not remade."""
    from cloud_agent_host.sandbox import ContainerBackend

    events: list[str] = []
    backend = ContainerBackend(image="sandbox", sandbox_network=_NETWORK)
    monkeypatch.setattr(backend, "preflight", lambda: events.append("full"))
    monkeypatch.setattr(backend, "version_stamp", lambda: {})
    monkeypatch.setattr(
        runner_mod, "HostQualification", lambda actual: FakeQualification(actual, [False], events)
    )

    def claim(**_kwargs):
        runner_mod._STOPPING.set()
        return _job(1)

    def spawn(_payload, **kwargs):
        # The startup-checked backend, whose one guard renews its verification.
        assert kwargs["qualification"].backend is backend
        events.append("launch")
        handle = FakeHandle()
        handle.exit_code = 0
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    monkeypatch.setattr(runner_mod, "_spawn_turn", spawn)

    runner_mod.run_forever(
        base_url="http://control", worker_id="runner-a", backend=backend, host="host-a"
    )

    assert events == ["refresh", "full", "refresh", "launch"]
