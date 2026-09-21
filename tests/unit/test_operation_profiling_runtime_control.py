"""What a runtime control source changes about collection, and what it must not.

The collector used to answer "is profiling on" from process configuration, read
once, at startup. It now also answers from a configuration another process
publishes, and that is only safe if four properties hold:

* the snapshot is replaced whole, and **a version never goes backwards**, so a
  delayed answer cannot put a stopped experiment back into force;
* a Stop stops *selecting new operations* -- and does **not** reach into work
  that already started, which finishes and delivers under the configuration that
  selected it;
* the experiment's own absolute deadline is what ends collection, including on a
  node that never hears anything again;
* nothing about the previous experiment leaks into the next one.

Each test here pins one of those through the same ``install_policy_provider`` /
``policy_store`` seam the control plane uses, so the rules are stated in one
place instead of being inferred from a worker.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cloud_agent_profiling import operation_profiling as profiling

RUN_A = "admin-20260921T120000Z-aaaa1111"
RUN_B = "admin-20260921T121000Z-bbbb2222"
ENV_RUN_ID = "env-run"


class _FakeProvider:
    """A control source a test drives by hand.

    Synchronous because that is the seam's contract: the collector asks for a
    snapshot on the path that decides whether a request is measured, and a
    provider that had to await would put I/O there.
    """

    def __init__(self, state: profiling.ControlState | None = None) -> None:
        self.state = state
        self.reads = 0

    def snapshot(self) -> profiling.ControlState:
        """Return the state this test published, counting the read."""
        self.reads += 1
        if self.state is None:
            return profiling.ControlState(mode=profiling.MODE_ADMIN)
        return self.state


class _BrokenProvider:
    """A control source that cannot answer at all."""

    def snapshot(self) -> profiling.ControlState:
        """Fail, which must read as "no run" rather than as an experiment."""
        raise RuntimeError("control source is unreachable")


@pytest.fixture(autouse=True)
def _clean_profiling_state(monkeypatch):
    """Every test starts from a process that never read a control source."""
    monkeypatch.delenv(profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_RUN, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_SINK, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_MODE, raising=False)
    profiling.reset_cache()
    profiling.reset_policy()
    yield
    # The provider is process-global, so a test that installed one must not be
    # able to answer for the next test's process.
    profiling.install_policy_provider(None)
    profiling.reset_cache()


def _expiry(minutes: int = 60) -> str:
    """Return an ISO-8601 expiry that is still in the future."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _run(
    *,
    run_id: str = RUN_A,
    operations: tuple[str, ...] = ("terminal.open",),
    target: dict[str, str] | None = None,
    max_operations: int = 10,
    config_version: int = 1,
    expires_at: str | None = None,
    source: str = profiling.SOURCE_ADMIN,
) -> profiling.ProfilingRun:
    """Return a parsed run descriptor, as the control loop builds one.

    Rebuilt through the collector's own parser rather than assembled field by
    field, because that is what the loop does and a hand-built dataclass could
    accept a descriptor the parser would refuse.
    """
    descriptor = {
        "run_id": run_id,
        "operations": list(operations),
        "target": dict(target or {}),
        "expires_at": expires_at or _expiry(),
        "config_version": config_version,
        "max_operations": max_operations,
    }
    return replace(profiling.parse_run(json.dumps(descriptor)), source=source)


def _state(
    *,
    run: profiling.ProfilingRun,
    revision: int = 1,
) -> profiling.ControlState:
    """Return the configuration a control source publishes for one live run."""
    return profiling.ControlState(
        mode=profiling.MODE_ADMIN,
        config_version=revision,
        run=run,
    )


def _stopped(*, revision: int = 2, reason: str = profiling.STOP_REASON_STOPPED):
    """Return the configuration a Stop publishes: the tombstone, and no run."""
    return profiling.ControlState(
        mode=profiling.MODE_ADMIN,
        config_version=revision,
        run=None,
        stop_reason=reason,
    )


def _admin(monkeypatch, state: profiling.ControlState | None = None) -> _FakeProvider:
    """Put this process under Admin control and publish one snapshot."""
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ADMIN)
    profiling.reset_cache()
    provider = _FakeProvider(state)
    profiling.install_policy_provider(provider)
    return provider


def _enable_environment(monkeypatch, **overrides) -> dict:
    """Turn the legacy switch on with a valid static run descriptor."""
    run = {
        "run_id": ENV_RUN_ID,
        "operations": ["attempt.startup"],
        "expires_at": _expiry(),
        "max_operations": 10,
    }
    run.update(overrides)
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, "true")
    monkeypatch.setenv(profiling.ENV_PROFILING_RUN, json.dumps(run))
    profiling.reset_cache()
    return run


def _records(caplog) -> list[dict]:
    """Return every emitted record, draining the exporter first."""
    profiling.flush_sink()
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("agent_operation_profile "):
            found.append(json.loads(message.split(" ", 1)[1]))
    return found


# ---------------------------------------------------------------------------
# Mode resolution: the mode decides who may control this deployment
# ---------------------------------------------------------------------------


