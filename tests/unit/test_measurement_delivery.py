"""What a producer retries, what it gives up on, and what it drops.

These are the rules that decide whether a diagnostic can become an outage. A
record that is retried after a refusal loops forever on an answer that will not
change; a queue with no bound turns a slow destination into unbounded memory in
the process that is supposed to be measuring latency. Both are cheap to get
wrong and expensive to discover in production, so both are pinned here.
"""

from __future__ import annotations

import httpx
import pytest

from cloud_agent_host import measurement_delivery as delivery


@pytest.mark.parametrize("status", sorted(delivery.RETRYABLE_STATUS))
def test_a_destination_asking_for_time_is_retried(status: int):
    """A destination that is briefly unavailable has not judged the request."""
    outcome = delivery.classify_response(status)

    assert outcome.retryable is True
    assert outcome.delivered is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_a_refusal_is_not_retried(status: int):
    """Sending the same bytes again gets the same judgement, so it is terminal."""
    outcome = delivery.classify_response(status)

    assert outcome.retryable is False
    assert outcome.delivered is False
    assert outcome.result == "dropped"
    assert str(status) in outcome.reason


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_a_stored_record_is_delivered(status: int):
    """Any 2xx that reports storage is the destination agreeing with us."""
    outcome = delivery.classify_response(status, {"result": "stored"})

    assert outcome.delivered is True
    assert outcome.result == "delivered"


def test_a_duplicate_is_a_success_not_a_failure():
    """Idempotence working is not an error to report or to retry."""
    outcome = delivery.classify_response(200, {"result": "duplicate"})

    assert outcome.delivered is True
    assert outcome.result == "duplicate"
    assert outcome.retryable is False


def test_a_conflict_is_terminal_and_named():
    """The id names different contents; no retry can change that."""
    outcome = delivery.classify_response(200, {"result": "conflict"})

    assert outcome.result == "dropped"
    assert outcome.reason == "conflict"
    assert outcome.retryable is False


def test_a_conflict_status_is_terminal_too():
    """A 409 body that never arrives is still a conflict."""
    outcome = delivery.classify_response(409)

    assert outcome.result == "dropped"
    assert outcome.reason == "conflict"


def test_a_transport_failure_is_retried_and_keeps_its_error():
    """A connection that never happened says nothing about the request."""
    failure = httpx.ConnectError("connection refused")

    outcome = delivery.classify_exception(failure)

    assert outcome.retryable is True
    assert outcome.error is failure


def test_a_timeout_is_retried():
    """A read that ran out of time is a destination problem, not a bad record."""
    outcome = delivery.classify_exception(httpx.ReadTimeout("too slow"))

    assert outcome.retryable is True


def test_an_unexpected_failure_is_not_retried():
    """A serialization bug is not fixed by sending it again."""
    outcome = delivery.classify_exception(ValueError("cannot serialize"))

    assert outcome.retryable is False
    assert outcome.result == "dropped"
    assert isinstance(outcome.error, ValueError)


def test_attempts_are_bounded():
    """One record gets a stated number of tries and then is given up on."""
    assert delivery.attempts_exhausted(delivery.DELIVERY_ATTEMPTS_MAX) is True
    assert delivery.attempts_exhausted(delivery.DELIVERY_ATTEMPTS_MAX - 1) is False


# ---------------------------------------------------------------------------
# The queue bounds memory, not only count
# ---------------------------------------------------------------------------


def test_a_full_queue_drops_rather_than_blocks():
    """A diagnostic that can stall what it measures is worse than none."""
    queue = delivery.DeliveryQueue(max_items=2)

    assert queue.offer(b"a") is True
    assert queue.offer(b"b") is True
    assert queue.offer(b"c") is False
    assert len(queue) == 2
    assert queue.dropped() == {"items": 1, "bytes": 1}


def test_a_queue_bounded_by_count_is_also_bounded_by_bytes():
    """Two hundred small records and two hundred large ones are not alike."""
    queue = delivery.DeliveryQueue(max_items=1000, max_bytes=10)

    assert queue.offer(b"123456") is True
    assert queue.offer(b"12345") is False
    assert queue.pending_bytes == 6
    assert queue.dropped()["items"] == 1


def test_a_record_larger_than_the_whole_budget_is_refused_outright():
    """Keeping it would evict everything else and still not fit."""
    queue = delivery.DeliveryQueue(max_items=10, max_bytes=8)

    assert queue.offer(b"x" * 9) is False
    assert len(queue) == 0
    assert queue.pending_bytes == 0
    assert queue.dropped() == {"items": 1, "bytes": 9}


def test_taking_releases_the_bytes_it_held():
    """The byte budget is a budget, not a high-water mark."""
    queue = delivery.DeliveryQueue(max_items=2, max_bytes=10)
    queue.offer(b"1234")
    queue.offer(b"12345")

    queue.take()

    assert queue.pending_bytes == 5
    assert queue.offer(b"6789") is True


def test_taking_from_an_empty_queue_yields_nothing():
    """An empty queue is not an error to the drain loop."""
    queue = delivery.DeliveryQueue()

    assert queue.take() is None
    assert queue.dropped() == {"items": 0, "bytes": 0}
