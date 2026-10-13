"""The host service: it claims up to its limit, and hands each turn to a child.

The loop itself is small; what it has to get right is not. A limit that is
compared stale runs more turns than an operator allowed. A limit that is
applied by stopping something interrupts a user mid-answer. A turn handed back
after it started runs someone's instruction twice, and one not handed back when
it provably never started leaves them waiting out a lease for nothing. Each of
those is a test here, including the reversals an operator produces by typing
a number, changing their mind, and typing another.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from cloud_agent_host import runner as runner_mod
from cloud_agent_host.enrollment import HostIdentity
from cloud_agent_host.host_runtime import (
    HOST_CONCURRENCY_CAPABILITY,
    LAUNCH_NOT_STARTED,
    LAUNCH_STARTED,
    LAUNCH_UNCERTAIN,
    LaunchResult,
)
from cloud_agent_host.sandbox import ProcessBackend
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy
from cloud_agent_shared.shared_protocol import HOST_SETTINGS_CAPABILITY

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeHandle:
    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        # Service tests need a finite turn after the parent begins its drain,
        # just as real one-turn children eventually exit on their own. This is
        # deliberately not implemented through terminate(): graceful
        # deployment must not signal a child whose ownership is ambiguous.
        if self.exit_code is None and runner_mod._STOPPING.is_set():
            self.exit_code = 0
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self.exit_code = -9


class FakeHeartbeat:
    """What the loop reads off ``HostHeartbeat``: status, removal, credential."""

    last: FakeHeartbeat | None = None
    #: Whether ``start`` delivers a first heartbeat answer, as a reachable
    #: control plane does. Turned off to stand for one whose heartbeat fails.
    answers_on_start = True

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.identity: HostIdentity = kwargs["identity"]
        self.removed = __import__("threading").Event()
        self.started = False
        self.stopped = False
        self.wakes = 0
        FakeHeartbeat.last = self

    @property
    def status(self) -> str:
        return self.identity.status

    @property
    def claimable(self) -> bool:
        return self.identity.status == "online"

    @property
    def credential(self) -> str:
        return self.identity.credential

    def start(self) -> None:
        self.started = True
        # A real host beats at once, and that first answer carries the runtime
        # settings and archive policy; a fake that never answered holds every
        # claim back because the archive boundary has no environment fallback.
        if FakeHeartbeat.answers_on_start:
            if self.kwargs.get("on_settings") is not None:
                self.kwargs["on_settings"](dict(self.identity.settings))
            if self.kwargs.get("on_sandbox_archive_settings") is not None:
                settings = self.identity.sandbox_archive_settings
                self.kwargs["on_sandbox_archive_settings"](
                    None if settings is None else dict(settings)
                )
            # These callbacks model the first answer itself. A wake raised
            # while applying that same answer cannot make it arrive sooner;
            # keep later assertions scoped to post-start state changes.
            self.wakes = 0

    def stop(self) -> None:
        self.stopped = True

    def wake(self) -> None:
        self.wakes += 1


def _no_wait_endpoint(**_kwargs: Any) -> str:
    raise runner_mod.WorkWaitUnsupported("404")


class _SettingsSandboxManager:
    """Acknowledge the two DB-owned settings contracts used by this suite."""

    def apply_host_settings(self, settings: dict[str, int]) -> dict[str, object]:
        return {"applied": dict(settings), "local": {}}

    def apply_sandbox_archive_settings(self, settings: dict[str, Any]) -> dict[str, int]:
        return {"applied_revision": int(settings["revision"])}

    def close(self) -> None:
        return None


@pytest.fixture
def service(monkeypatch):
    """``run_forever`` with its enrollment, heartbeat and attestation stubbed."""
    runner_mod._STOPPING.clear()
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "sha")
    monkeypatch.setenv("AGENT_HOST_SLOTS", "3")
    identity = HostIdentity(
        host="host-a",
        credential="host-cred",
        status="online",
        sandbox_archive_settings=SandboxArchivePolicy().as_dict(),
    )
    monkeypatch.setattr(runner_mod, "ensure_enrolled", lambda **_kw: identity)
    monkeypatch.setattr(runner_mod, "HostHeartbeat", FakeHeartbeat)
    monkeypatch.setattr(runner_mod, "_session_supervisor_from_env", _SettingsSandboxManager)
    monkeypatch.setattr(runner_mod, "_start_host_capacity_collector", lambda _b: None)
    monkeypatch.setattr(
        runner_mod,
        "_shared_live_host_capabilities",
        lambda _b, _s: [runner_mod.SHARED_SANDBOX_CAPABILITY],
    )
    monkeypatch.setattr(runner_mod, "_shared_live_deployment_ready", lambda _b, _s: True)
    # The service is a loop that idles rather than exiting, which is what a
    # host does. A test drives it by scripting the queue and lets this stop it
    # once it has gone quiet, so a full host — which idles by design — ends the
    # test instead of spinning in it.
    idle = {"n": 0}

    def sleep(_seconds: float) -> None:
        idle["n"] += 1
        if idle["n"] >= 20:
            runner_mod._STOPPING.set()

    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    # A control plane without the wait endpoint, so an idle host sleeps its
    # interval as these tests expect. The waiting itself is tested below.
    monkeypatch.setattr(runner_mod, "wait_for_work", _no_wait_endpoint)
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kw: None)

    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner_mod,
        "release_turn",
        lambda *, base_url, worker_token, job_id, reason: released.append((job_id, reason)) or True,
    )
    yield identity, backend, released
    runner_mod._STOPPING.clear()


def _serve(backend, *, launch: Callable[[dict], LaunchResult]) -> int:
    """Run the host service against a scripted queue and scripted launches."""
    return runner_mod.run_forever(
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        backend=backend,
        host="host-a",
        spawn_turn=launch,
        tick_s=0.0,
    )


def _queue(jobs: list[dict], *, stop_after: int | None = None) -> Callable[..., dict | None]:
    """A claim endpoint that hands out ``jobs`` and then stops the service."""
    handed = {"n": 0}

    def claim(**_kwargs: Any) -> dict | None:
        if handed["n"] >= len(jobs):
            runner_mod._STOPPING.set()
            return None
        job = jobs[handed["n"]]
        handed["n"] += 1
        if stop_after is not None and handed["n"] >= stop_after:
            runner_mod._STOPPING.set()
        return job

    return claim


def _job(n: int) -> dict:
    return {"job_id": f"ajob_{n}", "attempt_id": n, "worker_token": f"ajt.{n}"}


def _delivered_job() -> dict[str, Any]:
    return {
        **_job(1),
        "thread_id": "athr_1",
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "sandbox_token": "grant",
    }


@pytest.mark.parametrize("verdict", ["adopted", "unusable", "updating", "network_failure"])
def test_a_child_with_the_hosts_verdict_checks_only_the_network(monkeypatch, verdict):
    import io
    import os

    from cloud_agent_host.sandbox import ContainerBackend, SandboxError

    events = []
    read_fd, write_fd = os.pipe()
    backend = ContainerBackend(image="sandbox", runtime="io.containerd.kata.v2")
    handed = {"guest_kernel": "guest-kernel", "epoch": {"generation": "g"}}
    payload = {**_delivered_job(), runner_mod.VERDICT_KEY: handed}
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
            gateway_url="http://gateway",
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


@pytest.mark.parametrize("raw", ["", "{", "[]"])
def test_the_deployment_canary_always_runs_the_full_probe(monkeypatch, raw):
    import io
    import os

    from cloud_agent_host.sandbox import ContainerBackend

    read_fd, write_fd = os.pipe()
    backend = ContainerBackend(image="sandbox", runtime="io.containerd.kata.v2")
    events = []
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    monkeypatch.setattr(backend, "preflight", lambda: events.append("full"))
    monkeypatch.setattr(backend, "check_network", lambda: pytest.fail("canary skipped the probe"))
    try:
        assert (
            runner_mod._serve_turn(
                handover_fd=write_fd,
                base_url="http://control",
                gateway_url="http://gateway",
                worker_id="runner-a",
                lease_ttl=120.0,
                agent_timeout_s=3600.0,
                host="host-a",
            )
            == 0
        )
        assert events == ["full"]
        assert os.read(read_fd, 256).decode().startswith("declined")
    finally:
        os.close(read_fd)


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
            gateway_url="http://gateway",
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


def test_a_handover_read_error_keeps_the_spawned_child_tracked(monkeypatch):
    """Once spawned, an ambiguous child must remain in the graceful-drain set."""
    import errno
    import io
    from types import SimpleNamespace

    from cloud_agent_host import host_runtime as host_runtime_mod

    process = SimpleNamespace(stdin=io.BytesIO(), killed=False)

    def kill() -> None:
        process.killed = True

    process.kill = kill
    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def broken_select(*_args, **_kwargs):
        raise OSError(errno.EIO, "handover pipe failed")

    monkeypatch.setattr(host_runtime_mod.select, "select", broken_select)

    result = runner_mod._spawn_turn(
        _delivered_job(),
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert result.outcome == LAUNCH_UNCERTAIN
    assert result.handle is process
    assert "handover failed (OSError)" in result.detail
    assert not process.killed


def test_a_handover_pipe_failure_is_not_started(monkeypatch):
    """Failure before a process exists may safely hand the claim back."""
    import errno

    def broken_pipe():
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(runner_mod.os, "pipe", broken_pipe)
    result = runner_mod._spawn_turn(
        _delivered_job(),
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert result.outcome == LAUNCH_NOT_STARTED
    assert result.handle is None
    assert "handover pipe" in result.detail


def test_handover_eof_keeps_the_spawned_child_tracked(monkeypatch):
    """A failed accepted write and a pre-answer exit both look like EOF."""
    import io
    import os
    from types import SimpleNamespace

    process = SimpleNamespace(stdin=io.BytesIO(), killed=False, waited=False)

    def kill() -> None:
        process.killed = True

    def wait(*, timeout: float) -> int:
        process.waited = True
        return 0

    process.kill = kill
    process.wait = wait
    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def eof(fd: int) -> str:
        os.close(fd)
        return ""

    monkeypatch.setattr(runner_mod, "read_handover", eof)

    result = runner_mod._spawn_turn(
        _delivered_job(),
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert result.outcome == LAUNCH_UNCERTAIN
    assert result.handle is process
    assert not process.killed
    assert not process.waited


def test_ctrl_c_during_handover_kills_and_reaps_the_untracked_child(monkeypatch):
    """Immediate shutdown owns a child that TurnPool has not received yet."""
    import io
    import os
    from types import SimpleNamespace

    process = SimpleNamespace(stdin=io.BytesIO(), killed=False, waited=False)

    def kill() -> None:
        process.killed = True

    def wait(*, timeout: float) -> int:
        assert timeout == 10
        process.waited = True
        return -9

    process.kill = kill
    process.wait = wait
    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def interrupted_read(fd: int) -> str:
        os.close(fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_mod, "read_handover", interrupted_read)

    with pytest.raises(KeyboardInterrupt):
        runner_mod._spawn_turn(
            _delivered_job(),
            base_url="http://control",
            gateway_url="http://gateway",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
        )

    assert process.killed
    assert process.waited


class FakeQualification:
    """What ``run_forever`` uses of ``HostQualification``: one scripted answer per refresh."""

    def __init__(self, backend: Any, answers: list[Any], events: list[str]) -> None:
        self.backend = backend
        self.answers = list(answers)
        self.events = events
        self.calls = 0

    def refresh(self) -> bool:
        self.calls += 1
        self.events.append("refresh")
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, Exception):
            raise answer
        return bool(answer)

    def verdict(self) -> None:
        return None


def _qualified_backend(monkeypatch, events: list[str]):
    from cloud_agent_host.sandbox import ContainerBackend

    backend = ContainerBackend(image="sandbox")
    monkeypatch.setattr(backend, "preflight", lambda: events.append("full"))
    monkeypatch.setattr(backend, "check_network", lambda: events.append("network"))
    monkeypatch.setattr(backend, "version_stamp", lambda: {})
    monkeypatch.setattr(backend, "check_gateway_reachable", lambda _url: None)
    return backend


@pytest.mark.parametrize("qualified", [True, False])
def test_the_host_proves_its_runtime_once_at_start(service, monkeypatch, qualified):
    """The probe at start becomes the verdict its children inherit.

    Proving the runtime and then running the full preflight as well would boot
    a second probe guest on every restart, before the first claim.
    """
    events: list[str] = []
    backend = _qualified_backend(monkeypatch, events)
    monkeypatch.setattr(
        runner_mod,
        "HostQualification",
        lambda actual: FakeQualification(actual, [qualified], events),
    )
    monkeypatch.setattr(runner_mod, "claim_payload", lambda **_k: runner_mod._STOPPING.set())
    assert (
        runner_mod.run_forever(
            base_url="http://control", worker_id="runner-a", backend=backend, host="host-a"
        )
        == 0
    )
    assert events[:2] == (["refresh", "network"] if qualified else ["refresh", "full"])


def test_runtime_qualification_failure_pauses_claims_until_recovered(service, monkeypatch):
    from cloud_agent_host.sandbox import SandboxError

    events: list[str] = []
    backend = _qualified_backend(monkeypatch, events)
    qualification = FakeQualification(
        backend, [True, SandboxError("runtime updating"), True], events
    )
    monkeypatch.setattr(runner_mod, "HostQualification", lambda _backend: qualification)

    def sleep(seconds):
        assert seconds == 5
        assert "runtime updating" in FakeHeartbeat.last.kwargs["launch_error"]()
        events.append("backoff")

    def claim(**_kwargs):
        events.append("claim")
        assert FakeHeartbeat.last.kwargs["launch_error"]() == ""
        runner_mod._STOPPING.set()
        return _job(1)

    def spawn(payload, **kwargs):
        assert kwargs["qualification"] is qualification
        events.append("launch")
        handle = FakeHandle()
        handle.exit_code = 0
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    monkeypatch.setattr(runner_mod, "_spawn_turn", spawn)
    assert (
        runner_mod.run_forever(
            base_url="http://control", worker_id="runner-a", backend=backend, host="host-a"
        )
        == 0
    )
    assert events == ["refresh", "network", "refresh", "backoff", "refresh", "claim", "launch"]


def test_a_persisting_qualification_failure_is_reported_once(service, monkeypatch):
    """The retry backs off inside the qualification; the loop must not add noise.

    A traceback and a forced heartbeat on every poll buried the first report
    under copies of itself, for as long as the runtime stayed broken.
    """
    from cloud_agent_host.sandbox import SandboxError

    events: list[str] = []
    backend = _qualified_backend(monkeypatch, events)
    failure = SandboxError("observer self-test failed")
    monkeypatch.setattr(
        runner_mod,
        "HostQualification",
        lambda actual: FakeQualification(actual, [True, failure, failure, failure, True], events),
    )
    warnings: list[str] = []
    monkeypatch.setattr(runner_mod.logger, "warning", lambda event, *a, **k: warnings.append(event))
    monkeypatch.setattr(runner_mod, "claim_payload", lambda **_k: runner_mod._STOPPING.set())
    assert (
        runner_mod.run_forever(
            base_url="http://control", worker_id="runner-a", backend=backend, host="host-a"
        )
        == 0
    )
    assert warnings.count("agent_runtime_qualification_failed") == 1
    assert FakeHeartbeat.last.wakes == 2, "one beat reports the failure and one clears it"


@pytest.mark.parametrize("full", [False, True])
def test_freed_slots_are_detected_without_faster_empty_queue_polling(service, monkeypatch, full):
    _identity, backend, _released = service
    waits = []
    launched = []

    def sleep(seconds):
        waits.append(seconds)
        runner_mod._STOPPING.set()

    def launch(_payload):
        launched.append(FakeHandle())
        return LaunchResult(LAUNCH_STARTED, launched[-1])

    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    monkeypatch.setattr(
        runner_mod, "claim_payload", lambda **_k: _job(len(launched)) if full else None
    )
    assert (
        runner_mod.run_forever(
            base_url="http://control",
            worker_id="runner-a",
            backend=backend,
            host="host-a",
            spawn_turn=launch,
        )
        == 0
    )
    # Shutdown may sleep while draining live handles; the service's first
    # sleep is the relevant interval and never triggers an extra API claim.
    assert waits[0] == (1.0 if full else 5.0)
    assert len(launched) == (3 if full else 0)


@pytest.mark.parametrize("reusable", [False, True])
def test_repaired_image_is_refreshed_but_claims_always_require_current_attestation(
    service, monkeypatch, reusable
):
    events: list[str] = []
    backend = _qualified_backend(monkeypatch, events)
    qualification = FakeQualification(backend, [reusable] * 3, events)
    monkeypatch.setattr(runner_mod, "HostQualification", lambda _backend: qualification)
    monkeypatch.setattr(backend, "version_stamp", lambda: {"compatible": qualification.calls >= 3})

    def ready(_backend, stamp):
        events.append("gate")
        return stamp["compatible"]

    def sleep(_seconds):
        events.append("wait")
        assert events.count("wait") <= 1, "stale attestation must not prevent checking a repair"
        assert "claim" not in events, "an incompatible image must never claim"

    def claim(**_kwargs):
        assert qualification.calls == 3
        events.append("claim")
        runner_mod._STOPPING.set()
        return _job(1)

    def spawn(_payload, **_kwargs):
        handle = FakeHandle()
        handle.exit_code = 0
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod, "_shared_live_deployment_ready", ready)
    monkeypatch.setattr(runner_mod.time, "sleep", sleep)
    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    monkeypatch.setattr(runner_mod, "_spawn_turn", spawn)
    assert (
        runner_mod.run_forever(
            base_url="http://control", worker_id="runner-a", backend=backend, host="host-a"
        )
        == 0
    )
    # The first two entries are the check at start, by probe or by preflight.
    assert events[2:] == ["refresh", "gate", "wait", "refresh", "gate", "claim"]


# ── The limit ──────────────────────────────────────────────────────────


def test_the_service_claims_up_to_its_default_limit_and_no_further(service, monkeypatch):
    identity, backend, _released = service
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(n) for n in range(10)]))
    live: list[FakeHandle] = []
    peak = {"n": 0}

    def launch(_job_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        peak["n"] = max(peak["n"], sum(1 for h in live if h.exit_code is None))
        return LaunchResult(LAUNCH_STARTED, handle)

    assert _serve(backend, launch=launch) == 0

    # Three claimed, and then nothing: the fourth would have exceeded the limit
    # and the loop stops asking rather than claiming and queuing locally.
    assert len(live) == 3
    assert peak["n"] == 3
    assert identity.slots_desired is None


def test_a_saved_limit_is_taken_up_and_reported_back(service, monkeypatch):
    identity, backend, _released = service
    identity.slots_desired = 5
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(n) for n in range(10)]))
    live: list[FakeHandle] = []

    def launch(_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        return LaunchResult(LAUNCH_STARTED, handle)

    _serve(backend, launch=launch)

    assert len(live) == 5
    beat = FakeHeartbeat.last
    assert beat is not None
    # Reported as the limit this host is serving, read fresh per beat, and
    # advertised beside shared-live so the page can tell it will apply one.
    assert beat.kwargs["slots_total"]() == 5
    assert HOST_CONCURRENCY_CAPABILITY in beat.kwargs["capabilities"]()
    assert beat.wakes >= 1, "a limit this host adopted is told to the scheduler now"


def test_each_turn_is_handed_the_admin_settings_from_the_latest_beat(service, monkeypatch):
    """A value saved between two claims reaches the second turn and never the first."""
    _identity, backend, _released = service
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(0), _job(1)]))
    handed: list[dict] = []

    def launch(payload: dict) -> LaunchResult:
        handed.append(payload)
        if len(handed) == 1:
            # The heartbeat thread adopting an answer, between the two claims.
            FakeHeartbeat.last.kwargs["on_settings"](
                {"agent_timeout_s": 900, "terminal_max_sessions": 16}
            )
        handle = FakeHandle()
        handle.exit_code = 0
        return LaunchResult(LAUNCH_STARTED, handle)

    _serve(backend, launch=launch)

    assert handed[0]["_host_settings"]["agent_timeout_s"] == runner_mod.DEFAULT_AGENT_TIMEOUT_S
    assert handed[1]["_host_settings"]["agent_timeout_s"] == 900
    assert "terminal_max_sessions" not in handed[1]["_host_settings"], (
        "the sandbox manager's, not a turn's"
    )
    beat = FakeHeartbeat.last
    assert HOST_SETTINGS_CAPABILITY in beat.kwargs["capabilities"]()
    applied, local = beat.kwargs["settings_report"]()
    assert (applied["agent_timeout_s"], local["agent_timeout_s"]) == (
        900,
        runner_mod.DEFAULT_AGENT_TIMEOUT_S,
    )
    assert beat.wakes >= 1, "a changed report is sent now, not an interval later"


def test_a_restarted_host_waits_for_its_first_answer_before_claiming(service, monkeypatch):
    """A turn claimed before the admin's settings arrive would run without them."""
    _identity, backend, _released = service
    monkeypatch.setattr(FakeHeartbeat, "answers_on_start", False)
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(0)]))
    launched: list[dict] = []

    _serve(
        backend,
        launch=lambda payload: launched.append(payload) or LaunchResult(LAUNCH_STARTED, None),
    )

    assert launched == [], "no answer within the wait, and the wait has not run out"


