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

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeHandle:
    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
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

    def stop(self) -> None:
        self.stopped = True

    def wake(self) -> None:
        self.wakes += 1


@pytest.fixture
def service(monkeypatch):
    """``run_forever`` with its enrollment, heartbeat and attestation stubbed."""
    runner_mod._STOPPING.clear()
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", "sha")
    monkeypatch.setenv("AGENT_HOST_SLOTS", "3")
    identity = HostIdentity(host="host-a", credential="host-cred", status="online")
    monkeypatch.setattr(runner_mod, "ensure_enrolled", lambda **_kw: identity)
    monkeypatch.setattr(runner_mod, "HostHeartbeat", FakeHeartbeat)
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
    handle = FakeHandle()

    _serve(
        backend,
        launch=lambda _p: LaunchResult(LAUNCH_UNCERTAIN, handle, "no answer"),
    )

    assert released == []
    assert handle.terminated, "still waited for, like any other turn in flight"


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


# ── The child ──────────────────────────────────────────────────────────


def test_a_turn_process_declines_an_empty_delivery_and_runs_nothing(monkeypatch, capsys):
    """What the deployment check exercises, and what a hand-started process does.

    It declines, but only after preflighting — so a rollout that runs this
    against a broken image learns that the machine could not have run a turn,
    rather than only that Python started.
    """
    import os

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
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


def test_a_turn_process_declines_when_its_backend_is_not_usable(monkeypatch):
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
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
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
