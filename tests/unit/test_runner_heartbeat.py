"""Deterministic network outage tests for attempt lease renewal."""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from cloud_agent_host import runner


def test_brief_control_outage_recovers_before_the_original_lease_expires(monkeypatch):
    """Two timed-out reads must not add two full normal heartbeat sleeps."""
    clock = SimpleNamespace(now=0.0)
    attempts = []

    class Stop:
        def wait(self, delay):
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


def test_stopped_heartbeat_makes_no_request():
    control = Mock(job_id="ajob_stopped")
    heart = runner.Heartbeater(control, ttl=120, interval=30)
    heart.stop()
    heart._run()
    control.heartbeat.assert_not_called()