def test_a_host_without_an_archive_policy_answer_never_claims(service, monkeypatch):
    """The bounded settings fallback cannot bypass the archive safety boundary."""
    _identity, backend, _released = service
    monkeypatch.setattr(FakeHeartbeat, "answers_on_start", False)
    monkeypatch.setattr(runner_mod, "FIRST_SETTINGS_WAIT_S", 0.0)
    claims: list[dict[str, Any]] = []

    def claim(**_kwargs: Any) -> dict[str, Any]:
        payload = _job(0)
        claims.append(payload)
        return payload

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    launched: list[dict] = []

    def launch(payload: dict) -> LaunchResult:
        launched.append(payload)
        handle = FakeHandle()
        handle.exit_code = 0
        return LaunchResult(LAUNCH_STARTED, handle)

    _serve(backend, launch=launch)

    assert claims == []
    assert launched == []


def test_a_lowered_limit_stops_claiming_without_touching_a_running_turn(service, monkeypatch):
    """The shrink-while-full case: nothing is interrupted, nothing more is taken."""
    identity, backend, _released = service
    identity.slots_desired = 4
    live: list[FakeHandle] = []

    def claim(**_kwargs: Any) -> dict | None:
        if len(live) >= 4:
            # Full at four; the operator now types two.
            identity.slots_desired = 2
            runner_mod._STOPPING.set()
            return None
        return _job(len(live))

    def launch(_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    _serve(backend, launch=launch)

    assert len(live) == 4, "the four already running are still four"
    assert all(handle.exit_code == 0 for handle in live), "they finished; none was killed"
    assert not any(handle.killed for handle in live)


def test_reversing_the_limit_repeatedly_converges_on_the_last_one(service, monkeypatch):
    """3 → 10 → 3 → 8, with the loop never claiming past whichever is current."""
    identity, backend, _released = service
    script = [10, 3, 8]
    live: list[FakeHandle] = []
    breaches: list[tuple[int, int]] = []

    def claim(**_kwargs: Any) -> dict | None:
        if script:
            identity.slots_desired = script.pop(0)
            return None
        if len(live) >= 8:
            runner_mod._STOPPING.set()
            return None
        return _job(len(live))

    def launch(_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        current = identity.slots_desired or 3
        if len(live) > current:
            breaches.append((len(live), current))
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    _serve(backend, launch=launch)

    assert breaches == [], f"claimed past the limit in force: {breaches}"
    assert len(live) == 8, "and it settles on the last number, not an intermediate one"
    beat = FakeHeartbeat.last
    assert beat is not None and beat.kwargs["slots_total"]() == 8


def test_clearing_the_limit_returns_the_host_to_its_default_without_a_restart(service, monkeypatch):
    """*Host default* on the page has to reach the machine, not just the row.

    Regression: the loop only adopted a number it could parse, so clearing one
    left the host serving whatever it had last been given until the container
    was replaced. An operator who set 8, saw it applied, and then pressed the
    button that says the host runs its own number would have gone on running 8.
    """
    identity, backend, _released = service
    identity.slots_desired = 8
    live: list[FakeHandle] = []
    cleared = {"done": False}

    def claim(**_kwargs: Any) -> dict | None:
        if len(live) >= 4 and not cleared["done"]:
            # Four running under the saved 8; the operator now clears it.
            identity.slots_desired = None
            cleared["done"] = True
            return None
        return _job(len(live))

    def launch(_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    _serve(backend, launch=launch)

    # Back on its own default of three, which it is already over, so it takes
    # nothing more and interrupts nothing.
    assert len(live) == 4
    beat = FakeHeartbeat.last
    assert beat is not None and beat.kwargs["slots_total"]() == 3


def test_an_unreachable_control_plane_does_not_move_the_limit(service, monkeypatch):
    """Holding the last number across an outage needs no special case here.

    A failed beat raises before it reads the answer, so ``slots_desired`` keeps
    the value it last carried; the loop can therefore follow it exactly,
    including when it is cleared.
    """
    identity, backend, _released = service
    identity.slots_desired = 5
    live: list[FakeHandle] = []
    beats = {"n": 0}

    def claim(**_kwargs: Any) -> dict | None:
        beats["n"] += 1
        if beats["n"] > 2:
            # The control plane is gone. Nothing writes slots_desired now.
            raise runner_mod.ClaimUnreachable("connection refused")
        return _job(len(live))

    def launch(_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        return LaunchResult(LAUNCH_STARTED, handle)

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    _serve(backend, launch=launch)

    beat = FakeHeartbeat.last
    assert beat is not None and beat.kwargs["slots_total"]() == 5


def test_a_limit_the_host_will_not_honour_leaves_it_on_its_own_default(service, monkeypatch):
    """Out of bounds is no request at all, checked on the side that would act."""
    identity, backend, _released = service
    identity.slots_desired = 999
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(n) for n in range(10)]))
    live: list[FakeHandle] = []

    def launch(_payload: dict) -> LaunchResult:
        handle = FakeHandle()
        live.append(handle)
        return LaunchResult(LAUNCH_STARTED, handle)

    _serve(backend, launch=launch)

    assert len(live) == 3


# ── Handing a turn over ────────────────────────────────────────────────


def test_a_turn_that_never_started_goes_back_to_the_queue(service, monkeypatch):
    _identity, backend, released = service
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(1)]))

    _serve(
        backend,
        launch=lambda _p: LaunchResult(LAUNCH_NOT_STARTED, None, "could not start"),
    )

    assert released == [("ajob_1", "launch_failed")]


