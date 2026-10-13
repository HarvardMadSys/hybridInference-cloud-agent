"""Deterministic network outage tests for attempt lease renewal."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from cloud_agent_host import runner


def test_brief_control_outage_recovers_before_the_original_lease_expires(monkeypatch):
    """Two timed-out reads must not add two full normal heartbeat sleeps."""
    clock = SimpleNamespace(now=0.0)
    attempts = []

    class Stop:
        def wait(self, delay):
            # The runner stops renewal once the cancelled turn has ended.
            if heart.cancel_requested:
                return True
            clock.now += delay
            assert clock.now < 200, "heartbeat loop did not stop"
            return False

    class Control:
        job_id = "ajob_outage"

        def heartbeat(self, ttl):
            attempts.append(clock.now)
            if clock.now >= 120:
                raise runner.LeaseLost("expired")
            if clock.now < 100:
                clock.now += 30
                raise httpx.ReadTimeout("temporary outage")
            return {"cancel_requested": True}

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.now)
    heart = runner.Heartbeater(Control(), ttl=120, interval=30)
    heart._stop = Stop()
    heart._run()

    assert heart.cancel_requested
    assert not heart.lease_lost
    assert attempts[-1] < 120


def test_heartbeat_recovery_restores_normal_cadence_and_logs_no_credentials(monkeypatch, caplog):
    """Error diagnostics contain identity, type and status, never request secrets."""
    clock = SimpleNamespace(now=0.0)
    waits = []
    control = Mock(job_id="ajob_redaction")
    request = httpx.Request("POST", "https://example.test/?token=PRIVATE_TOKEN")
    response = httpx.Response(503, request=request, text="PRIVATE_BODY")
    control.heartbeat.side_effect = [
        httpx.HTTPStatusError("PRIVATE_MESSAGE", request=request, response=response),
        {},
        {"cancel_requested": True},
    ]

    class Stop:
        def wait(self, delay):
            # The runner stops renewal once the cancelled turn has ended.
            if heart.cancel_requested:
                return True
            waits.append(delay)
            clock.now += delay
            assert len(waits) <= 3
            return False

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.now)
    caplog.set_level("INFO", logger=runner.__name__)
    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart._stop = Stop()
    heart._run()

    assert waits == [30, 5, 30]
    assert "status_code=503" in caplog.text
    assert "agent_heartbeat_recovered" in caplog.text
    assert "ajob_redaction" in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_repeated_heartbeat_failures_record_the_detail_once(monkeypatch, caplog):
    """A sustained outage logs one exception detail, not one per retry."""
    clock = SimpleNamespace(now=0.0)
    waits = []
    control = Mock(job_id="ajob_outage")
    control.heartbeat.side_effect = [
        httpx.ReadTimeout("first"),
        httpx.ReadTimeout("second"),
        {"cancel_requested": True},
    ]

    class Stop:
        def wait(self, delay):
            # The runner stops renewal once the cancelled turn has ended.
            if heart.cancel_requested:
                return True
            waits.append(delay)
            clock.now += delay
            assert len(waits) <= 3
            return False

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.now)
    caplog.set_level("INFO", logger=runner.__name__)
    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart._stop = Stop()
    heart._run()

    assert waits == [30, 5, 5]
    assert caplog.text.count("agent_control_failure") == 1
    assert caplog.text.count("agent_heartbeat_retry") == 2
    assert "consecutive_failures=2" in caplog.text


def test_fenced_heartbeat_never_retries_or_claims_a_replacement(caplog):
    """A 409 terminates renewal even when fast retries are enabled."""
    control = Mock(job_id="ajob_fenced")
    control.heartbeat.side_effect = runner.LeaseLost("PRIVATE_RESPONSE")
    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart._stop = Mock()
    heart._stop.wait.return_value = False

    heart._run()

    control.heartbeat.assert_called_once_with(120)
    assert heart.lease_lost
    assert not heart.cancel_requested
    assert "PRIVATE_RESPONSE" not in caplog.text


def test_cancellation_keeps_the_lease_renewed_until_the_runner_stops():
    """A Stop is surfaced at once, but renewal continues while the runner cleans up.

    The interrupted turn drains for up to three minutes and a startup that saw
    the Stop still has to release its activation. Ending renewal at the first
    cancelled beat let a 120-second lease expire under either, and the reaper
    then marked the chat's context lost.
    """
    control = Mock(job_id="ajob_cancelled")
    control.heartbeat.return_value = {"cancel_requested": True}
    beats = []

    class Stop:
        def wait(self, delay):
            beats.append(heart.cancel_requested)
            return len(beats) > 4

    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart._stop = Stop()
    heart._run()

    assert control.heartbeat.call_count == 4
    assert beats == [False, True, True, True, True]
    assert heart.cancel_requested and not heart.lease_lost


def test_an_outage_longer_than_the_lease_stops_the_agent(monkeypatch, caplog):
    """No acknowledged renewal for a whole lease means the lease is gone.

    The reaper supersedes such an attempt and marks its member lost, but the
    heartbeat used to retry every five seconds for as long as the partition
    lasted, so a silent agent kept writing to the shared workspace until a 409
    finally got through.
    """
    clock = SimpleNamespace(now=0.0)
    attempts = []

    class Stop:
        def wait(self, delay):
            clock.now += delay
            assert clock.now < 300, "heartbeat never gave the lease up"
            return False

    class Control:
        job_id = "ajob_partitioned"

        def heartbeat(self, ttl):
            attempts.append(clock.now)
            clock.now += 10
            raise httpx.ConnectTimeout("partitioned")

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.now)
    heart = runner.Heartbeater(Control(), ttl=120, interval=30)
    heart._stop = Stop()
    heart._run()

    assert heart.lease_lost and not heart.cancel_requested
    # Every renewal that could still have landed inside the lease was tried.
    assert attempts == [30, 45, 60, 75, 90, 105, 120]
    assert clock.now == 130
    assert "operation=heartbeat_expired" in caplog.text


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_refused_worker_credential_stops_the_agent_at_once(status):
    """The token never changes during an attempt, so a refusal is final."""
    request = httpx.Request("POST", "https://control.test/v1/agent/worker/jobs/a/heartbeat")
    response = httpx.Response(status, request=request)
    control = Mock(job_id="ajob_refused")
    control.heartbeat.side_effect = httpx.HTTPStatusError(
        "refused", request=request, response=response
    )
    waits = []

    class Stop:
        def wait(self, delay):
            waits.append(delay)
            assert len(waits) <= 2, "a refused credential was retried"
            return False

    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart._stop = Stop()

    heart._run()

    control.heartbeat.assert_called_once_with(120)
    assert heart.lease_lost


@pytest.mark.parametrize("configured", ["5", "30", "40"])
def test_a_lease_no_renewal_could_reach_is_not_used(monkeypatch, capsys, configured):
    """A lease that ends before its first renewal would lose every attempt."""
    monkeypatch.setenv("AGENT_LEASE_TTL", configured)

    assert runner.build_parser().parse_args([]).lease_ttl == runner.DEFAULT_LEASE_TTL_S
    assert "ignoring AGENT_LEASE_TTL" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(["--lease-ttl", configured])


def test_a_lease_that_outlasts_its_first_renewal_is_used(monkeypatch):
    monkeypatch.setenv("AGENT_LEASE_TTL", "45")
    assert runner.build_parser().parse_args([]).lease_ttl == 45.0
    assert runner.build_parser().parse_args(["--lease-ttl", "300"]).lease_ttl == 300.0


def test_stopped_heartbeat_makes_no_request():
    control = Mock(job_id="ajob_stopped")
    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart.stop()
    heart._run()
    control.heartbeat.assert_not_called()


@pytest.mark.parametrize("network_recovers_at", [0, 100, 105])
def test_silent_turn_completes_across_three_lease_periods(monkeypatch, network_recovers_at):
    """A silent agent stream does not gate background renewal or successful finish.

    Exercise the real heartbeat thread, stream loop and HTTP client against a
    simulated lease endpoint. Advance virtual time instead of sleeping six
    minutes; the two-second event waits are only deadlock watchdogs.
    """
    clock = SimpleNamespace(now=0.0, expires_at=120.0)
    stream_started = threading.Event()
    renewal_done = threading.Event()
    stopped = threading.Event()
    requests = []
    dispatch_events = []

    class Stop:
        def set(self):
            stopped.set()

        def wait(self, delay):
            if stopped.is_set():
                return True
            assert stream_started.wait(2), "agent stream did not start"
            clock.now += delay
            if clock.now >= 360:
                clock.now = 360
                return True
            return False

    def endpoint(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation == "events":
            assert clock.now == 0
            dispatch_events.append(json.loads(request.content))
            return httpx.Response(200, json={})
        requests.append((operation, clock.now))
        if clock.now >= clock.expires_at:
            return httpx.Response(409, json={"detail": "lease expired"})
        if operation == "heartbeat":
            if clock.now < network_recovers_at:
                # Model the configured timeout, not the client's unrelated
                # default for events and other control requests.
                clock.now += request.extensions["timeout"]["read"]
                raise httpx.ReadTimeout("temporary outage", request=request)
            clock.expires_at = clock.now + 120
            return httpx.Response(200, json={"cancel_requested": False})
        assert operation == "finish"
        assert json.loads(request.content)["state"] == "succeeded"
        return httpx.Response(200, json={})

    class Stream:
        closed = False

        def frames(self):
            stream_started.set()
            assert renewal_done.wait(2), "heartbeat thread did not finish"
            # No message or progress events during the entire six-minute turn.
            yield {"kind": "result", "status": "completed", "exit_code": 0}

        def close(self):
            self.closed = True

    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.now)
    control = runner.ControlPlane("https://control.test", "test-token")
    control.close()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    control.job_id = "ajob_silent"
    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart._stop = Stop()
    run_heartbeat = heart._run

    def renew_until_turn_ends():
        try:
            run_heartbeat()
        finally:
            renewal_done.set()

    monkeypatch.setattr(heart, "_run", renew_until_turn_ends)
    stream = Stream()
    supervisor = Mock()
    supervisor.start_shared_turn.return_value = stream
    job = SimpleNamespace(
        thread_id="athr_silent", task_prompt="wait", model="m", reasoning_effort=None
    )
    heart.start()
    try:
        code, _tail, errors, result, _reported = runner._stream_shared_agent_turn(
            supervisor=supervisor,
            job=job,
            control=control,
            heart=heart,
            lease_token="lease",
            timeout_s=600,
        )
        assert code == 0 and not errors
        assert result["status"] == "completed"
        assert clock.now == 360
        assert not heart.lease_lost and not heart.cancel_requested
        assert clock.expires_at > clock.now
        assert dispatch_events == [
            {"event_type": "lifecycle", "payload": {"phase": "turn_started"}, "event_seq": 1}
        ]
        control.finish("succeeded")
        assert requests[-1] == ("finish", 360)
        assert all(operation in {"heartbeat", "finish"} for operation, _ in requests)
        supervisor.interrupt_shared.assert_not_called()
        assert stream.closed
    finally:
        heart.stop()
        control.close()


def test_heartbeat_has_a_short_timeout_without_changing_other_control_requests():
    timeouts = {}

    def endpoint(request):
        timeouts[request.url.path.rsplit("/", 1)[-1]] = request.extensions["timeout"]
        return httpx.Response(200, json={})

    control = runner.ControlPlane("https://control.test", "test-token")
    control.close()
    control._client = httpx.Client(timeout=30, transport=httpx.MockTransport(endpoint))
    control.job_id = "ajob_timeouts"
    try:
        control.heartbeat(120)
        control.finish("succeeded")
    finally:
        control.close()

    assert timeouts["heartbeat"] == dict.fromkeys(("connect", "read", "write", "pool"), 10)
    assert timeouts["finish"] == dict.fromkeys(("connect", "read", "write", "pool"), 30)
