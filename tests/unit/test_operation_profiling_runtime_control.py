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
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
    assert context["operations"] == ["attempt.startup"]
    assert context["target"] == {"host": "host-a"}
    assert context["config_version"] == 3
    assert context["max_operations"] == 7
    assert context["control_revision"] == 5
    assert context["run_expires_at"] == run.expires_at_utc.isoformat()

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


def test_attach_selection_refuses_an_attempt_from_the_previous_experiment(monkeypatch):
    """A child that re-read its own snapshot must not file an old attempt under it."""
    run_a = _run(run_id=RUN_A, operations=("attempt.startup",), target={"host": "host-a"})
    _admin(monkeypatch, _state(run=run_a, revision=1))
    measurement = profiling.start_operation(operation="attempt.startup", host="host-a")
    assert measurement is not None
    assert profiling.bind(measurement, host="host-a") is True
    context = profiling.handoff_context(measurement)
    assert context is not None
    assert profiling.attach_selection(context) is not None

    profiling.policy_store().publish(
        _state(
            run=_run(run_id=RUN_B, operations=("attempt.startup",), target={"host": "host-b"}),
            revision=2,
        )
    )

    assert profiling.attach_selection(context) is None