def test_a_turn_that_started_is_never_handed_back(service, monkeypatch):
    _identity, backend, released = service
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(1)], stop_after=1))

    _serve(backend, launch=lambda _p: LaunchResult(LAUNCH_STARTED, FakeHandle()))

    assert released == []


def test_an_uncertain_handover_is_never_handed_back(service, monkeypatch):
    """Silence is not proof. The turn stays claimed and its lease decides.

    Requeueing here would be this host overruling the retained-session rules
    with a guess, and those rules exist because replaying an admitted prompt
    executes a user's instruction a second time.
    """
    _identity, backend, released = service
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(1)], stop_after=1))

    class NaturallyFinishingHandle(FakeHandle):
        def __init__(self) -> None:
            super().__init__()
            self.polls = 0

        def poll(self) -> int | None:
            self.polls += 1
            if self.polls >= 2:
                self.exit_code = 0
            return self.exit_code

    handle = NaturallyFinishingHandle()

    _serve(
        backend,
        launch=lambda _p: LaunchResult(LAUNCH_UNCERTAIN, handle, "no answer"),
    )

    assert released == []
    assert handle.polls >= 2, "still waited for, like any other turn in flight"
    assert not handle.terminated and not handle.killed


def test_a_turn_with_no_worker_token_is_not_released(service, monkeypatch):
    """Nothing to authenticate with is not a reason to guess at the job's state."""
    _identity, backend, released = service
    monkeypatch.setattr(
        runner_mod, "claim_payload", _queue([{"job_id": "ajob_1", "attempt_id": 1}])
    )

    _serve(
        backend,
        launch=lambda _p: LaunchResult(LAUNCH_NOT_STARTED, None, "could not start"),
    )

    assert released == []