def test_no_mode_and_no_switch_is_disabled():
    """A deployment that configures nothing must collect nothing."""
    assert profiling.profile_mode() == profiling.MODE_DISABLED
    assert profiling.active_run() is None
    assert profiling.reason() == "not_enabled"
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_no_mode_still_honours_the_legacy_switch(monkeypatch):
    """The pre-existing ENABLED + RUN pair must behave exactly as it did."""
    _enable_environment(monkeypatch)

    assert profiling.profile_mode() == profiling.MODE_ENVIRONMENT
    live = profiling.active_run()
    assert live is not None and live.run_id == ENV_RUN_ID

    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    assert measurement.run_source == profiling.SOURCE_ENVIRONMENT
    # A static descriptor has no control source, so it carries no configuration
    # version: nothing published it and nothing can withdraw it.
    assert measurement.control_revision == 0


def test_mode_disabled_vetoes_a_valid_switch_and_run(monkeypatch):
    """``disabled`` is the deployment refusing runtime control altogether."""
    _enable_environment(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_DISABLED)
    profiling.reset_cache()

    assert profiling.profile_mode() == profiling.MODE_DISABLED
    assert profiling.active_run() is None
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_environment_mode_ignores_the_published_control_state(monkeypatch):
    """In environment mode the descriptor is configuration, not one source of two."""
    _enable_environment(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ENVIRONMENT)
    profiling.reset_cache()
    profiling.policy_store().publish(
        _state(run=_run(run_id=RUN_A, operations=("terminal.open",)), revision=7)
    )

    live = profiling.active_run()
    assert live is not None and live.run_id == ENV_RUN_ID
    assert profiling.control_state().config_version == 0
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is not None


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_admin_mode_ignores_the_environment_descriptor_entirely(monkeypatch, enabled):
    """A leftover RUN must not resurrect an experiment after an Admin Stop."""
    _enable_environment(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_ENABLED, enabled)
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ADMIN)
    profiling.reset_cache()

    assert profiling.profile_mode() == profiling.MODE_ADMIN
    assert profiling.active_run() is None
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_an_unrecognised_mode_is_disabled_and_says_so(monkeypatch, caplog):
    """A typo must refuse to collect and say so, not fall back to the old mode.

    Regression: ``_read_mode`` wrote the rejected value without a ``global``, so
    the operator saw a deployment that had quietly stopped collecting and no
    line explaining why.
    """
    _enable_environment(monkeypatch)
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, "Administrator")
    profiling.reset_cache()
    caplog.set_level(logging.WARNING, logger=profiling.__name__)

    assert profiling.profile_mode() == profiling.MODE_DISABLED
    assert profiling.active_run() is None
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None
    assert "is not a profiling mode" in caplog.text


def test_the_mode_is_read_once_per_process(monkeypatch):
    """Changing how a deployment may be controlled is a redeploy, not a request."""
    assert profiling.profile_mode() == profiling.MODE_DISABLED

    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ADMIN)

    assert profiling.profile_mode() == profiling.MODE_DISABLED


# ---------------------------------------------------------------------------
# The store is the only thing that decides what is collected
# ---------------------------------------------------------------------------


def test_an_admin_run_is_selected_through_the_published_snapshot(monkeypatch):
    """Nothing about an admin experiment comes from this process's environment."""
    run = _run(run_id=RUN_A, operations=("terminal.open",), target={"host": "host-a"})
    provider = _admin(monkeypatch, _state(run=run, revision=3))

    assert provider.reads == 1
    assert profiling.policy_provider() is provider
    assert profiling.active_run() is run
    assert profiling.control_state().config_version == 3
    assert profiling.control_state().mode == profiling.MODE_ADMIN

    measurement = profiling.start_operation(operation="terminal.open", host="host-a")
    assert measurement is not None
    assert measurement.run_id == RUN_A
    assert profiling.start_operation(operation="files.list", host="host-a") is None

    manifest = profiling.manifest_for_measurement(measurement)
    assert manifest is not None
    assert manifest["run_id"] == RUN_A
    assert manifest["operations"] == ["terminal.open"]
    assert manifest["target"] == {"host": "host-a"}
    assert manifest["expires_at"] == run.expires_at_utc.isoformat()


def test_bind_applies_the_published_run_target(monkeypatch):
    """A target from the snapshot is authoritative for a measurement from it."""
    run = _run(run_id=RUN_A, target={"host": "host-a"})
    _admin(monkeypatch, _state(run=run, revision=1))

    matched = profiling.start_operation(operation="terminal.open", host="host-a")
    assert matched is not None
    assert profiling.bind(matched, host="host-a") is True

    mismatched = profiling.start_operation(operation="terminal.open", host="host-a")
    assert mismatched is not None
    assert profiling.bind(mismatched, host="host-b") is False


def test_an_installed_provider_that_was_not_read_collects_nothing(monkeypatch):
    """There is no previous run to fall back to, which is what survives a restart."""
    run = _run(run_id=RUN_A)
    _admin(monkeypatch, _state(run=run, revision=1))
    assert profiling.active_run() is run

    profiling.reset_policy()
    assert profiling.active_run() is None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None

    profiling.refresh_policy()
    assert profiling.active_run() is run
    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None


