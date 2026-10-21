"""Loss-aware Kata guest process observation and tool attribution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec
from cloud_agent_host.guest_observer import (
    GUEST_BOOT_ID_PATH,
    GUEST_OBSERVER_BINARY,
    GUEST_OBSERVER_EVENTS,
    GUEST_OBSERVER_ROTATE_BYTES,
    GUEST_OBSERVER_SOURCE,
    GUEST_OBSERVER_STATUS,
    GUEST_OBSERVER_VERSION,
    MAX_OBSERVED_TOOL_MARKS,
    GuestObserverController,
    GuestObserverCursor,
    GuestObserverProtocolError,
    GuestObserverRuntimeError,
    GuestObserverSnapshot,
    GuestObserverStatus,
    add_process_resources,
    attribute_processes,
    finish_observation,
    parse_drain,
    parse_snapshot,
    parse_status,
)
from cloud_agent_host.guest_observer_locks import GuestObserverLocks
from cloud_agent_host.sandbox import KATA_RUNTIME, ContainerBackend
from cloud_agent_host.tool_windows import TOOL_MARK_HELPER, ToolMark, ToolWindow

if TYPE_CHECKING:
    from collections.abc import Iterator


def _cursor(**overrides) -> GuestObserverCursor:
    values = {
        "uid": 11003,
        "offset": 0,
        "lost": 4,
        "journal_dropped": 2,
        "poll_seq": 10,
        "guest_mono_ns": 1_000_000_000,
        "mono_offset_ns": 10_000_000_000,
        "clock_error_ns": 0,
        "available": True,
    }
    values.update(overrides)
    return GuestObserverCursor(**values)


def _snapshot(**overrides) -> GuestObserverSnapshot:
    status = GuestObserverStatus(
        pid=91,
        poll_seq=11,
        event_seq=9,
        lost=4,
        journal_dropped=2,
    )
    return GuestObserverSnapshot(
        status=overrides.pop("status", status),
        journal_size=overrides.pop("journal_size", 100),
        guest_mono_ns=overrides.pop("guest_mono_ns", 2_000_000_000),
        **overrides,
    )


def _event(seq: int, kind: str, mono_ns: int, **overrides) -> bytes:
    value = {
        "version": GUEST_OBSERVER_VERSION,
        "seq": seq,
        "kind": kind,
        "mono_ns": mono_ns,
        "uid": 11003,
        "pid": 42,
        "tgid": 42,
        "ppid": 7,
        "comm": "tiny",
        "filename": "/bin/tiny",
        "exit_code": 0,
    }
    value.update(overrides)
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _publication(snapshot: GuestObserverSnapshot) -> dict[str, object]:
    """One status publication, as the fixed guest scripts print it."""
    status = snapshot.status
    return {
        "status": {
            "version": GUEST_OBSERVER_VERSION,
            "ready": True,
            "pid": status.pid,
            "poll_seq": status.poll_seq,
            "event_seq": status.event_seq,
            "lost": status.lost,
            "journal_dropped": status.journal_dropped,
            "journal_bytes": status.journal_bytes,
            "journal_dev": status.journal_dev,
            "journal_ino": status.journal_ino,
        },
        "journal_size": snapshot.journal_size,
        "guest_mono_ns": snapshot.guest_mono_ns,
    }


def _drained(
    closing: GuestObserverSnapshot,
    flushed: GuestObserverSnapshot,
    payload: bytes = b"",
) -> SimpleNamespace:
    """The drain script's output: both publications on one line, then the slice."""
    header = json.dumps(
        {"closing": _publication(closing), "flushed": _publication(flushed)},
        separators=(",", ":"),
    )
    return SimpleNamespace(returncode=0, stdout=header.encode() + b"\n" + payload, stderr=b"")