# ── Staying alive ──────────────────────────────────────────────────────


def test_a_drained_host_claims_nothing_and_keeps_heartbeating(service, monkeypatch):
    identity, backend, _released = service
    identity.status = "draining"
    claimed = {"n": 0}

    def claim(**_kwargs: Any) -> dict | None:
        claimed["n"] += 1
        return _job(claimed["n"])

    monkeypatch.setattr(runner_mod, "claim_payload", claim)

    _serve(backend, launch=lambda _p: LaunchResult(LAUNCH_STARTED, FakeHandle()))

    assert claimed["n"] == 0, "draining means take no new work"
    beat = FakeHeartbeat.last
    assert beat is not None and beat.started and beat.stopped


def test_an_unreachable_control_plane_does_not_end_the_service(service, monkeypatch):
    """The queue comes back when the control plane does; the host keeps waiting."""
    _identity, backend, _released = service
    attempts = {"n": 0}

    def claim(**_kwargs: Any) -> dict | None:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise runner_mod.ClaimUnreachable("connection refused")
        runner_mod._STOPPING.set()
        return None

    monkeypatch.setattr(runner_mod, "claim_payload", claim)

    assert _serve(backend, launch=lambda _p: LaunchResult(LAUNCH_STARTED, FakeHandle())) == 0
    assert attempts["n"] == 4, "it kept asking rather than exiting"
    beat = FakeHeartbeat.last
    assert beat is not None and beat.stopped


def test_a_rejected_credential_stops_the_service(service, monkeypatch):
    _identity, backend, _released = service
    monkeypatch.setattr(runner_mod, "claim_payload", _queue([_job(1)], stop_after=1))
    handle = FakeHandle()

    def claim(**_kwargs: Any) -> dict | None:
        beat = FakeHeartbeat.last
        assert beat is not None
        beat.removed.set()
        return None

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    assert _serve(backend, launch=lambda _p: LaunchResult(LAUNCH_STARTED, handle)) == 0


def test_a_bad_default_limit_is_a_startup_error_naming_the_variable(service, monkeypatch):
    _identity, backend, _released = service
    monkeypatch.setenv("AGENT_HOST_SLOTS", "1")

    with pytest.raises(RuntimeError, match="AGENT_HOST_SLOTS"):
        runner_mod.run_forever(
            base_url="http://control",
            worker_id="runner-a",
            backend=backend,
            host="host-a",
            spawn_turn=lambda _p: LaunchResult(LAUNCH_STARTED, FakeHandle()),
            tick_s=0.0,
        )


# ── Waiting for work ───────────────────────────────────────────────────


def test_an_idle_host_waits_for_work_instead_of_sleeping(service, monkeypatch):
    """Each wait hands back the token the last one returned, taken before the claim.

    That is what makes a message queued during the claim answer the next wait
    at once, and the credential is read per wait so a rotated one is used.
    """
    identity, backend, _released = service
    order: list[str] = []
    waits: list[dict[str, Any]] = []

    def claim(**_kwargs: Any) -> None:
        order.append("claim")
        if len(waits) == 2:
            runner_mod._STOPPING.set()

    def wait(**kwargs: Any) -> str:
        order.append("wait")
        waits.append(kwargs)
        identity.credential = f"rotated-{len(waits)}"
        return f"token-{len(waits)}"

    monkeypatch.setattr(runner_mod, "claim_payload", claim)
    monkeypatch.setattr(runner_mod, "wait_for_work", wait)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda seconds: order.append(f"sleep {seconds}"))

    assert (
        runner_mod.run_forever(
            base_url="http://control",
            worker_id="runner-a",
            backend=backend,
            host="host-a",
            spawn_turn=lambda _p: LaunchResult(LAUNCH_STARTED, FakeHandle()),
        )
        == 0
    )
    assert order == ["claim", "wait"] * 3, "the wait takes the place of the sleep"
    assert [(w["since"], w["host_credential"]) for w in waits] == [
        (None, "host-cred"),
        ("token-1", "rotated-1"),
        ("token-2", "rotated-2"),
    ]
    assert {w["timeout_s"] for w in waits} == {5.0}, "never longer than the poll it replaces"
    assert {w["base_url"] for w in waits} == {"http://control"}


def test_a_control_plane_without_the_wait_is_polled_and_asked_again_later(monkeypatch):
    clock = {"now": 1000.0}
    sleeps: list[float] = []
    asked: list[str | None] = []

    def unsupported(**kwargs: Any) -> str:
        asked.append(kwargs["since"])
        raise runner_mod.WorkWaitUnsupported("404")

    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(runner_mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(runner_mod, "wait_for_work", unsupported)
    waiter = runner_mod._WorkWaiter(base_url="http://control", interval_s=5.0)

    for _ in range(3):
        waiter.wait("cred")
        clock["now"] += 5.0
    assert asked == [None], "a rolling deploy does not cost a 404 per poll"
    assert sleeps == [5.0] * 3

    clock["now"] += runner_mod.WORK_WAIT_RETRY_S
    waiter.wait("cred")
    assert asked == [None, None], "the endpoint is tried again once the control plane may have it"


def test_a_failed_wait_sleeps_its_interval_and_starts_over(monkeypatch):
    sleeps: list[float] = []
    asked: list[str | None] = []
    answers: list[Any] = ["token-1", runner_mod.ClaimUnreachable("connection reset"), "token-2"]

    def wait(**kwargs: Any) -> str:
        asked.append(kwargs["since"])
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runner_mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(runner_mod, "wait_for_work", wait)
    waiter = runner_mod._WorkWaiter(base_url="http://control", interval_s=5.0)

    for _ in range(3):
        waiter.wait("cred")
    # The token is dropped with the failure: a control plane that restarted
    # meanwhile answers a missing one at once rather than waiting it out.
    assert asked == [None, "token-1", None]
    assert sleeps == [5.0], "only the failure falls back to sleeping"


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (200, {"token": "abc:3"}, "abc:3"),
        (404, {"detail": "Not Found"}, runner_mod.WorkWaitUnsupported),
        (401, {"detail": "unauthorized"}, httpx.HTTPStatusError),
    ],
)
def test_the_wait_request_and_how_its_answers_are_read(monkeypatch, status, body, outcome):
    sent: dict[str, Any] = {}

    def post(url: str, **kwargs: Any) -> httpx.Response:
        sent.update(url=url, **kwargs)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(runner_mod.httpx, "post", post)

    def call() -> str:
        return runner_mod.wait_for_work(
            base_url="http://control/", host_credential="aht.cred", since="abc:2", timeout_s=5.0
        )

    if isinstance(outcome, str):
        assert call() == outcome
    else:
        with pytest.raises(outcome):
            call()
    assert sent["url"] == "http://control/v1/agent/worker/wait"
    assert sent["json"] == {"since": "abc:2", "timeout_seconds": 5.0}
    assert sent["headers"] == {"Authorization": "Bearer aht.cred"}
    assert sent["timeout"] > 5.0, "the control plane answers before the client gives up"


def test_an_unreachable_control_plane_is_reported_as_one(monkeypatch):
    def post(url: str, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(runner_mod.httpx, "post", post)
    with pytest.raises(runner_mod.ClaimUnreachable):
        runner_mod.wait_for_work(
            base_url="http://control", host_credential="aht.cred", since=None, timeout_s=5.0
        )


# ── The child ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["", "{", "[]"])
def test_a_turn_process_declines_an_empty_delivery_and_runs_nothing(monkeypatch, raw):
    """What the deployment check exercises, and what a hand-started process does.

    It declines, but only after preflighting — so a rollout that runs this
    against a broken image learns that the machine could not have run a turn,
    rather than only that Python started.
    """
    import os

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(raw))
    ran: list[str] = []
    monkeypatch.setattr(runner_mod, "run_turn", lambda *a, **k: ran.append("ran") or 0)
    preflighted: list[str] = []
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kw: preflighted.append("yes"))
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)

    code = runner_mod._serve_turn(
        handover_fd=write_fd,
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert code == 0
    assert ran == []
    assert preflighted == ["yes"], "the probe must prove this machine can run a turn"
    assert os.read(read_fd, 256).decode().startswith("declined")
    os.close(read_fd)


def test_a_turn_process_accepts_before_it_runs_the_turn(monkeypatch):
    import io
    import os

    from cloud_agent_host.host_runtime import encode_turn

    read_fd, write_fd = os.pipe()
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    answered: list[str] = []
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kw: None)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)

    def run_turn(job, **_kwargs: Any) -> int:
        # By the time the turn runs, the answer is already on the pipe.
        answered.append(os.read(read_fd, 256).decode().strip())
        assert job.job_id == "ajob_1"
        return 0

    monkeypatch.setattr(runner_mod, "run_turn", run_turn)

    assert (
        runner_mod._serve_turn(
            handover_fd=write_fd,
            base_url="http://control",
            gateway_url="http://gateway",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
        )
        == 0
    )
    assert answered == ["accepted ajob_1"]
    os.close(read_fd)