def test_a_control_source_that_fails_collects_nothing(monkeypatch, caplog):
    """An unreachable control source must never be read as an experiment."""
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ADMIN)
    profiling.reset_cache()
    caplog.set_level(logging.WARNING, logger=profiling.__name__)

    profiling.install_policy_provider(_BrokenProvider())

    assert profiling.active_run() is None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None
    assert "control source is unavailable" in caplog.text


def test_the_store_refuses_a_run_that_does_not_describe_its_policy():
    """Half a snapshot is exactly the torn state the store exists to prevent."""
    policy = _state(run=_run(run_id=RUN_A), revision=1)

    with pytest.raises(ValueError):
        profiling.policy_store().publish(policy, run=_run(run_id=RUN_B))


# ---------------------------------------------------------------------------
# A Stop stops selecting; it does not reach into work already under way
# ---------------------------------------------------------------------------


def test_a_stop_stops_selecting_new_operations(monkeypatch):
    """The switch is about new work, and the deadline bounds the old."""
    run = _run(run_id=RUN_A)
    _admin(monkeypatch, _state(run=run, revision=1))
    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None

    profiling.policy_store().publish(_stopped(revision=2))

    assert profiling.active_run() is None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None


def test_a_stop_does_not_end_a_measurement_that_already_started(monkeypatch, caplog):
    """Work in flight finishes under the configuration that selected it.

    This is the whole simplified Stop contract: disabling collection must not
    truncate a sample that a user's request is still producing, and must not
    change what that sample says about itself.
    """
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    run = _run(run_id=RUN_A)
    _admin(monkeypatch, _state(run=run, revision=1))
    measurement = profiling.start_operation(operation="terminal.open", host="host-a")
    assert measurement is not None

    with profiling.span(measurement, "serving"):
        profiling.policy_store().publish(_stopped(revision=2))

    # Still live: the deadline is the experiment's, not the switch's.
    assert measurement.past_deadline() is False
    assert profiling.measurement_context(measurement) is not None
    profiling.finish(measurement, outcome="completed")

    record = _records(caplog)[0]
    assert record["profiling_run_id"] == RUN_A
    assert record["collection_complete"] is True
    assert "truncated" not in record
    assert "profiling_stopped" not in record["facts"]
    # The record still names the experiment that selected it.
    manifest = profiling.manifest_for_measurement(measurement)
    assert manifest is not None and manifest["run_id"] == RUN_A


def test_the_experiment_deadline_is_what_ends_an_open_measurement(monkeypatch, caplog):
    """A node that never hears the Stop stops at the experiment's own end."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    # The run is published live -- the parser refuses a past expiry -- and the
    # measurement is then aged past its own deadline, which is exactly "the
    # experiment ended while this was open".
    _admin(monkeypatch, _state(run=_run(run_id=RUN_A), revision=1))
    measurement = profiling.start_operation(operation="terminal.open", host="host-a")
    assert measurement is not None
    measurement.deadline_mono = time.monotonic() - 0.001

    assert measurement.past_deadline() is True
    assert measurement.expired is True
    profiling.finish(measurement, outcome="completed")

    record = _records(caplog)[0]
    assert record["facts"]["profiling_stopped"] == profiling.STOP_REASON_EXPIRED
    # A sample cut off by the deadline is a floor, not a duration, and it says so.
    assert record["collection_complete"] is False
    assert record["duration_us"] == 0


def test_a_measurement_keeps_the_experiment_that_selected_it(monkeypatch, caplog):
    """A record stored after a switch must not be filed under the new run."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    run_a = _run(run_id=RUN_A, operations=("terminal.open",), target={"host": "host-a"})
    run_b = _run(run_id=RUN_B, operations=("files.list",), target={"host": "host-b"})
    _admin(monkeypatch, _state(run=run_a, revision=1))

    measurement_a = profiling.start_operation(operation="terminal.open", host="host-a")
    assert measurement_a is not None
    assert profiling.bind(measurement_a, host="host-a") is True
    manifest_a = profiling.manifest_for_measurement(measurement_a)
    assert manifest_a is not None and manifest_a["run_id"] == RUN_A

    profiling.policy_store().publish(_state(run=run_b, revision=2))
    live = profiling.active_run()
    assert live is not None and live.run_id == RUN_B

    # The old measurement's answer is unchanged by the switch: same operations,
    # same target, same configured expiry.
    assert profiling.manifest_for_measurement(measurement_a) == manifest_a
    assert manifest_a["operations"] == ["terminal.open"]
    assert manifest_a["target"] == {"host": "host-a"}
    assert manifest_a["expires_at"] == run_a.expires_at_utc.isoformat()

    profiling.finish(measurement_a, outcome="completed")
    assert _records(caplog)[0]["profiling_run_id"] == RUN_A

    # A selection made after the switch belongs to the new experiment.
    fresh = profiling.start_operation(operation="files.list", host="host-b")
    assert fresh is not None
    assert fresh.run_id == RUN_B
    fresh_manifest = profiling.manifest_for_measurement(fresh)
    assert fresh_manifest is not None and fresh_manifest["run_id"] == RUN_B
    assert profiling.start_operation(operation="terminal.open", host="host-b") is None


