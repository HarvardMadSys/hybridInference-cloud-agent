"""A member's meter records, taken out of its CLI's stderr and reported once per turn."""

from __future__ import annotations

from cloud_agent_host.protocol_translator import PrefixMeter
from cloud_agent_host.request_prefixes import (
    MAX_PENDING_RECORDS,
    PrefixRecords,
    TurnPrefixes,
)
from cloud_agent_shared.request_prefix import RECORD_MARKER, measure, record_line

_BODY = {"system": "be brief", "tools": [{"name": "Read"}], "messages": []}


def _whole(**changes) -> str:
    record = measure("anthropic_messages", {**_BODY, **changes}, mode="passthrough")
    return record_line(record).decode()


def test_records_leave_the_diagnostic_text_and_are_read_across_chunks():
    records = PrefixRecords()
    line = _whole()
    stream = "warning: one\n" + line + "error: two\n"
    pieces = [stream[i : i + 7] for i in range(0, len(stream), 7)]
    text = "".join(records.feed(piece) for piece in pieces)
    assert text == "warning: one\nerror: two\n"
    (record,) = records.take()
    assert record["system"]["chars"] == len("be brief")
    assert records.take() == []


def test_a_repeat_is_the_record_it_repeats_and_an_unknown_one_is_dropped():
    meter = PrefixMeter(mode="passthrough")
    records = PrefixRecords()
    records.feed(meter.line("anthropic_messages", _BODY).decode())
    records.feed(meter.line("anthropic_messages", _BODY).decode())
    first, again = records.take()
    assert first == again
    other = PrefixMeter(mode="passthrough")
    other.line("anthropic_messages", {**_BODY, "system": "x"})
    records.feed(other.line("anthropic_messages", {**_BODY, "system": "x"}).decode())
    assert records.take() == [] and records.dropped == 1


def test_text_that_only_looks_like_a_record_stays_diagnostic():
    records = PrefixRecords()
    assert records.feed("\x1emodel-request-prefix {broken\n") == ""
    assert records.dropped == 1
    assert (
        records.feed("mid-line " + RECORD_MARKER + "{}\n") == "mid-line " + RECORD_MARKER + "{}\n"
    )
    assert records.feed("\x1eother\n") == "\x1eother\n"
    assert records.take() == []


def test_waiting_records_are_bounded():
    records = PrefixRecords()
    for index in range(MAX_PENDING_RECORDS + 5):
        records.feed(_whole(system=str(index)))
    assert len(records.take()) == MAX_PENDING_RECORDS
    assert records.dropped == 5


def test_a_turn_reports_each_prefix_once():
    records = PrefixRecords()
    records.feed(_whole() + _whole() + _whole(system="sub-agent"))
    turn = TurnPrefixes()
    events = turn.events(records.take())
    assert [event.payload["phase"] for event in events] == ["request_prefix"] * 2
    assert events[0].event_type == "lifecycle"
    records.feed(_whole())
    assert turn.events(records.take()) == []
    # The next turn reports what its own requests sent, though the CLI sent it before.
    records.feed(_whole())
    assert len(TurnPrefixes().events(records.take())) == 1


def test_the_reported_event_type_is_the_runtimes_lifecycle():
    from cloud_agent_host import request_prefixes
    from cloud_agent_host.runtimes.events import LIFECYCLE

    assert request_prefixes.LIFECYCLE == LIFECYCLE