@pytest.mark.parametrize("empty_delivery", [False, True])
def test_a_turn_process_declines_when_its_backend_is_not_usable(monkeypatch, empty_delivery):
    """Preflight is a read-only probe, so it belongs before the acceptance.

    Failing it afterwards would strand the turn: the host has been told the
    child took it, so it will not requeue, and the user waits out a lease for
    a turn that never ran and that spent an attempt. Failing it first costs
    nothing — the turn goes straight back to the queue.
    """
    import io
    import os

    from cloud_agent_host.host_runtime import encode_turn
    from cloud_agent_host.sandbox import SandboxError

    read_fd, write_fd = os.pipe()
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
    }
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("" if empty_delivery else encode_turn(payload).decode())
    )
    ran: list[str] = []
    monkeypatch.setattr(runner_mod, "run_turn", lambda *a, **k: ran.append("ran") or 0)

    def build() -> Any:
        backend = ProcessBackend(acknowledged_unsafe=True)
        monkeypatch.setattr(
            backend, "preflight", lambda **_kw: (_ for _ in ()).throw(SandboxError("no docker"))
        )
        return backend

    monkeypatch.setattr(runner_mod, "build_backend_from_env", build)

    # Non-zero, because the deployment check reads this exit code: an image
    # that cannot preflight must fail the deploy, not pass it quietly.
    assert (
        runner_mod._serve_turn(
            handover_fd=write_fd,
            base_url="http://control",
            gateway_url="http://gateway",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
        )
        == 2
    )
    answer = os.read(read_fd, 256).decode()
    assert answer.startswith("declined"), answer
    assert "no docker" in answer
    assert ran == [], "nothing ran, so the host may put the turn straight back"
    os.close(read_fd)


def test_a_turn_process_declines_a_payload_it_cannot_use(monkeypatch):
    """A half-written delivery is not a turn, and declining it requeues it."""
    import io
    import os

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("sys.stdin", io.StringIO('{"job_id": "ajob_1"}'))
    ran: list[str] = []
    monkeypatch.setattr(runner_mod, "run_turn", lambda *a, **k: ran.append("ran") or 0)
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(backend, "preflight", lambda **_kw: None)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)

    assert (
        runner_mod._serve_turn(
            handover_fd=write_fd,
            base_url="http://control",
            gateway_url="http://gateway",
            worker_id="runner-a",
            lease_ttl=120.0,
            agent_timeout_s=3600.0,
            host="host-a",
        )
        == 0
    )
    assert ran == []
    assert os.read(read_fd, 256).decode().startswith("declined")
    os.close(read_fd)


def test_the_cli_routes_turn_mode_and_hides_its_flags(monkeypatch):
    parser = runner_mod.build_parser()
    assert parser.parse_args([]).turn is False
    assert parser.parse_args(["--turn", "--handover-fd", "3"]).handover_fd == 3
    assert "--turn" not in parser.format_help()
    assert "--handover-fd" not in parser.format_help()

    calls: dict[str, Any] = {}
    monkeypatch.setenv("AGENT_CONTROL_PLANE_URL", "http://control")
    monkeypatch.setattr(runner_mod, "configure_process_logging", lambda: None)
    monkeypatch.setattr(runner_mod, "_serve_turn", lambda **kw: calls.setdefault("turn", kw) and 0)
    monkeypatch.setattr(
        runner_mod, "run_forever", lambda **kw: calls.setdefault("service", kw) and 0
    )

    runner_mod.main(["--turn", "--handover-fd", "9", "--host", "host-a"])
    runner_mod.main(["--host", "host-a"])

    assert calls["turn"]["handover_fd"] == 9
    assert "handover_fd" not in calls["service"]


def test_turn_mode_without_a_handover_descriptor_is_refused(monkeypatch):
    """The host service supplies both flags; one alone is a hand-built command."""
    monkeypatch.setenv("AGENT_CONTROL_PLANE_URL", "http://control")
    monkeypatch.setattr(runner_mod, "configure_process_logging", lambda: None)

    with pytest.raises(SystemExit):
        runner_mod.main(["--turn", "--host", "host-a"])


# ── The selection crossing the process boundary ────────────────────────


def _descriptor(run_id: str = "run-1") -> dict[str, Any]:
    """One experiment descriptor, as the deployment would set it."""
    from datetime import UTC, datetime, timedelta

    return {
        "run_id": run_id,
        "operations": ["attempt.startup"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        "max_operations": 10,
    }


def _run_descriptor(monkeypatch, **overrides: Any) -> dict[str, Any]:
    """Turn profiling on in this process with one usable run."""
    import json

    from cloud_agent_shared import operation_profiling

    descriptor = {**_descriptor(), **overrides}
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_RUN, json.dumps(descriptor))
    operation_profiling.reset_cache()
    return descriptor


def test_the_host_hands_its_selection_to_the_child(monkeypatch):
    """The deployed path measures startups, not only the single-process one.

    The host claims, then starts a child to run the turn -- so a selection made
    in the parent is worthless unless it crosses that boundary. Without this
    the whole feature is silent on the path production actually uses.
    """
    from cloud_agent_host.host_runtime import LAUNCH_STARTED, LaunchResult
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "base_sha": None,
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
        "execution_profile": "default",
    }
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert measurement is not None
    bound = runner_mod._bound_measurement(measurement, payload)
    assert bound is not None
    with operation_profiling.span(bound, "claim"):
        pass

    seen: list[dict[str, Any]] = []

    class _Turns:
        def launch(self, handed: dict[str, Any]) -> Any:
            seen.append(handed)
            return LaunchResult(LAUNCH_STARTED, None)

    runner_mod._hand_over_turn(
        payload,
        turns=_Turns(),
        base_url="http://control",
        worker_id="runner-a-1",
        measurement=bound,
    )

    assert seen, "the turn must be handed to a child"
    context = seen[0].get("_profiling")
    assert context is not None, "the child cannot measure what it was never told"
    assert context["run_id"] == "run-1"
    assert context["measurement_id"] == bound.measurement_id
    assert context["facts"]["job_id"] == "ajob_1"
    assert context["expires_at"] > 0
    assert context["inherited_spans"][0]["name"] == "claim"
    assert context["admission_instances"][0]["instance"] == operation_profiling.PRODUCER_INSTANCE_ID
    operation_profiling.reset_cache()


def test_the_spawn_payload_keeps_only_the_profiling_handoff():
    """The deployed child must receive the selection the parent paid for."""
    handed = runner_mod._child_turn_payload(
        {
            "job_id": "ajob_1",
            "_worker_id": "parent-only",
            "_profiling": {"run_id": "run-1", "measurement_id": "msm_1"},
        }
    )

    assert handed == {
        "job_id": "ajob_1",
        "_profiling": {"run_id": "run-1", "measurement_id": "msm_1"},
    }


@pytest.mark.parametrize("outcome", [LAUNCH_NOT_STARTED, LAUNCH_UNCERTAIN])
def test_a_parent_side_handover_failure_delivers_its_measurement(monkeypatch, outcome):
    """Failures before a child owns the sample still reach persistence."""
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
    }
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert measurement is not None
    assert runner_mod._bound_measurement(measurement, payload) is measurement
    queued: list[Any] = []
    monkeypatch.setattr(
        runner_mod,
        "_enqueue_delivery",
        lambda selected, **_kwargs: queued.append(selected),
    )

    class _Turns:
        def launch(self, _payload: dict[str, Any]) -> Any:
            return LaunchResult(outcome, None, "review failure")

    monkeypatch.setattr(runner_mod, "release_turn", lambda **_kwargs: True)
    runner_mod._hand_over_turn(
        payload,
        turns=_Turns(),
        base_url="http://control",
        worker_id="runner-a",
        measurement=measurement,
    )

    assert queued == [measurement]
    assert measurement.frozen_outcome == "incomplete"
    operation_profiling.reset_cache()


def test_an_expired_selection_is_delivered_by_the_parent_before_handoff(monkeypatch):
    """The child runs unmeasured, while the partial claim sample is retained."""
    import time

    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
    }
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION
    )
    assert measurement is not None
    assert runner_mod._bound_measurement(measurement, payload) is measurement
    measurement.deadline_mono = time.monotonic() - 1
    queued: list[Any] = []
    handed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner_mod,
        "_enqueue_delivery",
        lambda selected, **_kwargs: queued.append(selected),
    )

    class _Turns:
        def launch(self, child_payload: dict[str, Any]) -> Any:
            handed.append(child_payload)
            return LaunchResult(LAUNCH_STARTED, None)

    runner_mod._hand_over_turn(
        payload,
        turns=_Turns(),
        base_url="http://control",
        worker_id="runner-a",
        measurement=measurement,
    )

    assert "_profiling" not in handed[0]
    assert queued == [measurement]
    assert measurement.frozen_outcome == "incomplete"
    assert measurement.frozen_failure_stage == "expired_before_handoff"
    operation_profiling.reset_cache()