def test_a_record_states_the_configuration_version_that_selected_it(monkeypatch, caplog):
    """A reader has to be able to tell two versions of the switch apart."""
    caplog.set_level(logging.INFO, logger=profiling.__name__)
    _admin(monkeypatch, _state(run=_run(run_id=RUN_A), revision=9))
    measurement = profiling.start_operation(operation="terminal.open", host="host-a")
    assert measurement is not None
    assert measurement.control_revision == 9
    profiling.finish(measurement, outcome="completed")
    assert _records(caplog)[0]["control_revision"] == 9


# ---------------------------------------------------------------------------
# The broker is offered the configuration again until it holds it
# ---------------------------------------------------------------------------


class _FlakyBroker:
    """A broker client that refuses a fixed number of pushes, then accepts."""

    def __init__(self, *, refuse_first: int = 0) -> None:
        self._refuse_first = refuse_first
        self.calls: list[dict[str, Any]] = []
        self.delivered: dict[str, Any] | None = None

    def apply_profiling_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        """Refuse while the budget lasts, then accept and remember."""
        self.calls.append(dict(policy))
        if len(self.calls) <= self._refuse_first:
            raise RuntimeError("broker is not ready")
        self.delivered = dict(policy)
        return {"accepted": True, "changed": True, "revision": policy["revision"]}


class _FixedControlPlane:
    """A configuration source that always answers with the same experiment."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.reads = 0

    def post(self, *_args: Any, **_kwargs: Any) -> Any:
        """Return the same answer, counting the read."""
        self.reads += 1
        return _FixedResponse(self.answer)


class _FixedResponse:
    """The subset of an HTTP response the sync reads."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self._answer = answer
        self.is_error = False

    def json(self) -> dict[str, Any]:
        """Return the answer body."""
        return self._answer


def _sync_with_broker(monkeypatch, broker) -> Any:
    """A real sync, pointed at a fixed control-plane answer and one broker."""
    from cloud_agent_host.profiling_policy import ProfilingPolicySync

    run = _run(run_id=RUN_A, operations=("terminal.open",))
    answer = {
        "mode": profiling.MODE_ADMIN,
        "revision": 1,
        "run": {
            "run_id": RUN_A,
            "operations": ["terminal.open"],
            "target": {},
            "expires_at": run.expires_at_utc.isoformat(),
            "config_version": 1,
            "max_operations": 10,
        },
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ADMIN)
    profiling.reset_cache()
    sync = ProfilingPolicySync(
        base_url="http://control-plane.invalid",
        credential="credential",
        host="host-a",
        broker=broker,
    )
    monkeypatch.setattr(sync, "_client", _FixedControlPlane(answer))
    return sync


def test_a_failed_broker_push_is_offered_again(monkeypatch):
    """A push that failed must not be the last word, or the broker never hears.

    The runner records the revision as applied as soon as it reads it, so a
    change-only push gave up permanently after two failed attempts: the
    experiment ran with the broker contributing nothing, which is worse than
    not running it at all because the root record still looks like a sample.
    """
    # Two attempts per pass, and four refusals: two whole passes fail before the
    # broker comes up, which is the case that used to be permanent.
    broker = _FlakyBroker(refuse_first=4)
    sync = _sync_with_broker(monkeypatch, broker)

    assert sync._sync_once() == 5.0
    assert broker.delivered is None
    assert sync.state()["broker_ok"] is False

    # The configuration did not change, and that is the point: the next pass
    # offers it again anyway, because the runner cannot tell a failed push from
    # a broker that restarted.
    assert sync._sync_once() == 5.0
    assert broker.delivered is None

    assert sync._sync_once() == 5.0
    assert broker.delivered is not None
    assert broker.delivered["run"]["run_id"] == RUN_A
    assert sync.state()["broker_ok"] is True


def test_a_restarted_broker_is_given_the_configuration_again(monkeypatch):
    """The broker's configuration lives in memory, so a restart empties it."""
    broker = _FlakyBroker()
    sync = _sync_with_broker(monkeypatch, broker)
    assert sync._sync_once() == 5.0
    assert broker.delivered is not None

    # The broker restarts: its applied state is gone and it will accept the same
    # revision again. Nothing tells the runner, so it has to keep offering.
    broker.delivered = None
    for _ in range(3):
        sync._sync_once()

    assert broker.delivered is not None, "a restarted broker must be refilled"
    assert broker.delivered["run"]["run_id"] == RUN_A


# ---------------------------------------------------------------------------
# A selected operation keeps collecting its later stages
# ---------------------------------------------------------------------------