class _LocalObserver:
    """The trusted helper's published state, kept on this machine.

    The fixed guest scripts run unmodified against it, with only the three
    paths they are given rewritten: a sleeping interpreter stands in for the
    helper process, so ``/proc/<pid>/exe`` is the binary they are told to
    trust, and a thread republishes the status every 10 ms, the way the
    helper does after each poll.
    """

    def __init__(self, root: Path) -> None:
        self.status_path = root / "status.json"
        self.events_path = root / "events.jsonl"
        self.events_path.write_bytes(b"")
        self.trusted_binary = sys.executable
        self.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.poll_seq = 1
        self.event_seq = 0
        #: Clear it and the helper stops publishing, as if its poll stalled.
        self.polling = threading.Event()
        self.polling.set()
        self.execs: list[list[str]] = []
        self.outputs: list[bytes] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._publish()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    @property
    def pid(self) -> int:
        return self.process.pid

    def append(self, *records: bytes) -> None:
        """Journal records now; the helper's next poll publishes them."""
        with self._lock, self.events_path.open("ab") as handle:
            handle.write(b"".join(records))
            self.event_seq += len(records)

    def _publish(self) -> None:
        with self._lock:
            journal = self.events_path.stat()
            status = {
                "version": GUEST_OBSERVER_VERSION,
                "ready": True,
                "pid": self.pid,
                "poll_seq": self.poll_seq,
                "event_seq": self.event_seq,
                "lost": 0,
                "journal_dropped": 0,
                "journal_bytes": journal.st_size,
                "journal_dev": journal.st_dev,
                "journal_ino": journal.st_ino,
            }
            temporary = self.status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(status), encoding="utf-8")
            os.replace(temporary, self.status_path)

    def _poll(self) -> None:
        while not self._stop.wait(0.01):
            if self.polling.is_set():
                self.poll_seq += 1
                self._publish()

    def capture(self, argv: list[str], timeout_s: float) -> subprocess.CompletedProcess[bytes]:
        """The exec seam: run the fixed script here, against this state."""
        self.execs.append(list(argv))
        paths = {
            GUEST_OBSERVER_STATUS: str(self.status_path),
            GUEST_OBSERVER_EVENTS: str(self.events_path),
            GUEST_OBSERVER_BINARY: self.trusted_binary,
        }
        command = [paths.get(item, item) for item in argv]
        if command[0] == "python3":
            command[0] = sys.executable
        result = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
        self.outputs.append(result.stdout)
        return result

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.process.kill()
        self.process.wait(timeout=5)


@pytest.fixture
def local_observer(tmp_path: Path) -> Iterator[_LocalObserver]:
    observer = _LocalObserver(tmp_path)
    try:
        yield observer
    finally:
        observer.close()


def _never_restarted(*_args: object, **_kwargs: object) -> None:
    pytest.fail("a live observer must not be restarted")


def test_snapshot_contract_requires_ready_versioned_monotonic_state():
    payload = json.dumps(
        {
            "status": {
                "version": GUEST_OBSERVER_VERSION,
                "ready": True,
                "pid": 9,
                "poll_seq": 12,
                "event_seq": 2,
                "lost": 0,
                "journal_dropped": 0,
                "journal_bytes": 81,
                "journal_dev": 7,
                "journal_ino": 19,
            },
            "journal_size": 81,
            "guest_mono_ns": 123456,
        }
    ).encode()

    snapshot = parse_snapshot(payload)

    assert snapshot.status.pid == 9
    assert snapshot.journal_size == 81
    assert snapshot.guest_mono_ns == 123456


def test_snapshot_refuses_a_status_that_could_make_no_events_look_complete():
    with pytest.raises(GuestObserverProtocolError, match="not ready"):
        parse_snapshot(
            json.dumps(
                {
                    "status": {
                        "version": GUEST_OBSERVER_VERSION,
                        "ready": False,
                        "pid": 9,
                        "poll_seq": 1,
                        "event_seq": 0,
                        "lost": 0,
                        "journal_dropped": 0,
                        "journal_bytes": 0,
                        "journal_dev": 7,
                        "journal_ino": 19,
                    },
                    "journal_size": 0,
                    "guest_mono_ns": 1,
                }
            ).encode()
        )


def test_idle_rotation_failure_never_rejects_the_next_turn(monkeypatch):
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: None,
    )
    monkeypatch.setattr(
        controller,
        "snapshot",
        lambda **_kwargs: _snapshot(journal_size=GUEST_OBSERVER_ROTATE_BYTES),
    )

    def fail_start():
        raise RuntimeError("guest restart failed")

    monkeypatch.setattr(controller, "start", fail_start)

    assert controller.rotate_if_needed() is False


def test_a_replaced_journal_cannot_look_like_the_same_observer_after_pid_reuse():
    cursor = _cursor(observer_pid=91, journal_dev=7, journal_ino=19)
    replaced = _snapshot(
        status=GuestObserverStatus(
            pid=91,
            poll_seq=11,
            event_seq=20,
            lost=4,
            journal_dropped=2,
            journal_bytes=100,
            journal_dev=7,
            journal_ino=20,
        )
    )
    controller = GuestObserverController(
        capture=lambda *_args: _drained(replaced, replaced),
        privileged=lambda *_args: None,
    )

    observation = controller.finish(cursor)

    assert observation.complete is False
    assert "journal changed" in (observation.detail or "")


def test_regressed_loss_counters_cannot_look_like_a_complete_observation():
    regressed = _snapshot(
        status=GuestObserverStatus(
            pid=91,
            poll_seq=11,
            event_seq=9,
            lost=3,
            journal_dropped=2,
            journal_bytes=100,
            journal_dev=7,
            journal_ino=19,
        )
    )
    flushed = replace(regressed, status=replace(regressed.status, poll_seq=13))
    # The slice is read in the same exec as the fence, so it arrives with it.
    read_alongside = _event(10, "fork", 1_001_000_000) + _event(11, "exit", 1_002_000_000)
    controller = GuestObserverController(
        capture=lambda *_args: _drained(regressed, flushed, read_alongside),
        privileged=lambda *_args: None,
    )

    observation = controller.finish(_cursor(observer_pid=91, journal_dev=7, journal_ino=19))

    assert observation.complete is False
    assert "loss counters reset" in (observation.detail or "")
    # A reset fence is rejected before a byte read with it is parsed.
    assert observation.processes == ()
    assert observation.journal_payload == b""