def test_a_turn_process_reattaches_the_selection_it_was_handed(monkeypatch):
    """The child times the turn; it does not select one or spend a slot."""
    import io
    import os
    import time

    from cloud_agent_host.host_runtime import encode_turn
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    clock = [time.monotonic_ns()]
    monkeypatch.setattr(operation_profiling.time, "monotonic_ns", lambda: clock[0])
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
        "_profiling": {
            "run_id": "run-1",
            "measurement_id": "msm_handed",
            "operation": "attempt.startup",
            "producer": "runner",
            "coverage_scope": "host_startup",
            "started_mono_ns": clock[0] - 1_000_000_000,
            "facts": {"operation": "attempt.startup", "job_id": "ajob_1"},
            "call_id": "",
            "expires_at": __import__("time").time() + 600,
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)

    def preflight(**_kwargs: Any) -> None:
        # Measuring early must not move acceptance ahead of the actual check.
        with pytest.raises(BlockingIOError):
            os.read(read_fd, 256)
        clock[0] += 5_000_000_000

    monkeypatch.setattr(backend, "preflight", preflight)

    handed: list[Any] = []

    def _capture(*_args: Any, **kwargs: Any) -> int:
        assert os.read(read_fd, 256).decode().strip() == "accepted ajob_1"
        handed.append(kwargs.get("measurement"))
        operation_profiling.mark(kwargs.get("measurement"), "validate")
        return 0

    monkeypatch.setattr(runner_mod, "run_turn", _capture)

    code = runner_mod._serve_turn(
        handover_fd=write_fd,
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert code == 0
    assert len(handed) == 1
    measurement = handed[0]
    assert measurement is not None, "the child must measure the turn it was given"
    # The parent's identity and slot, not a second one of either.
    assert measurement.measurement_id == "msm_handed"
    assert measurement.run_id == "run-1"
    assert measurement.reserved is False, "the slot belongs to the host that claimed it"
    assert measurement.facts["job_id"] == "ajob_1"
    assert measurement.coverage_scope == "host_startup"
    record = operation_profiling.to_record(measurement, outcome="dispatched")
    assert record["duration_us"] == 6_000_000, "the parent's start is still the total boundary"
    preflight_span = next(item for item in record["spans"] if item["name"] == "preflight")
    assert preflight_span["start_offset_us"] == 1_000_000
    assert preflight_span["duration_us"] == 5_000_000
    preflight_stage = next(item for item in record["stages"] if item["stage"] == "preflight")
    assert preflight_stage["duration_us"] == 5_000_000
    os.close(read_fd)
    operation_profiling.reset_cache()


@pytest.mark.parametrize("selection", [None, "invalid", "another_run", "malformed_facts"])
def test_a_child_never_refuses_a_turn_over_an_unusable_selection(monkeypatch, selection):
    """A measurement setting is not a reason to drop a user's turn."""
    import io
    import os
    import time

    from cloud_agent_host.host_runtime import encode_turn
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    read_fd, write_fd = os.pipe()
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
        "_profiling": selection,
    }
    if selection == "another_run":
        payload["_profiling"] = {"run_id": "another-run", "expires_at": 2**31}
    elif selection == "malformed_facts":
        # Otherwise valid, but attach_selection cannot read this facts shape.
        # Metadata errors must not bypass preflight or prevent the user turn.
        payload["_profiling"] = {
            "run_id": "run-1",
            "operation": "attempt.startup",
            "expires_at": time.time() + 600,
            "facts": ["malformed"],
        }
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    preflighted: list[str] = []
    monkeypatch.setattr(backend, "preflight", lambda **_kw: preflighted.append("yes"))
    handed: list[Any] = []
    monkeypatch.setattr(
        runner_mod, "run_turn", lambda *a, **k: handed.append(k.get("measurement")) or 0
    )

    code = runner_mod._serve_turn(
        handover_fd=write_fd,
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert code == 0
    assert handed == [None], "the turn runs, unmeasured"
    assert preflighted == ["yes"]
    assert os.read(read_fd, 256).decode().strip() == "accepted ajob_1"
    os.close(read_fd)
    operation_profiling.reset_cache()


@pytest.mark.parametrize("failure", ["preflight", "payload"])
def test_a_declining_child_does_not_emit_or_refund_the_parent_selection(monkeypatch, failure):
    """Only the parent decides how to report a handover that never succeeded."""
    import io
    import os

    from cloud_agent_host.host_runtime import encode_turn
    from cloud_agent_host.sandbox import SandboxError
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch, max_operations=1)
    parent = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert parent is not None
    payload = {
        "job_id": "ajob_1",
        "thread_id": "athr_1",
        "attempt_id": 7,
        "attempt_no": 1,
        "repo": "o/n",
        "task_prompt": "do it",
        "runtime": "claude-code",
        "model": "m",
        "worker_token": "ajt.a.b",
        "sandbox_token": "grant",
    }
    assert runner_mod._bound_measurement(parent, payload) is parent
    payload["_profiling"] = operation_profiling.handoff_context(parent)
    if failure == "payload":
        del payload["thread_id"]
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    inherited: list[Any] = []
    attach = operation_profiling.attach_selection

    def capture_selection(context: Any) -> Any:
        child = attach(context)
        inherited.append(child)
        return child

    monkeypatch.setattr(operation_profiling, "attach_selection", capture_selection)
    monkeypatch.setattr(
        operation_profiling,
        "finish",
        lambda *a, **k: pytest.fail("a declining child must not emit a startup"),
    )
    monkeypatch.setattr(
        runner_mod, "run_turn", lambda *a, **k: pytest.fail("a declining child must not execute")
    )
    backend = ProcessBackend(acknowledged_unsafe=True)

    def preflight(**_kwargs: Any) -> None:
        if failure == "preflight":
            raise SandboxError("unavailable")

    monkeypatch.setattr(backend, "preflight", preflight)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    read_fd, write_fd = os.pipe()
    code = runner_mod._serve_turn(
        handover_fd=write_fd,
        base_url="http://control",
        gateway_url="http://gateway",
        worker_id="runner-a",
        lease_ttl=120.0,
        agent_timeout_s=3600.0,
        host="host-a",
    )

    assert code == (2 if failure == "preflight" else 0)
    assert os.read(read_fd, 256).decode().startswith("declined")
    os.close(read_fd)
    assert len(inherited) == 1 and inherited[0] is not None
    assert inherited[0].finished and not inherited[0].selected
    assert inherited[0].reserved is False
    assert parent.reserved and not parent.finished
    assert (
        operation_profiling.start_operation(
            operation=operation_profiling.STARTUP_OPERATION, host="host-a"
        )
        is None
    ), "discarding an inherited selection must not refund the parent's only slot"
    operation_profiling.discard(parent)
    operation_profiling.reset_cache()


def test_a_poll_that_claimed_nothing_spends_no_slot(monkeypatch):
    """An idle poll is not a startup, so it must not consume the experiment."""
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch, max_operations=1)

    # The first selection is the experiment's only slot.
    first = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert first is not None
    operation_profiling.discard(first)

    # Discarded, so the slot came back and an idle poll can still be measured.
    second = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert second is not None
    operation_profiling.discard(second)
    operation_profiling.reset_cache()


# ── The exporter runtime ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_delivery_state():
    """Every test starts with no cached profiling run or delivered records."""
    from cloud_agent_shared import operation_profiling

    operation_profiling.reset_cache()
    runner_mod.reset_delivery_state()
    yield
    runner_mod.reset_delivery_state()
    operation_profiling.reset_cache()


def _sink_with(monkeypatch: Any, tmp_path: Any, records: list[dict[str, Any]]) -> str:
    """Point profiling at a sink holding these frozen records."""
    import json

    from cloud_agent_shared import operation_profiling

    sink = tmp_path / "records.jsonl"
    sink.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    monkeypatch.setenv(
        operation_profiling.ENV_PROFILING_RUN,
        json.dumps(_descriptor()),
    )
    operation_profiling.reset_cache()
    return str(sink)


def _frozen(measurement_id: str = "msm_1", **overrides: Any) -> dict[str, Any]:
    """One record as the collector freezes it."""
    from datetime import UTC, datetime

    record = {
        "schema_version": 3,
        "measurement_id": measurement_id,
        "interaction_id": measurement_id,
        "profiling_run_id": "run-1",
        "operation": "attempt.startup",
        "request_kind": "attempt.startup",
        "boundary_version": 1,
        "coverage_scope": "host_startup",
        "producer": "runner",
        "producer_instance_id": "p-1",
        "duration_us": 10,
        "outcome": "dispatched",
        "collection_complete": True,
        "end_to_end_complete": False,
        "startup_path": "new_group",
        "observed_at": datetime.now(UTC).isoformat(),
        "facts": {"operation": "attempt.startup"},
    }
    record.update(overrides)
    return record