def test_a_selected_operation_keeps_its_later_stages_after_a_stop(monkeypatch):
    """The broker must not refuse the stages of work that was already chosen.

    Disabling stops *selecting*. An attempt that was selected before the switch
    still calls the broker for its container and its CLI process, and those
    fragments belong to the experiment that selected the attempt. Refusing them
    produced a record that was kept but incomplete -- the broker's stages simply
    missing -- which reads as a slow or partial startup rather than as a
    configuration change.
    """
    run = _run(run_id=RUN_A, operations=("attempt.startup",), target={"host": "host-a"})
    _admin(monkeypatch, _state(run=run, revision=1))
    measurement = profiling.start_operation(
        operation="attempt.startup", host="host-a", producer="runner"
    )
    assert measurement is not None
    assert profiling.bind(measurement, host="host-a") is True
    context = profiling.measurement_context(measurement, "c1")
    assert context is not None

    # The switch goes off, in the broker's own store.
    profiling.policy_store().publish(_stopped(revision=2))

    recorder = profiling.start_stage_recording(context, producer="broker")
    assert recorder is not None, "the continuation must outlive the switch"
    assert recorder.operation == "attempt.startup"
    with recorder.stage("container_start"):
        pass
    assert recorder.names() == ["container_start"]
    # And a *new* root operation is still refused.
    assert profiling.start_operation(operation="attempt.startup", host="host-a") is None


def test_a_continuation_cannot_widen_the_operations_it_was_given(monkeypatch):
    """The manifest's operation list still decides, not the caller's candidates.

    A context may narrow what it offers -- a route states the candidates its own
    call could still turn out to be -- and that narrowing is intersected with
    the experiment. It may never widen it: a file read may not collect under an
    operation the experiment that selected it never asked for.
    """
    run = _run(run_id=RUN_A, operations=("attempt.startup",))
    _admin(monkeypatch, _state(run=run, revision=1))
    measurement = profiling.start_operation(
        operation="attempt.startup", host="host-a", producer="runner"
    )
    assert measurement is not None
    context = profiling.measurement_context(measurement, "c1")
    assert context is not None
    profiling.policy_store().publish(_stopped(revision=2))

    widened = dict(context)
    widened["operations"] = ["files.open"]

    assert profiling.start_stage_recording(widened, producer="broker") is None
    # And the operation it *was* selected for still continues.
    assert profiling.start_stage_recording(context, producer="broker") is not None


def _admin_context(monkeypatch) -> tuple[Any, dict[str, Any]]:
    """One real Admin selection and the context a child would be handed."""
    run = _run(run_id=RUN_A, operations=("attempt.startup",), target={"host": "host-a"})
    _admin(monkeypatch, _state(run=run, revision=1))
    measurement = profiling.start_operation(
        operation="attempt.startup", host="host-a", producer="runner"
    )
    assert measurement is not None
    assert profiling.bind(measurement, host="host-a") is True
    context = profiling.handoff_context(measurement)
    assert context is not None
    return measurement, context


def _retime(context: dict[str, Any], *, run_expires_in: float, transport_expires_in: float):
    """Return the same context with the two deadlines stated separately.

    A real context derives both from one measurement, so a test that wants to
    prove which one wins has to state them apart -- which is also what a
    delayed, replayed or hostile context looks like from the receiver's side.
    """
    retimed = json.loads(json.dumps(context))
    retimed["run"]["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=run_expires_in)
    ).isoformat()
    retimed["expires_at"] = time.time() + transport_expires_in
    return retimed


def test_a_disabled_deployment_refuses_a_continuation(monkeypatch):
    """``disabled`` is not the Admin switch. It forbids collection outright.

    A continuation is honoured because a *configuration* may change while work
    is in flight -- which is an Admin-mode statement. A process told the
    deployment does not collect at all must not be talked into allocating a
    measurement or a recorder by an upstream context, however well formed, or
    a staged rollout could not silence a producer.
    """
    _measurement, context = _admin_context(monkeypatch)
    assert profiling.attach_selection(context) is not None

    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_DISABLED)
    profiling.reset_cache()

    assert profiling.profile_mode() == profiling.MODE_DISABLED
    assert profiling.attach_selection(context) is None
    assert profiling.start_stage_recording(context, producer="broker") is None
    # And through the real parser a broker route uses, not just the helpers.
    from cloud_agent_profiling.operation_call import open_call

    assert open_call(json.dumps(context), operations=("attempt.startup",)).recording is False


def test_a_default_off_deployment_refuses_a_continuation(monkeypatch):
    """A deployment that configures nothing collects nothing, however it is asked."""
    _measurement, context = _admin_context(monkeypatch)

    monkeypatch.delenv(profiling.ENV_PROFILING_MODE, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_ENABLED, raising=False)
    monkeypatch.delenv(profiling.ENV_PROFILING_RUN, raising=False)
    profiling.reset_cache()

    assert profiling.profile_mode() == profiling.MODE_DISABLED
    assert profiling.attach_selection(context) is None
    assert profiling.start_stage_recording(context, producer="broker") is None