def test_a_journal_replaced_after_the_final_snapshot_is_refused(local_observer):
    """The fd opened for the read must still be the cursor's exact journal."""
    local_observer.append(_event(1, "fork", 1_001_000_000))
    controller = GuestObserverController(
        capture=local_observer.capture,
        privileged=_never_restarted,
    )
    # The helper publishes the journal at its path; the cursor was fenced on
    # another file, the one this read must find there.
    cursor = _cursor(observer_pid=local_observer.pid, journal_dev=7, journal_ino=19)

    observation = controller.finish(cursor)

    assert observation.complete is False
    assert "journal changed before read" in (observation.detail or "")


def test_begin_crosses_a_complete_poll_before_publishing_its_fence(monkeypatch):
    opening = _snapshot(
        status=GuestObserverStatus(91, 20, 8, 4, 2, 100, 7, 19),
        journal_size=100,
        guest_mono_ns=1_900_000_000,
    )
    flushed = _snapshot(
        status=GuestObserverStatus(91, 22, 9, 5, 2, 180, 7, 19),
        journal_size=180,
        guest_mono_ns=2_000_000_000,
    )
    waits: list[int | None] = []
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: None,
    )
    monkeypatch.setattr(controller, "ensure", lambda: None)

    def snapshot(*, after_poll_seq=None, **_kwargs):
        waits.append(after_poll_seq)
        return opening if after_poll_seq is None else flushed

    monkeypatch.setattr(controller, "snapshot", snapshot)

    cursor = controller.begin(user_uid=11003)

    assert waits == [None, 21]
    assert cursor.offset == 180
    assert cursor.lost == 5
    assert cursor.event_seq == 9
    assert cursor.guest_mono_ns == 2_000_000_000


def test_active_shared_observations_never_restart_on_a_new_snapshot_failure(monkeypatch):
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: pytest.fail("an active sibling forbids observer restart"),
    )
    monkeypatch.setattr(
        controller,
        "ensure",
        lambda: pytest.fail("restart recovery must stay disabled while a cursor is active"),
    )

    def unavailable_snapshot(**_kwargs):
        raise GuestObserverRuntimeError("transient docker exec failure")

    monkeypatch.setattr(controller, "snapshot", unavailable_snapshot)

    cursor = controller.begin(user_uid=11004, restart_if_missing=False)

    assert cursor.available is False
    assert "transient docker exec failure" in (cursor.detail or "")


def test_a_warm_begin_is_one_opening_read_and_one_bracketed_fence(local_observer):
    """Liveness, trusted binary, journal size, opening fence and boot: one read."""
    controller = GuestObserverController(
        capture=local_observer.capture,
        privileged=_never_restarted,
    )

    cursor = controller.begin(user_uid=11003, rotate_threshold=GUEST_OBSERVER_ROTATE_BYTES)

    assert cursor.available, cursor.detail
    assert len(local_observer.execs) == 2
    opening = parse_snapshot(local_observer.outputs[0])
    assert opening.guest_boot_id == Path(GUEST_BOOT_ID_PATH).read_text().strip()
    assert cursor.guest_boot_id == opening.guest_boot_id
    # The fence still crosses a whole poll after the opening read, and only
    # that read is bracketed by the host clock: it reads nothing else.
    assert cursor.poll_seq > opening.status.poll_seq + 1
    assert local_observer.execs[1][-3:] == [
        GUEST_OBSERVER_BINARY,
        str(opening.status.poll_seq + 1),
        "3.0",
    ]


def test_finish_drains_a_full_poll_and_reads_the_slice_in_one_exec(local_observer):
    controller = GuestObserverController(
        capture=local_observer.capture,
        privileged=_never_restarted,
    )
    cursor = controller.begin(user_uid=11003)
    assert cursor.available, cursor.detail
    started = time.monotonic_ns()
    local_observer.append(
        _event(1, "fork", started + 1),
        _event(2, "exit", started + 2, counters=16, group_dead=True),
    )
    local_observer.execs.clear()
    local_observer.outputs.clear()

    observation = controller.finish(cursor)

    assert len(local_observer.execs) == 1
    assert observation.complete is True, observation.detail
    assert [process.pid for process in observation.processes] == [42]
    closing, flushed, journal = parse_drain(local_observer.outputs[0])
    # Two publications after the closing one: the first may already have been
    # in flight when it was read; the second closes that tail.
    assert flushed.status.poll_seq > closing.status.poll_seq + 1
    assert journal == observation.journal_payload