class _StubControl:
    """A control plane that answers measurements with a scripted verdict."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def append_event(self, event: Any) -> None:
        """Record execution milestones separately from measurement delivery."""
        self.events.append((event.event_type, event.payload))

    def record_measurement(self, job_id: str, body: dict[str, Any]) -> Any:
        """Record the body and return the next scripted answer."""
        self.sent.append((job_id, body))
        answer = self.answers.pop(0) if self.answers else (200, {"result": "stored"})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        """Mark the client closed."""
        self.closed = True


def test_a_frozen_measurement_is_delivered_after_the_turn(monkeypatch, tmp_path):
    """The record the exporter wrote reaches the control plane."""

    sink = _sink_with(monkeypatch, tmp_path, [_frozen()])
    control = _StubControl([(200, {"result": "stored"})])

    counts = runner_mod.deliver_measurements(control=control, job_id="ajob_1")

    assert counts == {"delivered": 1, "duplicate": 0, "dropped": 0, "retried": 0}
    assert len(control.sent) == 1
    job_id, body = control.sent[0]
    assert job_id == "ajob_1"
    assert body["record"]["measurement_id"] == "msm_1"
    # The manifest travels with the record, derived from the same descriptor.
    assert body["run"]["run_id"] == "run-1"
    assert body["run"]["operations"] == ["attempt.startup"]
    # The sink is append-only and may be shared, so the record stays in it
    # and this process remembers it was delivered. Rewriting the file to
    # remove it would race another process appending to the same path.
    with open(sink) as handle:
        assert len([line for line in handle if line.strip()]) == 1
    assert {"msm_1"} == runner_mod._DELIVERED_MEASUREMENT_IDS


def test_a_duplicate_is_counted_as_such(monkeypatch, tmp_path):
    """The control plane already having it is a success, said differently."""
    _sink_with(monkeypatch, tmp_path, [_frozen()])
    control = _StubControl([(200, {"result": "duplicate"})])

    counts = runner_mod.deliver_measurements(control=control, job_id="ajob_1")

    assert counts["duplicate"] == 1
    assert counts["delivered"] == 0


def test_a_refusal_is_not_retried(monkeypatch, tmp_path):
    """A conflict is terminal: the same bytes get the same answer."""
    _sink_with(monkeypatch, tmp_path, [_frozen()])
    control = _StubControl([(200, {"result": "run_conflict"})])

    counts = runner_mod.deliver_measurements(control=control, job_id="ajob_1")

    assert counts["dropped"] == 1
    assert counts["retried"] == 0
    assert len(control.sent) == 1, "a refusal must not be sent again"


def test_a_transport_failure_is_retried_then_given_up_on(monkeypatch, tmp_path):
    """A destination that never answers costs bounded attempts, not a hang."""
    import httpx

    _sink_with(monkeypatch, tmp_path, [_frozen()])
    failure = httpx.ConnectError("refused")
    control = _StubControl([failure, failure, failure])

    counts = runner_mod.deliver_measurements(control=control, job_id="ajob_1")

    assert counts["dropped"] == 1
    assert len(control.sent) == 3, "the stated attempt ceiling, no more"


def test_a_turn_with_no_measurement_delivers_nothing(monkeypatch, tmp_path):
    """Profiling off means no sink, no request and no cost."""
    import os

    from cloud_agent_shared import operation_profiling

    monkeypatch.delenv(operation_profiling.ENV_PROFILING_SINK, raising=False)
    control = _StubControl([])

    counts = runner_mod.deliver_measurements(control=control, job_id="ajob_1")

    assert counts == {"delivered": 0, "duplicate": 0, "dropped": 0, "retried": 0}
    assert control.sent == []
    assert not os.path.exists(str(tmp_path / "records.jsonl"))


def test_runtime_delivery_does_not_require_the_optional_jsonl_sink(monkeypatch):
    """Admin persistence is independent of the operator's baseline file."""
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_SINK, raising=False)
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert measurement is not None
    operation_profiling.finish(measurement, outcome="dispatched")
    control = _StubControl([(200, {"result": "stored"})])
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_args, **_kwargs: control)

    runner_mod._enqueue_delivery(
        measurement,
        job=_claimed(),
        base_url="http://control",
        drain_s=1.0,
    )

    assert [body["record"]["measurement_id"] for _job, body in control.sent] == [
        measurement.measurement_id
    ]
    assert control.sent[0][1]["run"]["admission_instances"]


def test_only_this_turns_record_is_delivered(monkeypatch, tmp_path):
    """A sink shared with an earlier turn does not re-send its samples."""
    _sink_with(monkeypatch, tmp_path, [_frozen("msm_old"), _frozen("msm_this")])
    control = _StubControl([(200, {"result": "stored"})])

    counts = runner_mod.deliver_measurements(
        control=control, job_id="ajob_1", measurement_ids=["msm_this"]
    )

    assert counts["delivered"] == 1
    assert [body["record"]["measurement_id"] for _job, body in control.sent] == ["msm_this"]


# ── When the startup sample is queued, and who waits for it ────────────


def _claimed(job_id: str = "ajob_1") -> Any:
    """A claimed turn, with only the fields the delivery path reads."""
    from cloud_agent_host.runner import ClaimedJob

    return ClaimedJob(
        job_id=job_id,
        thread_id="athr_1",
        attempt_id=7,
        attempt_no=1,
        repo="o/n",
        base_sha=None,
        task_prompt="do it",
        runtime="claude-code",
        model="m",
        worker_token="ajt.a.b",
        sandbox_token="grant",
    )


@pytest.fixture
def startup_measurement(monkeypatch) -> Any:
    """A selected, frozen startup measurement, under a live experiment."""
    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert measurement is not None, "the experiment must select this operation"
    operation_profiling.finish(measurement, outcome="dispatched")
    yield measurement
    operation_profiling.reset_cache()


class _IdleSupervisor:
    """A supervisor whose turn produces no frames and ends at once."""

    def start_shared_turn(self, *_args: Any, **_kwargs: Any) -> Any:
        """Return a stream that immediately ends."""
        return _EmptyStream()


class _IdleHeart:
    """A lease that is alive and never asks to cancel."""

    lease_lost = False
    cancel_requested = False


class _EmptyStream:
    """A sandbox manager stream that ends as soon as it is read."""

    def frames(self) -> Any:
        """Yield the one frame that ends the turn."""
        return iter(({"kind": "result", "exit_code": 0},))

    def close(self) -> None:
        """Release nothing."""


def test_the_streaming_helper_invokes_the_freeze_callback(startup_measurement):
    """The prompt handoff is where the startup ends, so it is where it is sent.

    Left to the turn's exit, a long turn would report its startup an hour late
    -- and past the experiment's grace period the endpoint would refuse a
    perfectly good sample.
    """
    measurement = startup_measurement
    frozen: list[Any] = []
    control = _StubControl([])
    runner_mod._stream_shared_agent_turn(
        supervisor=_IdleSupervisor(),
        job=_claimed(job_id="ajob_1"),
        control=control,
        heart=_IdleHeart(),
        lease_token="lease",
        timeout_s=1.0,
        measurement=measurement,
        on_frozen=frozen.append,
    )

    assert frozen == [measurement], "the record must leave the host at the handoff"
    assert control.events == [("lifecycle", {"phase": "turn_started"})]


def test_the_call_path_actually_passes_the_freeze_callback():
    """The callback has to be threaded to where the freeze happens.

    ``_frozen_measurement`` was defined at the call site and never passed, so
    the handoff queued nothing and the startup still waited for the model. A
    behaviour test cannot see that: the helper is correct and the *wiring* is
    missing, which only the call site can show.
    """
    import ast
    import inspect

    source = ast.parse(inspect.getsource(runner_mod._run_shared_live_attempt))
    calls = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_stream_shared_agent_turn"
    ]

    assert calls, "the streaming helper must be called from the attempt"
    passed = {keyword.arg for keyword in calls[0].keywords}
    assert "on_frozen" in passed, (
        "the streaming helper receives no on_frozen, so a startup record waits "
        "for the model to finish"
    )


def test_a_later_caller_waits_for_the_send_already_running(
    startup_measurement, monkeypatch, tmp_path
):
    """Dedup must not turn two bounded attempts into none.

    The handoff can only afford a short wait because the turn goes on working;
    the turn's exit is the one that can afford a long one. An exit that
    returned immediately on "already queued" would make that sweep a no-op for
    exactly the case it exists for.
    """
    import threading

    _sink_with(monkeypatch, tmp_path, [_frozen()])
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()

    def _slow_send(**_kwargs: Any) -> Any:
        def _run() -> None:
            started.set()
            # Held until the test lets go: the slow destination the second
            # caller is supposed to wait for.
            release.wait(timeout=10)
            finished.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(runner_mod, "_deliver_in_background", _slow_send)
    measurement = startup_measurement
    job = _claimed()
    try:
        runner_mod._enqueue_delivery(measurement, job=job, base_url="http://control", drain_s=0.05)
        assert started.wait(timeout=5), "the first enqueue must start a send"
        assert not finished.is_set()

        # The handoff's short bound returns while the send is still in flight.
        runner_mod._enqueue_delivery(measurement, job=job, base_url="http://control", drain_s=0.05)
        assert not finished.is_set(), "the handoff's short bound must not wait it out"

        # The exit's longer bound waits for the same in-flight send rather than
        # declining to try, which is the whole point of leaving it running.
        release.set()
        runner_mod._enqueue_delivery(measurement, job=job, base_url="http://control", drain_s=5.0)
        assert finished.is_set(), "the exit sweep waited for the send already running"
    finally:
        release.set()


