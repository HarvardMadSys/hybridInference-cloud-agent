"""Why a turn could not borrow its chat's live CLI process, and what the record keeps.

A credential-less probe that the sandbox manager refuses turns a follow-up
into a ``resume_running`` startup. The refusal names the differing fields,
never their values, and the runner copies only that field list into the
startup measurement.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.unit.test_agent_runner import (  # noqa: F401 - the autouse pytest fixture
    _configure_host,
    _enable_profiling,
    _probe_then_replace,
    _profiled_measurement,
    _reset_profiling_cache,
    _run_shared_boundary_case,
)
from tests.unit.test_shared_session_supervisor import (
    _launch,
    _Manager,
    _members,
    _spec,
    _Transport,
    _TransportFactory,
)

from cloud_agent_host import resident_probe, runner as runner_mod
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransport
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.session_supervisor_client import SessionSupervisorError
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor
from cloud_agent_shared import operation_profiling


class _FieldNamingTransport(_Transport):
    """The supervisor fake, naming differing fields the way ResidentTransport does."""

    def incompatible_fields(self, launch: ResidentLaunch) -> tuple[str, ...]:
        real = ResidentTransport(launch=self.launch, session=SimpleNamespace())
        return real.incompatible_fields(launch)


class _FieldNamingFactory(_TransportFactory):
    def open(self, spec, launch: ResidentLaunch) -> _Transport:
        self.opened.append(launch)
        transport = _FieldNamingTransport(launch)
        self.transports.append(transport)
        return transport


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"model": "glm-5.2"}, "(differs: model)"),
        ({"residency_generation": 9}, "(differs: residency_generation)"),
    ],
)
def test_a_refused_probe_names_what_differs_without_its_value(change, reason):
    """A credential-less probe that cannot borrow the live process says why.

    The startup measurement records this reason, so it carries field names
    only: the model, keys and tokens a launch holds never reach the message.
    """
    supervisor = SharedChatSessionSupervisor(_Manager(), transport_factory=_FieldNamingFactory())
    first = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:1",
        activation_generation=1,
        has_settled_context=True,
        manifest=_members(),
    )
    launch = _launch("athr_source", native_session_id="native-source", slot=0)
    supervisor.ensure_resident("athr_source", first.token, launch)
    supervisor.settle("athr_source", first.token, job_id="ajob_source", context_status="live")
    lease = supervisor.acquire(
        _spec(),
        thread_id="athr_source",
        owner="attempt:2",
        activation_generation=2,
        has_settled_context=True,
        manifest=_members(),
    )

    with pytest.raises(SandboxError, match="requires a renewable credential") as refused:
        supervisor.ensure_resident(
            "athr_source", lease.token, replace(launch, credential=None, **change)
        )

    message = str(refused.value)
    assert message.endswith(reason)
    assert "glm-5.2" not in message
    assert "provider-key" not in message


@pytest.mark.parametrize(
    ("message", "differs"),
    [
        (
            "resident process restart requires a renewable credential "
            "(differs: model,residency_generation)",
            "model,residency_generation",
        ),
        # Free text that is not the field list stays out of the record.
        ("member resident transport is not live: secret-token-123", "other"),
    ],
)
def test_a_refused_probe_records_why_the_live_process_was_not_reused(
    monkeypatch, tmp_path, message, differs
):
    """A probe that keeps failing turns follow-ups into restarts; the record says why."""
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(control, supervisor, _finish):
        _probe_then_replace(control, supervisor, residency={})
        answer = supervisor.ensure_shared_resident.side_effect

        def refuse_first(*args, **kwargs):
            if supervisor.ensure_shared_resident.call_count == 1:
                raise SessionSupervisorError(503, message)
            return answer(*args, **kwargs)

        supervisor.ensure_shared_resident.side_effect = refuse_first

        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, _control, supervisor, _finish = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="resume",
    )

    assert result == 0
    assert supervisor.ensure_shared_resident.call_count == 2
    operation_profiling.flush_sink()
    record = json.loads(sink.read_text().splitlines()[0])
    facts = record["facts"]
    assert facts["startup_path"] == "resume_running"
    assert facts["resident_probe"] == "refused"
    assert facts["resident_probe_status"] == 503
    assert facts["resident_probe_differs"] == differs
    assert facts["resident_probe_us"] >= 0
    assert "secret-token-123" not in json.dumps(record)


def test_an_accepted_probe_is_recorded_as_accepted(monkeypatch, tmp_path):
    _configure_host(monkeypatch)
    sink = tmp_path / "startup.jsonl"
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(operation_profiling.ENV_PROFILING_SINK, str(sink))
    measurement = _profiled_measurement()
    assert measurement is not None

    def configure(_control, _supervisor, _finish):
        def stream(**kwargs):
            operation_profiling.finish(kwargs.get("measurement"), outcome="dispatched")
            return 0, "", [], {"kind": "result", "status": "completed", "exit_code": 0}, None

        monkeypatch.setattr(runner_mod, "_stream_shared_agent_turn", stream)

    result, *_ = _run_shared_boundary_case(
        monkeypatch,
        code=0,
        frame={"kind": "result", "status": "completed", "exit_code": 0},
        runtime_id="claude-code",
        configure=configure,
        measurement=measurement,
        container_action="reuse",
        resident_action="reuse",
    )

    assert result == 0
    operation_profiling.flush_sink()
    facts = json.loads(sink.read_text().splitlines()[0])["facts"]
    assert facts["resident_probe"] == "accepted"
    assert "resident_probe_differs" not in facts


def test_probe_lease_state_keeps_to_a_known_vocabulary():
    assert resident_probe.probe_lease_state("restarting") == "restarting"
    assert resident_probe.probe_lease_state(None) == "absent"
    assert resident_probe.probe_lease_state("x" * 50) == "other"