def test_the_drain_never_reads_before_a_full_poll_is_published(local_observer):
    controller = GuestObserverController(
        capture=local_observer.capture,
        privileged=_never_restarted,
    )
    cursor = controller.begin(user_uid=11003)
    assert cursor.available, cursor.detail
    local_observer.polling.clear()

    observation = controller.finish(cursor, wait_s=0.3)

    assert observation.complete is False
    assert "did not advance" in (observation.detail or "")


def test_every_observer_read_requires_the_trusted_binary(local_observer, tmp_path):
    controller = GuestObserverController(
        capture=local_observer.capture,
        privileged=_never_restarted,
    )
    cursor = controller.begin(user_uid=11003)
    assert cursor.available, cursor.detail
    impostor = tmp_path / "freeinference-guest-observer"
    impostor.write_bytes(b"")
    local_observer.trusted_binary = str(impostor)

    refused_begin = controller.begin(user_uid=11004, restart_if_missing=False)
    refused_finish = controller.finish(cursor)

    assert refused_begin.available is False
    assert refused_finish.complete is False
    for detail in (refused_begin.detail, refused_finish.detail):
        assert "does not name the trusted binary" in (detail or "")


def test_an_idle_rotation_is_decided_on_the_opening_read(monkeypatch):
    full = _snapshot(
        status=GuestObserverStatus(91, 20, 8, 4, 2, GUEST_OBSERVER_ROTATE_BYTES, 7, 19),
        journal_size=GUEST_OBSERVER_ROTATE_BYTES,
    )
    replacement = _snapshot(status=GuestObserverStatus(92, 1, 0, 0, 0, 0, 7, 20), journal_size=0)
    flushed = _snapshot(status=GuestObserverStatus(92, 3, 0, 0, 0, 0, 7, 20), journal_size=0)
    reads: list[tuple[int | None, bool]] = []
    starts: list[str] = []
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=lambda *_args: None,
    )

    def snapshot(*, after_poll_seq=None, wait_s=3.0, boot_id=False):
        reads.append((after_poll_seq, boot_id))
        if after_poll_seq is not None:
            return flushed
        return replacement if starts else full

    monkeypatch.setattr(controller, "snapshot", snapshot)
    monkeypatch.setattr(controller, "start", lambda: starts.append("start"))

    cursor = controller.begin(user_uid=11003, rotate_threshold=GUEST_OBSERVER_ROTATE_BYTES)

    assert starts == ["start"]
    # The opening read decided it, without a size read of its own; the fence
    # is then read again from the replacement before crossing its next poll.
    assert reads == [(None, True), (None, True), (2, False)]
    assert (cursor.observer_pid, cursor.journal_ino) == (92, 20)


def test_an_open_cursor_forbids_rotation_even_past_the_threshold(monkeypatch):
    full = _snapshot(
        status=GuestObserverStatus(91, 20, 8, 4, 2, GUEST_OBSERVER_ROTATE_BYTES, 7, 19),
        journal_size=GUEST_OBSERVER_ROTATE_BYTES,
    )
    flushed = _snapshot(
        status=GuestObserverStatus(91, 22, 8, 4, 2, GUEST_OBSERVER_ROTATE_BYTES, 7, 19),
        journal_size=GUEST_OBSERVER_ROTATE_BYTES,
    )
    controller = GuestObserverController(
        capture=lambda *_args: None,
        privileged=_never_restarted,
    )
    monkeypatch.setattr(
        controller,
        "snapshot",
        lambda *, after_poll_seq=None, **_kwargs: full if after_poll_seq is None else flushed,
    )
    monkeypatch.setattr(controller, "start", lambda: pytest.fail("a sibling's cursor is open"))

    cursor = controller.begin(
        user_uid=11004,
        restart_if_missing=False,
        rotate_threshold=GUEST_OBSERVER_ROTATE_BYTES,
    )

    assert cursor.available is True
    assert cursor.observer_pid == 91


def test_a_kata_turn_execs_into_the_guest_three_times(local_observer, monkeypatch):
    """Two execs before the prompt is written, one after the turn, no others."""
    manager = ChatSandboxManager(ContainerBackend(image="registry/agent:1", runtime=KATA_RUNTIME))
    monkeypatch.setattr(
        manager,
        "exec_capture",
        lambda _spec, argv, **kwargs: local_observer.capture(argv, kwargs["timeout_s"]),
    )
    monkeypatch.setattr(manager, "_observer_privileged_exec", _never_restarted)
    spec = ChatSandboxSpec(
        thread_id="athr_turns",
        sandbox_id="asbx_turns",
        generation=1,
        runtime_id="claude-code",
    )

    for turn in range(2):
        local_observer.execs.clear()
        cursor = manager.begin_guest_observation(
            spec, user_uid=11003, measurement_id=f"window-{turn}"
        )
        before_prompt = len(local_observer.execs)
        started = time.monotonic_ns()
        seq = local_observer.event_seq
        local_observer.append(
            _event(seq + 1, "fork", started + 1),
            _event(seq + 2, "exit", started + 2, counters=16, group_dead=True),
        )
        observation = manager.finish_guest_observation(spec, cursor)

        assert (before_prompt, len(local_observer.execs) - before_prompt) == (2, 1)
        assert observation.complete is True, observation.detail
        assert [process.pid for process in observation.processes] == [42]
        assert cursor.guest_boot_id