def test_environment_mode_still_follows_its_own_descriptor(monkeypatch):
    """The static descriptor decides, and it is the same one in parent and child."""
    _measurement, context = _admin_context(monkeypatch)

    # No static experiment configured: the deployment collects nothing.
    monkeypatch.setenv(profiling.ENV_PROFILING_MODE, profiling.MODE_ENVIRONMENT)
    monkeypatch.delenv(profiling.ENV_PROFILING_ENABLED, raising=False)
    profiling.reset_cache()
    assert profiling.attach_selection(context) is None
    assert profiling.start_stage_recording(context, producer="broker") is None

    # A matching static descriptor: the ordinary inherited-environment path,
    # where the local run is the authority rather than the carried manifest.
    _enable_environment(monkeypatch, run_id=RUN_A, operations=["attempt.startup"])
    attached = profiling.attach_selection(context)
    assert attached is not None
    assert attached.run_id == RUN_A
    assert attached.run_source == profiling.SOURCE_ENVIRONMENT


def test_an_inherited_manifest_that_has_expired_is_refused(monkeypatch):
    """The experiment's own end is a ceiling, not decoration.

    A context can outlive the experiment it names -- delayed in a queue, replayed
    after a sync -- and the transport's own expiry says nothing about that. A
    selection may not buy time by travelling.
    """
    _measurement, context = _admin_context(monkeypatch)
    stale = _retime(context, run_expires_in=-60, transport_expires_in=300)

    assert profiling.attach_selection(stale) is None
    assert profiling.start_stage_recording(stale, producer="broker") is None
    from cloud_agent_profiling.operation_call import open_call

    assert open_call(json.dumps(stale), operations=("attempt.startup",)).recording is False


def test_the_earlier_of_the_two_deadlines_wins(monkeypatch):
    """A context with 300 s left on an experiment with 2 s left gets 2 s.

    Both are ceilings and the earlier one applies, which is the same rule the
    environment path already applied to a locally read descriptor.
    """
    _measurement, context = _admin_context(monkeypatch)
    soon = _retime(context, run_expires_in=2, transport_expires_in=300)

    child = profiling.attach_selection(soon)
    assert child is not None
    recorder = profiling.start_stage_recording(soon, producer="broker")
    assert recorder is not None
    for deadline in (child.deadline_mono, recorder.deadline_mono):
        remaining = deadline - time.monotonic()
        assert 0 < remaining <= 3, f"got {remaining} s, not the manifest's 2"


def test_a_context_cannot_outlive_the_experiment_it_names(monkeypatch):
    """The transport's bound is a ceiling too, even when the manifest is later."""
    _measurement, context = _admin_context(monkeypatch)
    short_leash = _retime(context, run_expires_in=300, transport_expires_in=2)

    child = profiling.attach_selection(short_leash)
    assert child is not None
    remaining = child.deadline_mono - time.monotonic()
    assert 0 < remaining <= 3, f"got {remaining} s, not the transport's 2"


def test_a_context_without_a_manifest_still_needs_the_local_configuration(monkeypatch):
    """A caller that only names operations cannot enable collection by claiming."""
    _admin(monkeypatch, _state(run=_run(run_id=RUN_A, operations=("attempt.startup",)), revision=1))
    without_manifest = {
        "run_id": RUN_A,
        "operation": "files.open",
        "operations": ["files.open"],
        "expires_at": time.time() + 60,
        "call_id": "c1",
    }
    assert profiling.start_stage_recording(without_manifest, producer="broker") is None


# ---------------------------------------------------------------------------
# The version never goes backwards, and a re-publish hands nothing back
# ---------------------------------------------------------------------------


def test_an_older_configuration_cannot_replace_a_newer_one(monkeypatch):
    """A delayed answer must not put a stopped experiment back into force."""
    run = _run(run_id=RUN_A)
    _admin(monkeypatch, _state(run=run, revision=5))
    assert profiling.active_run() is not None

    published = profiling.policy_store().publish(_stopped(revision=4))

    assert published.state.config_version == 5
    assert profiling.active_run() is not None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None


def test_the_stopped_version_wins_over_the_one_it_replaced(monkeypatch):
    """The other direction is the one that matters: a Stop must stick."""
    run = _run(run_id=RUN_A)
    _admin(monkeypatch, _state(run=run, revision=5))
    profiling.policy_store().publish(_stopped(revision=6))

    # The old configuration arrives again, as a retry or out of order.
    profiling.policy_store().publish(_state(run=run, revision=5))

    assert profiling.active_run() is None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None


def test_republishing_the_same_run_does_not_hand_back_spent_budget(monkeypatch):
    """Otherwise re-syncing would be a way to collect without limit."""
    run = _run(run_id=RUN_A, max_operations=2)
    _admin(monkeypatch, _state(run=run, revision=1))

    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None

    # The same configuration, read again: same run, same allowance.
    profiling.policy_store().publish(_state(run=run, revision=1))

    assert profiling.budget_state(RUN_A)["used"] == 2
    assert profiling.start_operation(operation="terminal.open", host="host-a") is None

    # A genuinely new experiment starts from a full allowance.
    fresh = _run(run_id=RUN_B, max_operations=2)
    profiling.policy_store().publish(_state(run=fresh, revision=2))

    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None
    assert profiling.budget_state(RUN_B)["used"] == 1


