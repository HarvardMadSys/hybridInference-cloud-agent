"""The host's sandbox incident observer: what becomes a row, and when it is sent.

Each property is one way an incident record could mislead an operator, the
user or the agent:

- a stop the platform asked for must not read as a crash;
- a crash loop must not flood the table, and nothing may be counted by
  changing a row already sent (the table is append-only);
- one member's MCP failure must reach that member only, even after its slot
  is reused;
- a row is held until the control plane confirms it, and sent again until then.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_agent_host.observe.resources import Sample
from cloud_agent_host.observe.sandbox_resources import DockerResourceReading
from cloud_agent_host.sandbox_incidents import (
    BROWSER_JOURNAL,
    DRAIN_SCRIPT,
    HOURLY_CAP,
    MAX_SEEN,
    OUTBOX_MAX,
    IncidentOutbox,
    MemberCredentials,
    SandboxIncidents,
    slot_journal,
)
from cloud_agent_host.service import incident_relay
from cloud_agent_shared.sandbox_incidents import INCIDENTS_ACCEPTED, parse_incidents

REPO = Path(__file__).resolve().parents[2]
CONTAINER = "c" * 64


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _kinds(outbox: IncidentOutbox, sandbox: str = "sb") -> list[str]:
    return [row["kind"] for row in outbox.pending(sandbox)]


# -- the outbox: final rows only ------------------------------------------------


def test_the_first_occurrence_goes_out_at_once_and_repeats_as_one_summary() -> None:
    clock = Clock()
    outbox = IncidentOutbox(clock=clock)
    for n in range(4):
        outbox.observe("sb", "out_of_memory", incident_id=f"oom-{n}", observed_at=clock.now + n)
    assert _kinds(outbox) == ["out_of_memory"]  # told now, not after the window

    clock.now += 61
    outbox.tick()
    rows = outbox.pending("sb")
    assert [row["kind"] for row in rows] == ["out_of_memory", "out_of_memory.repeated"]
    summary = rows[1]
    assert summary["incident_id"] == "oom-0:repeated"
    assert summary["count"] == 3
    assert summary["details"]["window_end"] == clock.now - 61 + 3
    assert all(incident.is_valid() for incident in parse_incidents(rows))


def test_a_window_with_no_repeat_sends_nothing_more() -> None:
    clock = Clock()
    outbox = IncidentOutbox(clock=clock)
    outbox.observe("sb", "browser_crashed", incident_id="b-1", observed_at=clock.now)
    clock.now += 120
    outbox.tick()
    assert _kinds(outbox) == ["browser_crashed"]


def test_past_the_hourly_cap_rows_are_counted_into_one_dropped_row() -> None:
    clock = Clock(3600.0 * 500_000)
    outbox = IncidentOutbox(clock=clock)
    for n in range(HOURLY_CAP + 7):
        # Distinct subjects, so none of them is a repeat of another.
        outbox.observe(
            "sb", "memory_high", incident_id=f"m-{n}", observed_at=clock.now + n, subject=str(n)
        )
    assert len(outbox.pending("sb")) == min(HOURLY_CAP, OUTBOX_MAX)

    clock.now += 3600
    outbox.tick()
    dropped = [row for row in outbox.pending("sb") if row["kind"] == "dropped"]
    assert len(dropped) == 1 and dropped[0]["count"] >= 7
    assert dropped[0]["incident_id"] == f"dropped:{500_000}"


def test_a_confirmed_row_is_forgotten_and_an_unconfirmed_one_is_sent_again() -> None:
    outbox = IncidentOutbox(clock=Clock())
    outbox.observe("sb", "out_of_memory", incident_id="a", observed_at=1.0)
    outbox.observe("sb", "container_exited", incident_id="b", observed_at=2.0)
    assert len(outbox.pending("sb")) == 2
    assert len(outbox.pending("sb")) == 2  # listing is not sending
    outbox.confirm("sb", ["a"])
    assert [row["incident_id"] for row in outbox.pending("sb")] == ["b"]
    outbox.forget(set())
    assert outbox.pending("sb") == []


def test_a_member_incident_with_no_member_is_not_recorded() -> None:
    outbox = IncidentOutbox(clock=Clock())
    outbox.observe("sb", "mcp_server_failed", incident_id="x", observed_at=1.0, thread_id=None)
    assert outbox.pending("sb") == []


# -- Docker events -----------------------------------------------------------------


#: Members' credential digests, as the slot's admission names them.
A, B, OLD, NEW = ("a" * 64, "b" * 64, "0" * 64, "1" * 64)


def _members(*admitted: tuple[str, str, int]) -> MemberCredentials:
    members = MemberCredentials()
    for digest, thread_id, slot in admitted:
        members.admitted("sb", digest, thread_id=thread_id, slot=slot)
    return members


def _observer(read=lambda _sandbox, _request: None, members=None) -> SandboxIncidents:
    members = members or _members((A, "thread-a", 0), (B, "thread-b", 1))
    incidents = SandboxIncidents(read_journals=read, clock=Clock(), members=members)
    incidents.sync(
        [{"sandbox_id": "sb", "container_id": CONTAINER, "running": True}],
        {"sb": {0: "thread-a", 1: "thread-b"}},
    )
    return incidents


def _event(action: str, at: int, **attributes: str) -> dict:
    return {
        "Type": "container",
        "Action": action,
        "id": CONTAINER,
        "Actor": {"ID": CONTAINER, "Attributes": attributes},
        "timeNano": at * 1_000_000_000,
    }


def test_a_stop_the_platform_asked_for_is_not_an_incident() -> None:
    incidents = _observer()
    incidents.handle_event(_event("kill", 100, signal="15"))
    incidents.handle_event(_event("die", 105, exitCode="143"))
    assert incidents.pending("sb") == []


def test_an_exit_nobody_asked_for_and_an_oom_are_incidents() -> None:
    incidents = _observer()
    incidents.handle_event(_event("oom", 100))
    incidents.handle_event(_event("die", 101, exitCode="137"))
    rows = incidents.pending("sb")
    assert [row["kind"] for row in rows] == ["out_of_memory", "container_exited"]
    assert rows[1]["details"]["exit_code"] == 137
    assert rows[0]["incident_id"].startswith("docker:cccccccccccc:")


def test_another_containers_events_are_ignored() -> None:
    incidents = _observer()
    incidents.handle_event({**_event("oom", 100), "id": "d" * 64, "Actor": {"ID": "d" * 64}})
    assert incidents.pending("sb") == []


# -- replays are counted once ---------------------------------------------------------


def test_a_replayed_docker_event_is_not_a_repeat() -> None:
    """Review P2: Docker replays from ``since`` after a reconnect; one OOM, not two."""
    incidents = _observer()
    for _ in range(2):
        incidents.handle_event(_event("oom", 100))
    incidents.outbox._clock.now += 120  # the window closes
    incidents.outbox.tick()
    assert [row["kind"] for row in incidents.pending("sb")] == ["out_of_memory"]


def test_a_replay_after_its_row_was_confirmed_is_still_the_same_observation() -> None:
    incidents = _observer()
    incidents.handle_event(_event("oom", 100))
    [row] = incidents.pending("sb")
    incidents.confirm({"sb": [row["incident_id"]]})
    incidents.handle_event(_event("oom", 100))
    incidents.outbox._clock.now += 3600
    incidents.outbox.tick()
    assert incidents.pending("sb") == []


def test_two_different_events_in_a_window_are_one_row_and_a_repeat() -> None:
    incidents = _observer()
    incidents.handle_event(_event("oom", 100))
    incidents.handle_event(_event("oom", 130))
    incidents.handle_event(_event("oom", 130))  # that one's replay
    incidents.outbox._clock.now += 3600
    incidents.outbox.tick()
    rows = incidents.pending("sb")
    assert [(row["kind"], row["count"]) for row in rows] == [
        ("out_of_memory", 1),
        ("out_of_memory.repeated", 1),
    ]


def test_what_was_seen_is_bounded() -> None:
    outbox = IncidentOutbox(clock=Clock())
    for n in range(MAX_SEEN + 10):
        outbox.observe("sb", "memory_high", incident_id=f"m-{n}", observed_at=1.0, subject=str(n))
    assert len(outbox._seen) == MAX_SEEN
    outbox.forget(set())
    assert not outbox._seen


# -- whose MCP failure it was ------------------------------------------------------------


def _mcp(member: str, seq: int) -> str:
    return _line("slot0", seq, "mcp_server_failed", member=member, alias="docs", phase="crashed")


def test_a_failure_stays_with_its_member_after_the_slot_is_reused() -> None:
    """Review P2: old-thread failed, new-thread took slot 0, and the drain blamed new-thread."""
    members = _members((OLD, "old-thread", 0))
    reply = {slot_journal(0): [_mcp(OLD, 1)]}
    incidents = _observer(lambda *_: json.dumps(reply).encode(), members=members)
    # old-thread leaves; new-thread takes the slot before the journal is read.
    members.admitted("sb", NEW, thread_id="new-thread", slot=0)
    incidents.sync(
        [{"sandbox_id": "sb", "container_id": CONTAINER, "running": True}],
        {"sb": {0: "new-thread"}},
    )
    incidents.drain("sb")
    assert [row["thread_id"] for row in incidents.pending("sb")] == ["old-thread"]


def test_a_stopped_members_journal_is_still_read_and_still_its_own() -> None:
    members = _members((OLD, "old-thread", 3))
    requests: list[dict] = []

    def read(_sandbox: str, request: bytes) -> bytes:
        requests.append(json.loads(request))
        return json.dumps({slot_journal(3): [_mcp(OLD, 1)]}).encode()

    incidents = _observer(read, members=members)
    incidents.sync([{"sandbox_id": "sb", "container_id": CONTAINER, "running": True}], {"sb": {}})
    incidents.drain("sb")
    assert slot_journal(3) in requests[0]["journals"]  # no member holds slot 3 now
    assert [row["thread_id"] for row in incidents.pending("sb")] == ["old-thread"]


def test_failures_in_one_slot_go_to_whichever_member_each_was_served_to() -> None:
    members = _members((OLD, "old-thread", 0), (NEW, "new-thread", 0))
    reply = {slot_journal(0): [_mcp(OLD, 1), _mcp(NEW, 2)]}
    incidents = _observer(lambda *_: json.dumps(reply).encode(), members=members)
    incidents.drain("sb")
    assert [row["thread_id"] for row in incidents.pending("sb")] == ["old-thread", "new-thread"]


def test_the_member_registry_is_bounded_and_keeps_the_newest() -> None:
    members = MemberCredentials(limit=2)
    for n in range(3):
        members.admitted("sb", str(n), thread_id=f"t{n}", slot=n)
    assert members.thread("sb", "0") is None
    assert (members.thread("sb", "2"), members.slots("sb")) == ("t2", {1, 2})
    assert members.thread("other", "2") is None


# -- peaks ---------------------------------------------------------------------------


def _reading(memory: int, pids: int) -> DockerResourceReading:
    return DockerResourceReading(
        counters=Sample(mono=0.0, mem_bytes=memory),
        memory_limit_bytes=1000,
        pids=pids,
        pids_limit=100,
    )


def test_a_reading_near_a_limit_is_one_incident_until_it_falls_back() -> None:
    incidents = _observer()
    for memory in (950, 990, 850):  # stays above the re-arm line
        incidents.observe_reading(CONTAINER, _reading(memory, 10))
    assert [row["kind"] for row in incidents.pending("sb")] == ["memory_high"]
    assert incidents.pending("sb")[0]["details"] == {"peak": 950, "limit": 1000}

    incidents.observe_reading(CONTAINER, _reading(100, 95))  # re-arms memory; pids high
    assert [row["kind"] for row in incidents.pending("sb")] == ["memory_high", "pids_high"]


# -- journals ------------------------------------------------------------------------


def _line(journal: str, seq: int, kind: str, **fields) -> str:
    return json.dumps(
        {"journal": journal, "seq": seq, "ts": 1_800_000_000.0 + seq, "kind": kind, **fields}
    )


def test_journals_are_read_from_their_cursor_and_owned_by_the_member_served() -> None:
    requests: list[dict] = []
    replies = [
        {
            BROWSER_JOURNAL: [_line("j1", 1, "browser_crashed", exit_code=None, signal=11)],
            slot_journal(1): [
                _line("k1", 1, "mcp_server_failed", member=B, alias="docs", phase="start")
            ],
        },
        {BROWSER_JOURNAL: [_line("j1", 1, "browser_crashed"), _line("j1", 2, "browser_crashed")]},
    ]

    def read(sandbox_id: str, request: bytes) -> bytes:
        requests.append(json.loads(request))
        return json.dumps(replies[len(requests) - 1]).encode()

    incidents = _observer(read)
    assert incidents.drain("sb") == 2
    rows = incidents.pending("sb")
    assert [row["kind"] for row in rows] == ["browser_crashed", "mcp_server_failed"]
    assert rows[0]["scope"] == "sandbox" and rows[0]["details"]["signal"] == 11
    member = rows[1]
    assert (member["scope"], member["thread_id"]) == ("member", "thread-b")
    assert member["details"]["alias"] == "docs" and member["details"]["phase"] == "start"

    # The second read starts from what the first returned, and a line already
    # seen is not an incident again.
    assert incidents.drain("sb") == 1
    assert requests[1]["journals"][BROWSER_JOURNAL] == ["j1", 1]
    assert set(requests[1]["journals"]) == {BROWSER_JOURNAL, slot_journal(0), slot_journal(1)}


def test_an_unknown_or_malformed_journal_line_is_ignored() -> None:
    reply = {
        BROWSER_JOURNAL: ["not json", _line("j", 1, "something_else")],
        slot_journal(0): [
            _line("k", 1, "mcp_server_failed", member=A, alias="docs", phase="install-ish"),
            # A line without the member it happened to is nobody's.
            _line("k", 2, "mcp_server_failed", alias="docs", phase="start"),
            # Nor one whose credential this host never admitted, or forgot.
            _line("k", 3, "mcp_server_failed", member="f" * 64, alias="docs", phase="start"),
        ],
        "/etc/passwd": [_line("z", 1, "browser_crashed")],
    }
    incidents = _observer(lambda *_: json.dumps(reply).encode())
    incidents.drain("sb")
    assert incidents.pending("sb") == []


def test_the_drain_script_reads_only_lines_past_each_cursor(tmp_path: Path) -> None:
    browser, slot = tmp_path / "browser.jsonl", tmp_path / "slot.jsonl"
    browser.write_text("\n".join([_line("j", 1, "a"), _line("j", 2, "b"), "garbage"]) + "\n")
    slot.write_text(_line("k", 5, "c") + "\n")
    request = {
        "journals": {str(browser): ["j", 1], str(slot): ["other", 9], str(tmp_path / "x"): None}
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", DRAIN_SCRIPT],
        input=json.dumps(request).encode(),
        capture_output=True,
        check=True,
    )
    out = json.loads(result.stdout)
    assert [json.loads(line)["seq"] for line in out[str(browser)]] == [2]
    assert [json.loads(line)["seq"] for line in out[str(slot)]] == [5]  # a new journal: all of it
    assert str(tmp_path / "x") not in out


def test_only_members_holding_a_slot_are_mapped() -> None:
    incidents = SandboxIncidents(read_journals=lambda *_: None, clock=Clock())
    rows = [{"sandbox_id": "sb", "container_id": CONTAINER, "running": True}]
    groups = {
        "sb": [
            {"thread_id": "live", "resident_slot_id": 0, "resident_state": "live"},
            {"thread_id": "stopped", "resident_slot_id": 0, "resident_state": "stopped"},
        ]
    }
    incidents.annotate(rows, groups, complete=True)
    assert incidents._targets["sb"].slots == {0: "live"}
    assert rows[0]["incidents"] == []


# -- the runner's relay ----------------------------------------------------------------


class _Client:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def confirm_incidents(self, confirmed: dict) -> None:
        self.calls.append(confirmed)
        if self.fail:
            raise RuntimeError("sandbox manager away")


@pytest.fixture(autouse=True)
def _fresh_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incident_relay, "_carried", {})
    monkeypatch.setattr(incident_relay, "_confirmed", {})


def test_rows_are_confirmed_to_the_sandbox_manager_only_on_the_answers_word() -> None:
    row = {"sandbox_id": "sb", "incidents": [{"incident_id": "a"}, "junk"]}
    assert incident_relay.carry(row) == [{"incident_id": "a"}]
    incident_relay.settle(None)  # an older control plane, or a store failure
    client = _Client()
    incident_relay.confirm(client)
    assert client.calls == []

    incident_relay.carry(row)
    incident_relay.settle(INCIDENTS_ACCEPTED)
    incident_relay.confirm(client)
    assert client.calls == [{"sb": ["a"]}]


def test_a_failed_confirmation_is_tried_again() -> None:
    incident_relay.carry({"sandbox_id": "sb", "incidents": [{"incident_id": "a"}]})
    incident_relay.settle(INCIDENTS_ACCEPTED)
    failing = _Client(fail=True)
    incident_relay.confirm(failing)
    working = _Client()
    incident_relay.confirm(working)
    assert working.calls == [{"sb": ["a"]}]