def test_a_containers_observer_lock_is_kept_only_while_it_is_needed():
    locks = GuestObserverLocks()
    cursor = GuestObserverCursor(uid=11003, observer_pid=91, available=True)

    with locks.container("chat-a") as opened:
        opened.admit(cursor)
        opened.admit(GuestObserverCursor.unavailable(11004, "observer unavailable"))
    with locks.container("chat-a") as reused, locks.container("chat-b") as other:
        # An open cursor keeps its container's lock and bookkeeping; another
        # container has a lock of its own.
        assert reused is opened
        assert reused.active == {id(cursor): 91}
        assert other.lock is not reused.lock
        reused.release(cursor)
    with locks.container("chat-a") as fresh:
        assert fresh is not opened


def test_late_journal_delivery_cannot_charge_a_pre_turn_process():
    payload = b"".join(
        [
            _event(1, "fork", 999_000_000, pid=41, tgid=41),
            _event(2, "exit", 999_500_000, pid=41, tgid=41),
            _event(3, "fork", 1_001_000_000),
            _event(4, "exit", 1_002_000_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    assert [process.pid for process in observation.processes] == [42]


def test_an_event_at_or_before_the_cursor_sequence_makes_the_slice_partial():
    observation = finish_observation(
        cursor=_cursor(event_seq=5),
        snapshot=_snapshot(),
        payload=_event(5, "fork", 1_001_000_000),
        truncated=False,
    )

    assert observation.complete is False
    assert observation.truncated is True
    assert observation.processes == ()


def test_a_sub_millisecond_process_survives_without_any_sampler_reading():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, filename=""),
            _event(2, "exec", 1_001_100_000),
            # The kernel's live count reached zero: this exit ended the process.
            _event(3, "exit", 1_001_700_000, counters=16, group_dead=True),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.source == GUEST_OBSERVER_SOURCE
    assert observation.complete is True
    assert observation.lost_events == 0
    (process,) = observation.processes
    assert process.executable == "/bin/tiny"
    assert process.observed_fork and process.observed_exec and process.observed_exit
    assert process.duration_ms == 1
    assert process.exit_code == 0


def test_member_uid_is_the_attribution_fence_for_concurrent_siblings():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, uid=11004, pid=51, tgid=51),
            _event(2, "exit", 1_002_000_000, uid=11004, pid=51, tgid=51),
            _event(3, "fork", 1_003_000_000),
            _event(4, "exit", 1_004_000_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert [process.pid for process in observation.processes] == [42]


def test_authenticated_guest_marks_share_the_uid_and_monotonic_fence():
    payload = b"".join(
        [
            _event(1, "tool_pre", 1_001_000_000, pid=50, tool_use_id="toolu_1"),
            _event(
                2,
                "tool_post",
                1_003_000_000,
                uid=11004,
                pid=51,
                tool_use_id="sibling",
            ),
            _event(3, "tool_post", 1_004_000_000, pid=50, tool_use_id="toolu_1"),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.tool_marks == (
        ToolMark(11.001, "pre", "toolu_1", guest_mono_ns=1_001_000_000),
        ToolMark(11.004, "post", "toolu_1", guest_mono_ns=1_004_000_000),
    )


def test_tool_mark_overflow_cannot_downgrade_a_complete_process_census():
    marks = b"".join(
        _event(
            seq,
            "tool_pre",
            1_000_000_000 + seq,
            pid=50,
            tool_use_id=f"toolu_{seq}",
        )
        for seq in range(1, MAX_OBSERVED_TOOL_MARKS + 2)
    )
    process_seq = MAX_OBSERVED_TOOL_MARKS + 2
    payload = marks + _event(process_seq, "fork", 1_001_000_000)

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    assert len(observation.tool_marks) == MAX_OBSERVED_TOOL_MARKS
    assert [process.pid for process in observation.processes] == [42]


@pytest.mark.parametrize(
    ("status", "truncated", "expected_lost"),
    [
        (GuestObserverStatus(91, 11, 9, 7, 2), False, 3),
        (GuestObserverStatus(91, 11, 9, 4, 5), False, 3),
        (GuestObserverStatus(91, 11, 9, 4, 2), True, 0),
    ],
)
def test_loss_or_a_bounded_read_never_claims_a_complete_census(
    status: GuestObserverStatus,
    truncated: bool,
    expected_lost: int,
):
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(status=status),
        payload=b"",
        truncated=truncated,
    )

    assert observation.complete is False
    assert observation.lost_events == expected_lost


def test_process_start_is_attributed_even_when_it_fits_between_sampler_ticks():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "fork", 1_001_000_000) + _event(2, "exit", 1_002_000_000),
        truncated=False,
    )
    windows = [
        ToolWindow(
            tool_use_id="toolu_1",
            tool="Bash",
            opened_mono=11.000,
            closed_mono=11.010,
        )
    ]

    rows, counts = attribute_processes(observation, windows)

    assert counts == {1: 1}
    assert rows[0]["tool_call_seqs"] == [1]
    assert rows[0]["started_offset_ms"] == 1


def test_tool_marker_instrumentation_is_not_counted_as_user_workload():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, pid=41, tgid=41, filename=""),
            _event(
                2,
                "exec",
                1_001_100_000,
                pid=41,
                tgid=41,
                comm="freeinference-t",
                filename=TOOL_MARK_HELPER,
            ),
            _event(3, "exit", 1_001_500_000, pid=41, tgid=41),
            _event(4, "fork", 1_002_000_000, pid=42, tgid=42, filename=""),
            _event(5, "exec", 1_002_100_000, pid=42, tgid=42),
            _event(6, "exit", 1_002_600_000, pid=42, tgid=42),
        ]
    )
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )
    windows = [ToolWindow("one", "Bash", opened_mono=11.000, closed_mono=11.010)]

    rows, counts = attribute_processes(observation, windows)

    assert [row["pid"] for row in rows] == [41, 42]
    assert rows[0]["executable"] == TOOL_MARK_HELPER
    assert rows[0]["tool_call_seqs"] == []
    assert counts == {1: 1}