def test_budget_state_names_its_scope_and_counts_what_was_spent(monkeypatch):
    """A per-process count presented as a fleet cap would be a quota nobody enforces."""
    run = _run(run_id=RUN_A, max_operations=2)
    _admin(monkeypatch, _state(run=run, revision=1))

    untouched = profiling.budget_state(RUN_A)
    assert untouched["scope"] == "producer_instance"
    assert untouched["instance"] == profiling.PRODUCER_INSTANCE_ID
    assert untouched["max_operations"] is None
    assert untouched["used"] == 0
    assert untouched["exhausted"] is False

    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None
    spent = profiling.budget_state(RUN_A)
    assert spent["max_operations"] == 2
    assert spent["used"] == 1
    assert spent["exhausted"] is False

    assert profiling.start_operation(operation="terminal.open", host="host-a") is not None
    exhausted = profiling.budget_state(RUN_A)
    assert exhausted["used"] == 2
    assert exhausted["exhausted"] is True


def test_budget_state_for_an_unselected_run_invents_no_allowance(monkeypatch):
    """A run this process never selected has no local count to report."""
    _admin(monkeypatch, _state(run=_run(run_id=RUN_A), revision=1))

    state = profiling.budget_state(RUN_B)

    assert state["scope"] == "producer_instance"
    assert state["max_operations"] is None
    assert state["used"] == 0
    assert state["exhausted"] is False


def test_admits_answers_only_for_the_configuration_in_force(monkeypatch):
    """A broker's inherited selection is checked against the current configuration."""
    run = _run(run_id=RUN_A, operations=("terminal.open",))
    _admin(monkeypatch, _state(run=run, revision=1))
    store = profiling.policy_store()
    assert store.admits(RUN_A, operations=frozenset({"terminal.open"})) is True
    # An operation this run does not select is not collectable under it.
    assert store.admits(RUN_A, operations=frozenset({"files.open"})) is False
    # A selection from another experiment is refused rather than reported.
    assert store.admits(RUN_B, operations=frozenset({"terminal.open"})) is False
    profiling.policy_store().publish(_stopped(revision=2))
    assert store.admits(RUN_A, operations=frozenset({"terminal.open"})) is False


# ---------------------------------------------------------------------------
# A handed-over selection keeps the experiment that made it
# ---------------------------------------------------------------------------


def test_handoff_carries_the_manifest_and_the_child_inherits_it(monkeypatch):
    """A child must bind to the experiment that selected the attempt."""
    run = _run(
        run_id=RUN_A,
        operations=("attempt.startup",),
        target={"host": "host-a"},
        max_operations=7,
        config_version=3,
    )
    _admin(monkeypatch, _state(run=run, revision=5))

    measurement = profiling.start_operation(
        operation="attempt.startup", host="host-a", producer="runner"
    )
    assert measurement is not None
    assert profiling.bind(measurement, host="host-a", job_id="ajob_1") is True
    context = profiling.handoff_context(measurement)
    assert context is not None

    assert context["run_id"] == RUN_A
    assert context["run"]["run_id"] == RUN_A
    assert context["run"]["operations"] == ["attempt.startup"]
    assert context["run"]["target"] == {"host": "host-a"}
    assert context["run"]["config_version"] == 3
    assert context["run"]["max_operations"] == 7
    assert context["run"]["control_revision"] == 5
    assert context["run"]["expires_at"] == run.expires_at_utc.isoformat()

    child = profiling.attach_selection(context, producer="runner")
    assert child is not None
    assert child.run_id == RUN_A
    assert child.measurement_id == measurement.measurement_id
    assert child.run_operations == measurement.run_operations
    assert child.run_target == measurement.run_target
    assert child.run_source == measurement.run_source
    assert child.run_source == profiling.SOURCE_ADMIN
    assert child.run_config_version == 3
    assert child.run_expires_at == run.expires_at_utc
    assert child.run_admission_instances == measurement.run_admission_instances
    # The claim the parent already paid for is part of the startup being measured.
    assert child.started_mono_ns == measurement.started_mono_ns
    # The child did not take a slot, so discarding it must not return one.
    assert child.reserved is False
    child_manifest = profiling.manifest_for_measurement(child)
    assert child_manifest is not None and child_manifest["run_id"] == RUN_A


def test_a_child_continues_the_experiment_its_parent_selected(monkeypatch):
    """A late child keeps the *old* experiment rather than adopting the new one.

    The parent hands over an attempt it already proved; by the time the child
    starts, the deployment may have moved to another experiment. The child files
    the attempt under the one that selected it -- reporting it under the new
    experiment would attribute the sample to a run that never asked for it.
    """
    run_a = _run(run_id=RUN_A, operations=("attempt.startup",), target={"host": "host-a"})
    _admin(monkeypatch, _state(run=run_a, revision=1))
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    assert profiling.bind(measurement, host="host-a") is True
    context = profiling.handoff_context(measurement)
    assert context is not None

    # The deployment moves on before the child gets to run.
    profiling.policy_store().publish(
        _state(
            run=_run(run_id=RUN_B, operations=("attempt.startup",), target={"host": "host-b"}),
            revision=2,
        )
    )

    child = profiling.attach_selection(context)
    assert child is not None
    assert child.run_id == RUN_A
    assert child.run_target == {"host": "host-a"}
    assert child.measurement_id == measurement.measurement_id
    assert child.started_mono_ns == measurement.started_mono_ns
    # The child may not adopt the experiment that replaced its parent's.
    assert child.run_id != RUN_B