def test_prompt_handoff_starts_delivery_without_waiting(startup_measurement, monkeypatch, tmp_path):
    """A diagnostic send must not sit between prompt dispatch and stream reads."""
    import threading
    import time

    _sink_with(monkeypatch, tmp_path, [_frozen()])
    release = threading.Event()

    def _slow_send(**_kwargs: Any) -> Any:
        thread = threading.Thread(target=lambda: release.wait(timeout=10), daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(runner_mod, "_deliver_in_background", _slow_send)
    try:
        started = time.perf_counter()
        runner_mod._enqueue_delivery(
            startup_measurement,
            job=_claimed(),
            base_url="http://control",
            drain_s=runner_mod.DELIVERY_HANDOFF_DRAIN_S,
        )
        elapsed = time.perf_counter() - started
        assert elapsed < 0.05
    finally:
        release.set()


def test_concurrent_enqueues_start_only_one_delivery(startup_measurement, monkeypatch):
    """The measurement id is reserved atomically before a thread is started."""
    import threading

    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def _slow_start(**_kwargs: Any) -> Any:
        calls.append("start")
        entered.set()
        release.wait(timeout=5)
        thread = threading.Thread(target=lambda: None)
        thread.start()
        return thread

    monkeypatch.setattr(runner_mod, "_deliver_in_background", _slow_start)
    first = threading.Thread(
        target=lambda: runner_mod._enqueue_delivery(
            startup_measurement,
            job=_claimed(),
            base_url="http://control",
            drain_s=0,
        )
    )
    first.start()
    assert entered.wait(timeout=1)
    runner_mod._enqueue_delivery(
        startup_measurement,
        job=_claimed(),
        base_url="http://control",
        drain_s=0,
    )
    release.set()
    first.join(timeout=1)

    assert calls == ["start"]


def test_delivery_byte_budget_drops_before_starting_a_thread(startup_measurement, monkeypatch):
    """The count bound cannot hide an oversized in-memory delivery set."""
    starts: list[str] = []
    monkeypatch.setattr(runner_mod, "DELIVERY_BYTES_MAX", 1)
    monkeypatch.setattr(
        runner_mod,
        "_deliver_in_background",
        lambda **_kwargs: starts.append("started"),
    )

    runner_mod._enqueue_delivery(
        startup_measurement,
        job=_claimed(),
        base_url="http://control",
        drain_s=0,
    )

    assert starts == []
    assert runner_mod._DELIVERY_DROPPED_COUNT == 1
    assert runner_mod._DELIVERY_PENDING_BYTES == 0


def test_delivery_client_startup_failure_releases_its_queue_bytes(startup_measurement, monkeypatch):
    """A client constructor failure cannot permanently consume the byte bound."""

    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("client unavailable")

    monkeypatch.setattr(runner_mod, "ControlPlane", _fail)
    runner_mod._enqueue_delivery(
        startup_measurement,
        job=_claimed(),
        base_url="http://control",
        drain_s=1,
    )

    assert runner_mod._DELIVERY_PENDING_BYTES == 0
    assert runner_mod._IN_FLIGHT_DELIVERIES == {}


def test_a_send_that_never_started_is_retried_by_the_next_caller(
    startup_measurement, monkeypatch, tmp_path
):
    """A thread that could not start leaves nothing to dedup against."""
    _sink_with(monkeypatch, tmp_path, [_frozen()])
    measurement = startup_measurement
    attempts: list[str] = []

    def _refuse(**kwargs: Any) -> Any:
        attempts.append("tried")
        return None  # as a failed thread start reports itself

    monkeypatch.setattr(runner_mod, "_deliver_in_background", _refuse)

    for _ in range(2):
        runner_mod._enqueue_delivery(
            measurement, job=_claimed(), base_url="http://control", drain_s=0.05
        )

    assert len(attempts) == 2, "a send that never began must not be remembered as queued"


def test_the_first_enqueue_also_waits_its_bound(startup_measurement, monkeypatch, tmp_path):
    """The exit sweep is often the *first* caller, and it must still wait.

    A turn that fails before dispatch never reaches the handoff, so the exit's
    sweep starts the send. Returning as soon as the thread was running meant
    that path got no bounded wait at all, and the process could exit with the
    send in flight.
    """
    import threading

    _sink_with(monkeypatch, tmp_path, [_frozen()])
    release = threading.Event()
    finished = threading.Event()

    def _slow_send(**_kwargs: Any) -> Any:
        def _run() -> None:
            release.wait(timeout=10)
            finished.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread

    monkeypatch.setattr(runner_mod, "_deliver_in_background", _slow_send)
    try:
        # Released only from another thread, so the waiting call is the one
        # being measured: with no wait it returns while `_run` is still going.
        threading.Timer(0.2, release.set).start()
        runner_mod._enqueue_delivery(
            startup_measurement,
            job=_claimed(),
            base_url="http://control",
            drain_s=5.0,
        )
        assert finished.is_set(), "the first enqueue must wait out its own bound"
    finally:
        release.set()


def test_a_sink_that_does_not_exist_yet_is_not_a_completed_send(
    startup_measurement, monkeypatch, tmp_path
):
    """A record queued in the exporter is not yet a file on disk.

    The existence check ran before the drain, so a frozen record whose JSONL
    had not been created yet was read as "nothing to send" -- and the turn's
    exit only joins a send that already finished, so it was lost for good.
    """
    import json as _json

    from cloud_agent_shared import operation_profiling

    class _Recorder:
        """Records what a delivery would have sent, without a network."""

        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        def record_measurement(self, job_id: str, body: dict[str, Any]) -> Any:
            """Keep the body and report it stored."""
            self.sent.append(body)
            return (200, {"result": "stored"})

        def close(self) -> None:
            """Release nothing."""

    sink = tmp_path / "late" / "records.jsonl"
    sink.parent.mkdir()
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_RUN, _json.dumps(_descriptor()))
    operation_profiling.reset_cache()

    # Hold the exporter's write until the delivery drains it. Left to its own
    # thread, the exporter can win the race against the assertion below on a
    # busy machine; gated, the record is certainly still queued when the
    # delivery starts, and only the delivery's own drain can release it.
    import threading

    drained = threading.Event()
    publish = operation_profiling._Exporter._publish
    flush_sink = operation_profiling.flush_sink

    def held_publish(self: Any, line: str, target: str | None) -> None:
        drained.wait(timeout=10)
        publish(self, line, target)

    def draining_flush(timeout: float = 2.0) -> None:
        drained.set()
        flush_sink(timeout)

    monkeypatch.setattr(operation_profiling._Exporter, "_publish", held_publish)
    monkeypatch.setattr(operation_profiling, "flush_sink", draining_flush)

    # The record is frozen into the exporter while the file does not exist.
    frozen = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert frozen is not None
    operation_profiling.finish(frozen, outcome="dispatched")
    assert not sink.exists(), "the exporter has not written it yet"

    control = _Recorder()
    counts = runner_mod.deliver_measurements(
        control=control, job_id="ajob_1", measurement_ids=[frozen.measurement_id]
    )

    assert counts["delivered"] == 1, "the queued record must still be delivered"
    assert [body["record"]["measurement_id"] for body in control.sent] == [frozen.measurement_id]
    operation_profiling.reset_cache()


def test_the_startup_record_is_delivered_while_the_turn_is_still_running(monkeypatch, tmp_path):
    """A long turn must not delay its own startup sample.

    The prompt handoff is where the startup ends, so the record must leave the
    host there -- while the model may run for another hour. Delivering it at
    the turn's exit would delay an operator's view of it and, past the
    experiment's grace period, deliver it after the endpoint has begun refusing
    that run's records.
    """
    import threading

    from cloud_agent_shared import operation_profiling

    _run_descriptor(monkeypatch)
    sink = tmp_path / "records.jsonl"
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    operation_profiling.reset_cache()

    sent: list[str] = []
    turn_is_running = threading.Event()
    #: Set by the recorder the instant the record arrives, so the test thread
    #: waits on the send itself instead of polling a list for it.
    delivered = threading.Event()
    release_delivery = threading.Event()
    release_turn = threading.Event()
    frames_entered = threading.Event()

    class _Recorder:
        def record_measurement(self, job_id: str, body: dict[str, Any]) -> Any:
            """Keep the measurement id, then hold the delivery open.

            Held so the assertion can prove the sample left the host *while the
            turn was still running*: without this the turn could have ended
            first and the ordering being tested would go unobserved.
            """
            sent.append(str(body["record"]["measurement_id"]))
            delivered.set()
            release_delivery.wait(timeout=10)
            return (200, {"result": "stored"})

        def close(self) -> None:
            """Release nothing."""

    class _HangingSupervisor:
        """A model that has not finished: the turn is still in flight."""

        def start_shared_turn(self, *_args: Any, **_kwargs: Any) -> Any:
            """Hand back a stream that never produces a frame."""
            turn_is_running.set()
            return _HangingStream()

    class _HangingStream:
        def frames(self) -> Any:
            """Block until the test is done with the assertion."""
            frames_entered.set()
            release_turn.wait(timeout=10)
            return iter(({"kind": "result", "exit_code": 0},))

        def close(self) -> None:
            """Release nothing."""

    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert measurement is not None
    stream_control = _StubControl([])
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_a, **_k: _Recorder())
    try:
        turn = threading.Thread(
            target=lambda: runner_mod._stream_shared_agent_turn(
                supervisor=_HangingSupervisor(),
                job=_claimed(),
                control=stream_control,
                heart=_IdleHeart(),
                lease_token="lease",
                timeout_s=10.0,
                measurement=measurement,
                on_frozen=lambda frozen: runner_mod._enqueue_delivery(
                    frozen,
                    job=_claimed(),
                    base_url="http://control",
                    drain_s=runner_mod.DELIVERY_HANDOFF_DRAIN_S,
                ),
            ),
            daemon=True,
        )
        turn.start()
        assert turn_is_running.wait(timeout=5), "the turn must have started"
        assert delivered.wait(timeout=5), "the startup sample must reach the destination"
        assert frames_entered.wait(timeout=5), "the turn must be waiting for runtime output"
        assert stream_control.events == [("lifecycle", {"phase": "turn_started"})]

        # The record is out while the model is still working. The delivery is
        # held open, so the turn cannot have finished before this was sent.
        assert sent == [measurement.measurement_id], (
            "the startup sample must be delivered at the handoff, not at the turn's exit"
        )
        assert not release_delivery.is_set()
        assert turn.is_alive(), "the turn was still running when the sample was sent"
        # And the frozen record really is the startup's, not a placeholder. The
        # sink is written by the exporter thread, so drain it first rather than
        # reading a file the record has been queued for but not yet reached --
        # which is a race, not the ordering under test.
        operation_profiling.flush_sink()
        written = [json.loads(line) for line in sink.read_text().splitlines() if line.strip()]
        assert written and written[0]["operation"] == "attempt.startup"
    finally:
        release_delivery.set()
        release_turn.set()
        turn.join(timeout=2)
        operation_profiling.reset_cache()


def test_profiling_off_sends_nothing_at_all(monkeypatch, tmp_path):
    """The switch is the promise: off means no request, no sink, no record."""
    from cloud_agent_shared import operation_profiling

    monkeypatch.delenv(operation_profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.delenv(operation_profiling.ENV_PROFILING_RUN, raising=False)
    sink = tmp_path / "records.jsonl"
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    operation_profiling.reset_cache()

    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )

    assert measurement is None, "off must not select an operation"
    assert not sink.exists(), "off must not write a sink"
    operation_profiling.reset_cache()