def test_overlapping_tool_windows_keep_ambiguity_instead_of_picking_one():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "fork", 1_005_000_000),
        truncated=False,
    )
    windows = [
        ToolWindow("one", "Bash", opened_mono=11.000, closed_mono=11.010),
        ToolWindow("two", "Bash", opened_mono=11.003, closed_mono=11.020),
    ]

    rows, counts = attribute_processes(observation, windows)

    assert rows[0]["tool_call_seqs"] == [1, 2]
    assert counts == {1: 1, 2: 1}


def test_an_exact_window_is_compared_on_the_guest_clock_without_the_error_band():
    """Hook marks and process events share the guest clock; only a
    message-level bracket needs the bridge and its error."""
    observation = finish_observation(
        cursor=_cursor(clock_error_ns=100_000_000),
        snapshot=_snapshot(),
        payload=_event(1, "fork", 1_050_000_000),
        truncated=False,
    )
    exact = ToolWindow(
        "one",
        "Read",
        opened_mono=11.060,
        closed_mono=11.080,
        source="exact",
        opened_guest_ns=1_060_000_000,
        closed_guest_ns=1_080_000_000,
    )
    bracket = ToolWindow("two", "Read", opened_mono=11.060, closed_mono=11.080)

    rows, counts = attribute_processes(observation, [exact, bracket])

    # 10 ms before the exact window is outside it, full stop. The same 10 ms
    # is inside the 100 ms error band a message-level window must allow.
    assert rows[0]["tool_call_seqs"] == [2]
    assert counts == {1: 0, 2: 1}


def test_a_dropped_tool_window_prevents_false_process_ownership():
    """An omitted call may overlap every retained call, so all links are unknown."""
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "fork", 1_005_000_000),
        truncated=False,
    )
    windows = [
        ToolWindow("kept", "Bash", opened_mono=11.000, closed_mono=11.010),
    ]

    rows, counts = attribute_processes(
        observation,
        windows,
        windows_complete=False,
    )

    assert rows[0]["tool_call_seqs"] == []
    assert counts == {1: 0}


def test_an_exit_only_baseline_process_is_not_manufactured_into_turn_usage():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=_event(1, "exit", 1_005_000_000),
        truncated=False,
    )

    assert observation.complete is True
    assert observation.processes == ()


# Records as the loader actually writes them, captured from a run on a Linux
# 6.8 kernel: ``dd`` wrote 8 MiB, ``cat`` read it back from the page cache.
# The read shows up in ``rchar`` and not in ``read_bytes``; the write shows up
# in both. That asymmetry is the kernel's accounting, not ours. The exit
# record carries the group's peak and the I/O plus whether the exit emptied
# the group (bits 2|4|8|16); CPU arrives on the ``done`` record (bit 1), from
# the scheduler's runtime, once the task has left the CPU for the last time.
_DD_EXIT = {
    "counters": 30,
    "group_dead": True,
    "maxrss_bytes": 2_228_224,
    "read_bytes": 0,
    "write_bytes": 8_388_608,
    "rchar": 8_390_683,
    "wchar": 8_388_701,
}
_DD_DONE = {"counters": 1, "cpu_ns": 15_000_000}


def test_exit_and_done_records_carry_what_the_process_consumed():
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000, filename=""),
            _event(2, "exec", 1_001_100_000, comm="dd", filename="/usr/bin/dd"),
            _event(3, "exit", 1_001_700_000, comm="dd", **_DD_EXIT),
            _event(4, "done", 1_001_700_100, comm="dd", **_DD_DONE),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    (process,) = observation.processes
    assert process.observed_exit is True
    assert process.duration_ms == 1, "the process ended at its exit, not its final deschedule"
    assert process.cpu_usec == 15_000
    assert process.mem_peak_bytes == 2_228_224
    assert process.io_read_bytes == 0
    assert process.io_write_bytes == 8_388_608
    assert process.io_rchar_bytes == 8_390_683
    assert process.io_wchar_bytes == 8_388_701