_CHILD_SCRIPT = r"""
import json, os, sys
from cloud_agent_profiling import operation_profiling

payload = json.loads(sys.stdin.read())
context = payload.get("_profiling")
# What the deployment actually does: a brand-new interpreter under Admin
# control, with no provider installed and no static descriptor. Its policy store
# is empty until its own sync loop fills it -- and it must not need it.
measurement = operation_profiling.attach_selection(context, producer="runner")
if measurement is None:
    print(json.dumps({"attached": False, "mode": operation_profiling.profile_mode()}))
    raise SystemExit(0)
operation_profiling.mark(measurement, "prompt_dispatch")
operation_profiling.finish(measurement, outcome="dispatched")
operation_profiling.flush_sink()
print(json.dumps({
    "attached": True,
    "mode": operation_profiling.profile_mode(),
    "run_id": measurement.run_id,
    "measurement_id": measurement.measurement_id,
    "started_mono_ns": measurement.started_mono_ns,
    "run_source": measurement.run_source,
    "operations": sorted(measurement.run_operations),
    "target": dict(measurement.run_target),
    "budget": operation_profiling.budget_state(measurement.run_id)["used"],
}))
"""


def test_a_real_child_process_continues_an_admin_selection(monkeypatch, tmp_path):
    """The deployment path runs the child in a new interpreter, so this does too.

    Nothing about the handoff may depend on the child having selected the
    experiment for itself: under Admin control a fresh process has an empty
    policy store, no static descriptor, and therefore no run of its own. An
    in-process test cannot see that, because it shares the parent's store --
    which is exactly how ``attach_selection`` came to require a local run and
    silently collect nothing in production.
    """
    import pathlib
    import subprocess
    import sys

    sink = tmp_path / "child-records.jsonl"
    run = _run(
        run_id=RUN_A,
        operations=("attempt.startup",),
        target={"host": "host-a"},
        max_operations=7,
        config_version=3,
    )
    _admin(monkeypatch, _state(run=run, revision=5))
    measurement = profiling.start_operation(
        operation="attempt.startup", host="host-a", producer="runner"
    )
    assert measurement is not None
    assert profiling.bind(measurement, host="host-a", job_id="ajob_1") is True
    context = profiling.handoff_context(measurement)
    assert context is not None

    # The child's environment: Admin mode, no legacy switch, no descriptor.
    env = {
        "PATH": os.environ.get("PATH", ""),
        # The package root the child has to import from, plus whatever this
        # process was given.
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [
                    str(pathlib.Path(profiling.__file__).resolve().parents[1]),
                    os.environ.get("PYTHONPATH", ""),
                ],
            )
        ),
        profiling.ENV_PROFILING_MODE: profiling.MODE_ADMIN,
        profiling.ENV_PROFILING_SINK: str(sink),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        input=json.dumps({"_profiling": context}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    answer = json.loads(completed.stdout.strip().splitlines()[-1])

    assert answer["mode"] == profiling.MODE_ADMIN
    assert answer["attached"] is True, completed.stdout + completed.stderr
    # The same measurement, continued rather than restarted.
    assert answer["measurement_id"] == measurement.measurement_id
    assert answer["started_mono_ns"] == measurement.started_mono_ns
    # The same experiment, with the parent's identity and no second slot spent.
    assert answer["run_id"] == RUN_A
    assert answer["run_source"] == profiling.SOURCE_ADMIN
    assert answer["operations"] == ["attempt.startup"]
    assert answer["target"] == {"host": "host-a"}
    assert answer["budget"] == 0
    # And the record it emitted is filed under that experiment.
    written = json.loads(sink.read_text().strip().splitlines()[-1])
    assert written["profiling_run_id"] == RUN_A
    assert written["measurement_id"] == measurement.measurement_id
    assert written["control_revision"] == 5

    # The same child process, told the deployment does not collect at all,
    # refuses the very same handoff.
    disabled_env = dict(env)
    disabled_env[profiling.ENV_PROFILING_MODE] = profiling.MODE_DISABLED
    refused = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT],
        input=json.dumps({"_profiling": context}),
        capture_output=True,
        text=True,
        env=disabled_env,
        timeout=60,
        check=False,
    )
    assert refused.returncode == 0, refused.stderr
    answer = json.loads(refused.stdout.strip().splitlines()[-1])
    assert answer == {"attached": False, "mode": profiling.MODE_DISABLED}
    # Nothing was written for it: a disabled deployment collects nothing.
    assert len(sink.read_text().strip().splitlines()) == 1


def test_an_expired_selection_is_refused_however_it_arrives(monkeypatch):
    """Continuing is not a way around the experiment's own deadline."""
    run = _run(run_id=RUN_A, operations=("attempt.startup",))
    _admin(monkeypatch, _state(run=run, revision=1))
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    context = profiling.handoff_context(measurement)
    assert context is not None

    expired = dict(context)
    expired["expires_at"] = time.time() - 1.0

    assert profiling.attach_selection(expired) is None
