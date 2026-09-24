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
        "_profiling": {
            "run_id": "run-1",
            "measurement_id": "msm_handed",
            "operation": "attempt.startup",
            "producer": "runner",
            "coverage_scope": "host_startup",
            "facts": {"operation": "attempt.startup", "job_id": "ajob_1"},
            "call_id": "",
            "expires_at": __import__("time").time() + 600,
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)

    handed: list[Any] = []

    def _capture(*_args: Any, **kwargs: Any) -> int:
        handed.append(kwargs.get("measurement"))
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
    os.close(read_fd)
    operation_profiling.reset_cache()


def test_a_child_never_refuses_a_turn_over_an_unusable_selection(monkeypatch):
    """A measurement setting is not a reason to drop a user's turn."""
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
        # A run this process is not configured for: nothing may be measured,
        # and the turn still runs.
        "_profiling": {"run_id": "another-run", "expires_at": 2**31},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(encode_turn(payload).decode()))
    backend = ProcessBackend(acknowledged_unsafe=True)
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
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
    os.close(read_fd)


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
    """Every test starts having delivered nothing."""
    runner_mod.reset_delivery_state()
    yield
    runner_mod.reset_delivery_state()


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
        self.closed = False

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
    """A broker stream that ends as soon as it is read."""

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
    runner_mod._stream_shared_agent_turn(
        supervisor=_IdleSupervisor(),
        job=_claimed(job_id="ajob_1"),
        control=_StubControl([]),
        heart=_IdleHeart(),
        lease_token="lease",
        timeout_s=1.0,
        measurement=measurement,
        on_frozen=frozen.append,
    )

    assert frozen == [measurement], "the record must leave the host at the handoff"


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
            release_turn.wait(timeout=10)
            return iter(({"kind": "result", "exit_code": 0},))

        def close(self) -> None:
            """Release nothing."""

    measurement = operation_profiling.start_operation(
        operation=operation_profiling.STARTUP_OPERATION, host="host-a"
    )
    assert measurement is not None
    real_control = runner_mod.ControlPlane
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *_a, **_k: _Recorder())
    try:
        turn = threading.Thread(
            target=lambda: runner_mod._stream_shared_agent_turn(
                supervisor=_HangingSupervisor(),
                job=_claimed(),
                control=real_control("http://control", "t"),
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