def test_tick_based_cpu_on_an_exit_record_is_never_used():
    """Only the scheduler runtime on ``done`` counts; a stray exit CPU is dropped."""
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000),
            _event(2, "exit", 1_001_700_000, **{**_DD_EXIT, "counters": 31, "cpu_ns": 0}),
            _event(3, "done", 1_001_700_100, **_DD_DONE),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    (process,) = observation.processes
    assert process.cpu_usec == 15_000


def test_thread_exits_fold_into_their_leader_even_after_it_exited():
    """Each task reports its own share; the group's row is their sum.

    The kernel settles the peak on whichever thread empties the group, so
    the leader's own exit can carry a zero there while a later thread's
    carries the real value. The row must end up with the sum of the CPU and
    the maximum of the peak, whatever order the records arrived in.
    """
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000),
            # A thread being created is not a process start.
            _event(2, "fork", 1_001_050_000, pid=43, tgid=42),
            _event(3, "exit", 1_001_500_000, counters=18, group_dead=False, maxrss_bytes=0),
            _event(4, "done", 1_001_500_100, counters=1, cpu_ns=4_000_000),
            _event(
                5,
                "exit",
                1_001_600_000,
                pid=43,
                tgid=42,
                counters=18,
                group_dead=True,
                maxrss_bytes=4_096_000,
            ),
            _event(6, "done", 1_001_600_100, pid=43, tgid=42, counters=1, cpu_ns=6_000_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    (process,) = observation.processes
    assert process.pid == 42
    assert process.observed_exit is True
    assert process.exited_guest_mono_ns == 1_001_600_000, "the process ended with its last thread"
    assert process.cpu_usec == 10_000
    assert process.mem_peak_bytes == 4_096_000
    # Bits 4 and 8 were clear: this kernel accounted no I/O, which is not
    # the same as a process that did none.
    assert process.io_read_bytes is None
    assert process.io_write_bytes is None
    assert process.io_rchar_bytes is None


def test_a_leader_that_exited_before_its_threads_is_still_running():
    """Linux lets a leader exit while its threads live on; that is not the end."""
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000),
            _event(2, "fork", 1_001_050_000, pid=43, tgid=42),
            _event(3, "exit", 1_001_500_000, counters=18, group_dead=False, maxrss_bytes=1_024),
            _event(4, "done", 1_001_500_100, counters=1, cpu_ns=4_000_000),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is True
    (process,) = observation.processes
    assert process.observed_exit is False
    assert process.duration_ms is None
    assert process.cpu_usec is None
    assert process.mem_peak_bytes is None


def test_counters_wait_for_every_task_s_final_deschedule():
    """A dead group whose ``done`` never arrived reports no consumption."""
    payload = b"".join(
        [
            _event(1, "fork", 1_001_000_000),
            _event(2, "exit", 1_001_700_000, **_DD_EXIT),
        ]
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    (process,) = observation.processes
    assert process.observed_exit is True
    assert process.cpu_usec is None
    assert process.io_write_bytes is None
    rows, _counts = attribute_processes(
        observation,
        [ToolWindow("one", "Bash", opened_mono=11.000, closed_mono=11.010)],
    )
    (call,) = add_process_resources([{"call_seq": 1, "process_count": 1}], rows)
    assert call["process_cpu_usec"] is None
    assert call["process_resources_partial"] is True


def test_a_process_that_outlived_the_turn_reports_no_consumption():
    payload = _event(1, "fork", 1_001_000_000) + _event(
        2,
        "exit",
        1_001_200_000,
        pid=43,
        tgid=42,
        counters=18,
        group_dead=False,
        maxrss_bytes=4_096_000,
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    (process,) = observation.processes
    assert process.observed_exit is False
    # A thread's share arrived, but the group is still running: a partial
    # sum shown as the process's consumption would read as the whole.
    assert process.cpu_usec is None
    assert process.mem_peak_bytes is None


def test_a_malformed_counter_leaves_the_row_open_and_the_slice_partial():
    payload = _event(1, "fork", 1_001_000_000) + _event(
        2,
        "exit",
        1_001_700_000,
        counters=18,
        group_dead=True,
        maxrss_bytes="lots",
    )

    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=payload,
        truncated=False,
    )

    assert observation.complete is False
    (process,) = observation.processes
    assert process.observed_exit is False
    assert process.mem_peak_bytes is None


def test_process_rows_carry_the_counters_onto_the_wire():
    observation = finish_observation(
        cursor=_cursor(),
        snapshot=_snapshot(),
        payload=b"".join(
            [
                _event(1, "fork", 1_001_000_000),
                _event(2, "exit", 1_002_000_000, **_DD_EXIT),
                _event(3, "done", 1_002_000_100, **_DD_DONE),
            ]
        ),
        truncated=False,
    )

    rows, _counts = attribute_processes(observation, [])

    assert rows[0]["cpu_usec"] == 15_000
    assert rows[0]["mem_peak_bytes"] == 2_228_224
    assert rows[0]["io_write_bytes"] == 8_388_608
    assert rows[0]["io_rchar_bytes"] == 8_390_683


def _process_row(seqs: list[int], **overrides) -> dict:
    row = {
        "tool_call_seqs": seqs,
        "observed_exit": True,
        "cpu_usec": 1_000,
        "mem_peak_bytes": 1_310_720,
        "io_read_bytes": 0,
        "io_write_bytes": 0,
    }
    row.update(overrides)
    return row


def test_a_call_is_charged_only_with_processes_it_owns_outright():
    calls = [
        {"call_seq": 1, "process_count": 3},
        {"call_seq": 2, "process_count": 1},
        {"call_seq": 3, "process_count": 0},
    ]
    processes = [
        _process_row([1], cpu_usec=15_000, mem_peak_bytes=2_228_224, io_write_bytes=8_388_608),
        _process_row([1]),
        # Overlapping windows: plausibly either call, so charged to neither.
        _process_row([1, 2], cpu_usec=99_000, mem_peak_bytes=99_000_000),
        # The marker helper, or anything else no window claimed.
        _process_row([], cpu_usec=5),
    ]

    first, second, third = add_process_resources(calls, processes)

    assert first["process_cpu_usec"] == 16_000
    assert first["process_mem_peak_bytes"] == 2_228_224, "peaks are a maximum, not a sum"
    assert first["process_io_write_bytes"] == 8_388_608
    assert first["process_io_read_bytes"] == 0
    assert first["process_resources_partial"] is True
    assert second["process_cpu_usec"] is None
    assert second["process_resources_partial"] is True
    assert third["process_cpu_usec"] is None
    assert third["process_resources_partial"] is False


def test_a_process_still_running_makes_its_call_a_floor():
    (call,) = add_process_resources(
        [{"call_seq": 1, "process_count": 2}],
        [
            _process_row([1], cpu_usec=2_000),
            _process_row([1], observed_exit=False, cpu_usec=None, mem_peak_bytes=None),
        ],
    )

    assert call["process_cpu_usec"] == 2_000
    assert call["process_resources_partial"] is True


def test_a_counter_this_kernel_lacks_makes_the_total_unavailable_not_smaller():
    (call,) = add_process_resources(
        [{"call_seq": 1, "process_count": 2}],
        [
            _process_row([1], cpu_usec=2_000, io_read_bytes=None, io_write_bytes=None),
            _process_row([1], cpu_usec=3_000, io_read_bytes=None, io_write_bytes=None),
        ],
    )

    assert call["process_cpu_usec"] == 5_000
    assert call["process_io_read_bytes"] is None
    assert call["process_io_write_bytes"] is None
    assert call["process_resources_partial"] is False


def test_a_lossy_census_makes_every_charged_call_a_floor():
    """Lost events may hide a process the call started; no total is exact then."""
    with_processes, without = add_process_resources(
        [{"call_seq": 1, "process_count": 1}, {"call_seq": 2, "process_count": 0}],
        [_process_row([1], cpu_usec=2_000)],
        observation_complete=False,
    )

    assert with_processes["process_cpu_usec"] == 2_000
    assert with_processes["process_resources_partial"] is True
    assert without["process_cpu_usec"] is None
    assert without["process_resources_partial"] is False


class TestTheVersionHandshake:
    """The number the guest stamps and the number this host accepts.

    ``parse_status`` refuses a status object whose ``version`` is not
    ``GUEST_OBSERVER_VERSION``, and the journal reader refuses every record the
    same way. So the constant in ``deploy/sandbox/guest-observer.h`` and the one
    here are not two settings -- they are one handshake written twice, and a
    guest that publishes a number this host does not accept is a host that can
    never open an observation at all.

    That failure cannot appear in a test that builds both sides from the Python
    constant, and it cannot appear on a machine with no Kata guest: it appears
    on a real host, at preflight, as "the observer is unavailable", after the
    image has already been built and shipped. Reading the header is what makes
    it a test failure instead.
    """

    def test_a_status_from_another_version_is_refused(self) -> None:
        """Pins why the two must agree, so the check above reads as a rule."""
        with pytest.raises(GuestObserverProtocolError):
            parse_status(
                {
                    "version": GUEST_OBSERVER_VERSION + 1,
                    "ready": True,
                    "pid": 7,
                    "poll_seq": 1,
                    "event_seq": 1,
                    "lost": 0,
                    "journal_dropped": 0,
                    "journal_bytes": 0,
                    "journal_dev": 1,
                    "journal_ino": 1,
                }
            )
