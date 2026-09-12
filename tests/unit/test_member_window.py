"""The member-window reducer: every way one turn's work could be counted twice.

The reducer's whole job is a negative one. It must never charge a thread's
life to two windows, never add a settled exit to the readings that already
covered the same range, never treat a counter that fell as negative work, and
never let an unreadable cycle become a baseline of zero. Those are the cases
below, because a plausible-looking number produced by any of them is worse
than a missing one: a dashboard cannot tell that a total was double counted.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from guest_serializer import run_serializer
from guest_window_counters import run_chooser

from cloud_agent_host.member_resources import (
    EXIT_COUNTER_CPU,
    EXIT_COUNTER_IO,
    MEMBER_WINDOW_SOURCE,
    STATUS_AVAILABLE,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    WINDOW_ADDRESS_SPACES_DEDUPED,
    WINDOW_CPU,
    WINDOW_PSS,
    WINDOW_PSS_COMPLETE,
    WINDOW_READING_COMPLETE,
    WINDOW_STORAGE_IO,
    MemberWindowReducer,
    ThreadExit,
    ThreadReading,
    WindowAccumulator,
    WindowSample,
    exit_events_from_processes,
    instance_key,
    parse_window_sample,
    parse_window_samples,
)

MEASUREMENT = "00000000-0000-4000-8000-000000000001"
BOOT = "b0b1e2d3-0000-0000-0000-000000000000"
COMPLETE = WINDOW_CPU | WINDOW_STORAGE_IO | WINDOW_READING_COMPLETE
COMPLETE_PSS = WINDOW_PSS | WINDOW_PSS_COMPLETE | WINDOW_ADDRESS_SPACES_DEDUPED


def reading(
    tid: int,
    *,
    starttime: int = 100,
    cpu_ns: int | None = 0,
    read_bytes: int | None = 0,
    write_bytes: int | None = 0,
    uid: int = 11001,
    born: bool = False,
) -> ThreadReading:
    """One live task's cumulative readings at one census.

    ``born`` is what the parser sets when the task's own start time falls
    inside the window, and it defaults to False here because the ordinary task
    a census lists -- the resident agent -- was already running.
    """
    return ThreadReading(
        instance=instance_key(boot_id=BOOT, starttime_ticks=starttime, tid=tid),
        tid=tid,
        tgid=tid,
        uid=uid,
        cpu_ns=cpu_ns,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        born_in_window=born,
    )


def cycle(
    seq: int,
    *readings: ThreadReading,
    counters: int = COMPLETE | COMPLETE_PSS,
    mono_ns: int | None = None,
    pss_bytes: int | None = 0,
    address_spaces: int = 1,
    pss_failed: int = 0,
) -> WindowSample:
    """One census cycle, with only the fields under test varied."""
    return WindowSample(
        sample_seq=seq,
        guest_mono_ns=mono_ns if mono_ns is not None else seq * 1_000_000_000,
        counters=counters,
        tasks=readings,
        address_spaces=address_spaces,
        pss_bytes=pss_bytes,
        pss_failed=pss_failed,
    )


def exit_of(
    tid: int,
    *,
    starttime: int = 100,
    cpu_ns: int | None = 0,
    read_bytes: int | None = 0,
    write_bytes: int | None = 0,
    mono_ns: int = 1_000_000_000,
    counter_bits: int = EXIT_COUNTER_CPU | EXIT_COUNTER_IO,
    born_in_window: bool = True,
) -> ThreadExit:
    """One task's settled exit, as the kernel reported it.

    ``born_in_window`` defaults to True because that is what a task the window
    itself started looks like: the guest's fork record arrives inside the
    cursor's slice and the tracker stamps the proof onto the exit. Pass False
    to model a task that was already running when the window opened, whose
    settled lifetime the reducer must refuse to charge here.
    """
    return ThreadExit(
        instance=instance_key(boot_id=BOOT, starttime_ticks=starttime, tid=tid),
        guest_mono_ns=mono_ns,
        cpu_ns=cpu_ns,
        read_bytes=read_bytes,
        write_bytes=write_bytes,
        counter_bits=counter_bits,
        born_in_window=born_in_window,
    )


def reducer(**kwargs: object) -> MemberWindowReducer:
    return MemberWindowReducer(uid=11001, measurement_id=MEASUREMENT, **kwargs)  # type: ignore[arg-type]


def opened(**kwargs: object) -> MemberWindowReducer:
    """A reducer whose window opened at the guest clock's origin."""
    return reducer(opened_guest_mono_ns=0, **kwargs)


def summary(window: MemberWindowReducer, *, state: str = "final", duration_ms: int = 130_000):
    return window.summary(state=state, duration_ms=duration_ms)


class TestLiveEstimate:
    """A cumulative reading is charged by its growth, never by its value."""

    def test_only_growth_is_charged(self) -> None:
        """Three readings of one instance are one increment, not three totals."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000, read_bytes=100)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_500_000_000, read_bytes=160)))
        window.observe_sample(cycle(2, reading(7, cpu_ns=2_000_000_000, read_bytes=200)))

        result = summary(window)

        assert result["cpu"]["usec"] == 1_000_000
        assert result["storage_io"]["read_bytes"] == 100

    def test_the_first_reading_opens_the_range(self) -> None:
        """A resident agent is not billed for everything it ever did."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=90_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=90_500_000_000)))

        assert summary(window)["cpu"]["usec"] == 500_000

    def test_two_resident_threads_are_two_ranges(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000), reading(8, cpu_ns=2_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_400_000_000), reading(8, cpu_ns=5_000)))

        assert summary(window)["cpu"]["usec"] == 400_003

    def test_a_new_instance_does_not_donate_its_history_to_the_window(self) -> None:
        """A child that already ran for nine seconds is not nine seconds of this window."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000)))
        window.observe_sample(
            cycle(
                1,
                reading(7, cpu_ns=1_100_000_000),
                reading(9, starttime=500, cpu_ns=9_000_000_000),
            )
        )
        window.observe_sample(
            cycle(
                2,
                reading(7, cpu_ns=1_200_000_000),
                reading(9, starttime=500, cpu_ns=9_100_000_000),
            )
        )

        result = summary(window)

        # 0.2 s of resident work plus 0.1 s of the child's, not 9.3 s.
        assert result["cpu"]["usec"] == 300_000

    def test_an_instance_that_disappears_is_not_negative_work(self) -> None:
        """The member's live set shrinks when a task exits; that is not credit."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=5_000_000_000), reading(9, cpu_ns=1_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=6_000_000_000), reading(9, cpu_ns=2_000)))
        window.observe_sample(cycle(2, reading(7, cpu_ns=6_500_000_000)))

        result = summary(window)

        assert result["cpu"]["usec"] == 1_500_001

    def test_an_incomplete_cycle_cannot_move_a_total(self) -> None:
        """A cycle that could not read everything may not open or close a range."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=9_000_000_000), counters=WINDOW_CPU))

        result = summary(window)

        assert result["cpu"]["usec"] == 0
        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "sampling_over_budget" in result["cpu"]["reason_codes"]

    def test_a_counter_that_falls_is_refused_rather_than_reconciled(self) -> None:
        """Reconciling a backwards counter would mean inventing time."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=5_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_000_000_000)))

        result = summary(window)

        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "counter_regressed" in result["cpu"]["reason_codes"]
        assert window.regressions == 1


class TestExitEstimate:
    """A settled exit is the kernel's final word for exactly one instance."""

    def test_an_exit_is_counted_once_even_when_delivered_twice(self) -> None:
        window = reducer()
        settled = exit_of(42, cpu_ns=250_000_000, read_bytes=10, write_bytes=2)
        window.observe_exit(settled)
        window.observe_exit(settled)

        result = summary(window)

        assert result["cpu"]["usec"] == 250_000
        assert result["storage_io"]["read_bytes"] == 10
        assert window.duplicate_exits == 1

    def test_a_short_lived_process_is_measured_without_any_census(self) -> None:
        """The sub-second case a one-second sampler would otherwise lose."""
        window = reducer()
        window.observe_exit(exit_of(99, starttime=7, cpu_ns=400_000))

        result = summary(window)

        assert result["cpu"]["usec"] == 400
        assert result["cpu"]["status"] == STATUS_AVAILABLE

    def test_a_missing_counter_is_absent_rather_than_zero(self) -> None:
        """A kernel without task I/O accounting reports nothing, not nothing used."""
        window = reducer()
        window.observe_exit(exit_of(99, cpu_ns=400_000, counter_bits=EXIT_COUNTER_CPU))

        result = summary(window)

        assert result["cpu"]["usec"] == 400
        assert result["storage_io"]["status"] == STATUS_UNAVAILABLE
        assert result["storage_io"]["read_bytes"] is None

    def test_an_unproven_exit_is_refused_rather_than_charged_in_full(self) -> None:
        """A lifetime the window did not watch begin is not the window's work.

        The kernel's exit record carries what the task consumed over its whole
        life. A resident process that has run for ninety seconds across earlier
        turns and exits during this one would, charged in full, make a two
        second turn report ninety seconds of CPU -- and it would report it as a
        measurement rather than as a gap.
        """
        window = opened()
        window.observe_exit(exit_of(7, starttime=500, cpu_ns=90_000_000_000, born_in_window=False))

        result = summary(window, duration_ms=2_000)

        # Nothing was measured, so the metric is absent rather than a zero --
        # and above all it is not ninety seconds.
        assert result["cpu"]["usec"] is None
        assert result["cpu"]["status"] == STATUS_UNAVAILABLE
        assert "missing_thread_baseline" in result["cpu"]["reason_codes"]

    def test_an_incomplete_opening_census_cannot_donate_a_lifetime(self) -> None:
        """The 901x case: no baseline, then the resident exits inside the window.

        An opening census that could not read one task baselines nothing, so
        the resident that exits a second later has no range to close. Its
        settled lifetime must not become this turn's total.
        """
        window = opened()
        window.observe_sample(
            cycle(0, reading(7, starttime=500, cpu_ns=90_000_000_000), counters=WINDOW_CPU)
        )
        window.observe_exit(
            exit_of(
                7,
                starttime=500,
                cpu_ns=90_100_000_000,
                mono_ns=1_200_000_000,
                born_in_window=False,
            )
        )

        result = summary(window, duration_ms=2_000)

        assert result["cpu"]["usec"] is None
        assert result["cpu"]["status"] == STATUS_UNAVAILABLE
        assert "missing_thread_baseline" in result["cpu"]["reason_codes"]

    def test_a_refused_instance_does_not_poison_the_rest_of_the_window(self) -> None:
        """One unproven exit must not cost the window the work it did measure."""
        window = opened()
        window.observe_sample(cycle(0, reading(7, starttime=500, cpu_ns=90_000_000_000)))
        window.observe_sample(cycle(1, reading(7, starttime=500, cpu_ns=90_500_000_000)))
        window.observe_exit(exit_of(8, starttime=400, cpu_ns=30_000_000_000, born_in_window=False))

        result = summary(window)

        # The resident's half second is still counted; the unproven instance
        # contributes nothing and says so.
        assert result["cpu"]["usec"] == 500_000
        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "missing_thread_baseline" in result["cpu"]["reason_codes"]

    def test_a_proven_birth_still_closes_a_range_it_opened(self) -> None:
        """The refusal is about proof, not about exits in general."""
        window = opened()
        window.observe_sample(cycle(0, reading(9, starttime=5, cpu_ns=100_000, born=True)))
        window.observe_exit(exit_of(9, starttime=5, cpu_ns=400_000, born_in_window=False))

        # The reading proved the birth; the exit only closes what it opened.
        assert summary(window)["cpu"]["usec"] == 400

    def test_a_recycled_tid_is_a_different_instance(self) -> None:
        """Two threads that shared a TID at different times are two threads."""
        window = reducer()
        window.observe_exit(exit_of(42, starttime=100, cpu_ns=100))
        window.observe_exit(exit_of(42, starttime=200, cpu_ns=300))

        assert summary(window)["cpu"]["usec"] == 0

    def test_ring_loss_marks_the_metric_partial(self) -> None:
        window = reducer()
        window.observe_exit(exit_of(99, cpu_ns=400_000))
        window.note_ring_loss(3)

        result = summary(window)

        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "ring_loss" in result["cpu"]["reason_codes"]


class TestBothSources:
    """The same work seen twice must not be added twice."""

    def test_a_process_that_ran_and_exited_is_charged_once(self) -> None:
        """The decisive case: readings then a settled exit for one process.

        The census saw the process while it ran; the kernel then reported its
        settled total. Adding those two numbers would bill the process twice,
        and the result would still look entirely plausible.
        """
        window = reducer()
        window.observe_sample(cycle(0, reading(99, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(99, cpu_ns=1_800_000_000)))
        window.observe_exit(exit_of(99, cpu_ns=1_200_000_000, mono_ns=2_500_000_000))

        result = summary(window)

        # The readings took this instance to 1.8 s; the kernel settled 1.2 s.
        # Nothing more is owed, and the double counted 3.0 s must not appear.
        assert result["cpu"]["usec"] == 800_000
        assert result["cpu"]["usec"] < 1_200_000

    def test_the_exit_tail_beyond_the_last_reading_is_added_once(self) -> None:
        """Between the last census and the exit the process may still work."""
        window = reducer()
        window.observe_sample(cycle(0, reading(99, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(99, cpu_ns=1_100_000_000)))
        window.observe_exit(exit_of(99, cpu_ns=1_500_000_000, mono_ns=2_500_000_000))

        result = summary(window)

        assert result["cpu"]["usec"] == 500_000
        assert result["cpu"]["status"] == STATUS_AVAILABLE

    def test_storage_io_follows_the_same_rule(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(99, read_bytes=1_000, write_bytes=10)))
        window.observe_sample(cycle(1, reading(99, read_bytes=2_000, write_bytes=20)))
        window.observe_exit(exit_of(99, read_bytes=2_600, write_bytes=35, mono_ns=2_500_000_000))

        result = summary(window)

        assert result["storage_io"]["read_bytes"] == 1_600
        assert result["storage_io"]["write_bytes"] == 25

    def test_a_resident_agent_plus_a_short_lived_tool_child(self) -> None:
        """The ordinary shape of a turn."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000)))
        window.observe_sample(cycle(1, reading(7, cpu_ns=2_000_000_000)))
        window.observe_exit(exit_of(9, starttime=500, cpu_ns=300_000, mono_ns=1_500_000_000))
        window.observe_sample(cycle(2, reading(7, cpu_ns=3_000_000_000)))

        result = summary(window)

        assert result["cpu"]["usec"] == 2_000_300


class TestMemory:
    """The peak is a maximum over complete cycles, and nothing else."""

    def test_staggered_peaks_are_compared_not_added(self) -> None:
        """Two processes that each held 1 GiB at different times never held 2."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=1_073_741_824))
        window.observe_sample(cycle(1, reading(8, starttime=500), pss_bytes=1_073_741_824))

        assert summary(window)["memory"]["peak_bytes"] == 1_073_741_824

    def test_an_incomplete_cycle_cannot_set_the_peak(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=1_000))
        window.observe_sample(
            cycle(1, reading(7), pss_bytes=999_999, counters=COMPLETE | WINDOW_PSS, pss_failed=1)
        )

        result = summary(window)

        assert result["memory"]["peak_bytes"] == 1_000
        assert result["memory"]["status"] == STATUS_PARTIAL
        assert result["memory"]["valid_samples"] == 1
        assert result["memory"]["expected_samples"] == 2

    def test_no_complete_cycle_means_no_peak(self) -> None:
        """A missing measurement is not a small one, and RSS is never a substitute."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=None, counters=COMPLETE, pss_failed=2))

        result = summary(window)

        assert result["memory"]["peak_bytes"] is None
        assert result["memory"]["status"] == STATUS_UNAVAILABLE

    def test_a_genuine_zero_is_a_measurement(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=0))

        result = summary(window)

        assert result["memory"]["peak_bytes"] == 0
        assert result["memory"]["status"] == STATUS_AVAILABLE

    def test_unproven_address_space_sharing_degrades_the_metric(self) -> None:
        """Without a dedup proof the sum may count one mapping twice."""
        window = reducer()
        window.observe_sample(
            cycle(0, reading(7), pss_bytes=1_000, counters=WINDOW_PSS | WINDOW_PSS_COMPLETE)
        )

        result = summary(window)

        assert result["memory"]["status"] == STATUS_PARTIAL
        assert "address_space_share_unverified" in result["memory"]["reason_codes"]

    def test_the_gap_between_valid_cycles_is_recorded(self) -> None:
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=1_000, mono_ns=1_000_000_000))
        window.observe_sample(cycle(1, reading(7), pss_bytes=1_000, mono_ns=5_000_000_000))

        assert summary(window)["memory"]["max_gap_ms"] == 4_000

    def test_a_missing_cycle_makes_the_plan_short(self) -> None:
        """A gap in the requester's own counter is a cycle that never ran."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7)))
        window.observe_sample(cycle(4, reading(7)))

        result = summary(window)

        assert result["memory"]["expected_samples"] == 5
        assert "sampling_over_budget" in result["memory"]["reason_codes"]


class TestWindowShape:
    """What the snapshot says about the window, not about the numbers."""

    def test_a_prefix_is_never_rounded_up(self) -> None:
        window = opened()
        window.observe_sample(cycle(0, reading(7), mono_ns=0))
        window.observe_sample(cycle(1, reading(7), mono_ns=12_000_000_000))

        result = summary(window, state="collecting", duration_ms=130_000)

        assert result["window"]["observed_through_ms"] == 12_000
        assert result["window"]["duration_ms"] == 130_000
        assert result["state"] == "collecting"

    def test_a_missing_tail_is_reported_as_a_gap_not_a_shorter_window(self) -> None:
        """A truncated turn must not be dressed up as a shorter finished one.

        The window really ran for 130 seconds; the data reached 12. Clamping
        the duration to 12 would have hidden the missing tail behind a
        complete-looking short turn, which is why the true end and the true
        cut-off are both kept and no metric may then read as complete.
        """
        window = opened()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000), mono_ns=0))
        window.observe_sample(cycle(1, reading(7, cpu_ns=2_000_000), mono_ns=12_000_000_000))

        result = summary(window, state="final", duration_ms=130_000)

        assert result["window"]["duration_ms"] == 130_000
        assert result["window"]["observed_through_ms"] == 12_000
        assert result["cpu"]["status"] == STATUS_PARTIAL
        assert "window_prefix_retained" in result["cpu"]["reason_codes"]
        assert result["memory"]["status"] == STATUS_PARTIAL
        assert "window_prefix_retained" in result["memory"]["reason_codes"]

    def test_a_window_that_ended_where_it_was_measured_stays_complete(self) -> None:
        """The ordinary case: the closing census arrived, so nothing is missing."""
        window = opened()
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000), mono_ns=0))
        window.observe_sample(cycle(1, reading(7, cpu_ns=2_000_000), mono_ns=12_000_000_000))

        result = summary(window, state="final", duration_ms=12_000)

        assert result["window"]["duration_ms"] == 12_000
        assert result["window"]["observed_through_ms"] == 12_000
        assert result["cpu"]["status"] == STATUS_AVAILABLE

    def test_the_scope_and_source_are_stamped(self) -> None:
        result = summary(reducer())

        assert result["scope"] == "member_execution_window"
        assert result["source"] == MEMBER_WINDOW_SOURCE
        assert result["schema_version"] == 1

    def test_no_readings_at_all_is_unavailable_not_zero(self) -> None:
        result = summary(reducer())

        assert result["cpu"]["usec"] is None
        assert result["cpu"]["status"] == STATUS_UNAVAILABLE
        assert result["memory"]["peak_bytes"] is None
        assert result["storage_io"]["read_bytes"] is None

    def test_a_proven_zero_is_zero(self) -> None:
        """A complete window in which the member genuinely used nothing."""
        window = reducer()
        window.observe_sample(cycle(0, reading(7), pss_bytes=0))

        result = summary(window)

        assert result["cpu"]["usec"] == 0
        assert result["cpu"]["status"] == STATUS_AVAILABLE
        assert result["storage_io"]["read_bytes"] == 0

    def test_an_empty_member_is_a_measured_zero_not_a_missing_reading(self) -> None:
        """No tasks at all is the one case where a total of zero is complete."""
        window = reducer()
        window.observe_sample(cycle(0))

        result = summary(window)

        assert result["cpu"]["usec"] == 0
        assert result["cpu"]["status"] == STATUS_AVAILABLE


class TestJournalParsing:
    """Reading the guest's records without inventing what is not there."""

    def record(self, **overrides: object) -> dict[str, object]:
        window: dict[str, object] = {
            "sample_seq": 3,
            "counters": COMPLETE | COMPLETE_PSS,
            "tasks": 1,
            "address_spaces": 1,
            "pss_bytes": 4096,
            "pss_failed": 0,
            "cpu_ns": 5_000_000_000,
            "read_bytes": 100,
            "write_bytes": 20,
            "task_detail": [
                {
                    "tid": 7,
                    "tgid": 7,
                    "uid": 11001,
                    "starttime": 100,
                    "cpu_ns": 5_000_000_000,
                    "read_bytes": 100,
                    "write_bytes": 20,
                    "counters": WINDOW_CPU | WINDOW_STORAGE_IO,
                }
            ],
        }
        window.update(overrides)
        return {
            "version": 2,
            "seq": 11,
            "kind": "window_sample",
            "mono_ns": 1_000_000,
            "measurement_id": MEASUREMENT,
            "uid": 11001,
            "window": window,
        }

    def test_a_record_round_trips(self) -> None:
        parsed = parse_window_sample(self.record(), boot_id=BOOT)

        assert parsed is not None
        assert parsed.sample_seq == 3
        assert parsed.pss_bytes == 4096
        assert len(parsed.tasks) == 1
        assert parsed.tasks[0].tid == 7
        assert parsed.tasks[0].cpu_ns == 5_000_000_000

    def test_another_events_kind_is_not_a_cycle(self) -> None:
        other = self.record()
        other["kind"] = "exit"

        assert parse_window_sample(other, boot_id=BOOT) is None

    @pytest.mark.parametrize(
        "override",
        [
            pytest.param({"sample_seq": None}, id="no-sequence"),
            pytest.param({"counters": None}, id="no-counter-mask"),
        ],
    )
    def test_a_record_missing_its_own_identity_is_skipped(
        self, override: dict[str, object]
    ) -> None:
        assert parse_window_sample(self.record(**override), boot_id=BOOT) is None

    def test_a_task_entry_without_a_start_time_is_skipped(self) -> None:
        """Without the kernel's start time there is no stable instance."""
        detail = [{"tid": 7, "tgid": 7, "uid": 11001, "cpu_ns": 5}]

        parsed = parse_window_sample(self.record(task_detail=detail), boot_id=BOOT)

        assert parsed is not None
        assert parsed.tasks == ()

    def test_only_this_measurement_is_read_out_of_a_shared_guest(self) -> None:
        """A fork sibling's cycles share the journal and must not contribute."""
        mine = json.dumps(self.record()).encode()
        theirs = json.dumps(
            {**self.record(sample_seq=0), "measurement_id": "11111111-2222-4333-8444-555555555555"}
        ).encode()

        samples = parse_window_samples(
            theirs + b"\n" + mine + b"\n",
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
        )

        assert [item.sample_seq for item in samples] == [3]

    def test_a_damaged_line_is_skipped_rather_than_repaired(self) -> None:
        payload = b'{"kind": "window_sample"\n' + json.dumps(self.record()).encode() + b"\n"

        assert len(parse_window_samples(payload, measurement_id=MEASUREMENT, boot_id=BOOT)) == 1

    def test_cycles_are_ordered_by_the_requester_s_counter(self) -> None:
        payload = b"\n".join(json.dumps(self.record(sample_seq=seq)).encode() for seq in (2, 0, 1))

        samples = parse_window_samples(payload, measurement_id=MEASUREMENT, boot_id=BOOT)

        assert [item.sample_seq for item in samples] == [0, 1, 2]


class TestExitEventProjection:
    """Turning observed process rows into exit events without guessing."""

    class Row:
        def __init__(self, **kwargs: object) -> None:
            self.uid = 11001
            self.pid = 42
            self.started_guest_mono_ns = 1_000_000_000
            self.exited_guest_mono_ns = 2_000_000_000
            self.observed_exit = True
            self.cpu_usec = 1_500
            self.io_read_bytes = 10
            self.io_write_bytes = 2
            for key, value in kwargs.items():
                setattr(self, key, value)

    def test_a_settled_row_becomes_one_event(self) -> None:
        events = exit_events_from_processes([self.Row()], boot_id=BOOT)

        assert len(events) == 1
        assert events[0].cpu_ns == 1_500_000
        assert events[0].read_bytes == 10

    def test_a_row_that_never_exited_is_not_an_exit(self) -> None:
        """A process still running at close has no settled figure to report."""
        assert exit_events_from_processes([self.Row(observed_exit=False)], boot_id=BOOT) == []

    def test_a_row_without_a_start_time_is_not_keyed_by_pid(self) -> None:
        """Without the kernel's start time there is no stable identity."""
        assert exit_events_from_processes([self.Row(started_guest_mono_ns=0)], boot_id=BOOT) == []


class TestTheGuestCycleBits:
    """What one census claims about itself, from the guest's own code.

    The host gates every per-instance range on ``FI_WINDOW_READING_COMPLETE``,
    so whatever sets that bit decides whether a turn is measured at all. These
    cases are computed by compiling ``window_counter_bits`` out of
    ``guest-observer.c``, for the same reason the wire format below is: a rule
    checked by reading it is a rule that drifts.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def cycle_bits(cls, tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
        return run_chooser(tmp_path_factory.mktemp("guest-window-counters"))

    def test_a_kernel_without_task_io_accounting_still_measures_cpu(
        self, cycle_bits: dict[str, int]
    ) -> None:
        """The coupling that made the whole metric depend on an unused counter.

        Task I/O accounting has its own bit and the platform declines to publish
        the figure at all. While it also had to be present for a cycle to count
        as complete, a guest whose kernel does not account it -- or one process
        whose io file could not be read -- left every cycle short, so no range
        ever opened and the turn measured no CPU.
        """
        bits = cycle_bits["io_unaccounted"]

        assert bits & WINDOW_READING_COMPLETE
        assert bits & WINDOW_CPU
        assert not bits & WINDOW_STORAGE_IO

    def test_an_empty_member_is_a_complete_cycle(self, cycle_bits: dict[str, int]) -> None:
        """A turn that finished its work and went quiet is a measured zero."""
        bits = cycle_bits["empty_member"]

        assert bits & WINDOW_READING_COMPLETE
        assert not bits & WINDOW_PSS

    def test_an_unread_cpu_counter_still_makes_the_cycle_short(
        self, cycle_bits: dict[str, int]
    ) -> None:
        """The bit is about CPU, so a missing CPU counter must still clear it."""
        bits = cycle_bits["cpu_unread"]

        assert not bits & WINDOW_READING_COMPLETE
        assert not bits & WINDOW_CPU

    def test_a_failed_pss_read_does_not_touch_the_cpu_bits(
        self, cycle_bits: dict[str, int]
    ) -> None:
        """Per-metric independence, at the cycle level where it starts."""
        bits = cycle_bits["pss_failed"]

        assert bits & WINDOW_READING_COMPLETE
        assert not bits & WINDOW_PSS_COMPLETE

    def test_the_host_advances_a_cycle_the_guest_calls_complete(
        self, cycle_bits: dict[str, int]
    ) -> None:
        """The two halves agree: these exact bits open and close a range."""
        window = opened()
        bits = cycle_bits["io_unaccounted"]
        window.observe_sample(cycle(0, reading(7, cpu_ns=1_000_000_000), counters=bits))
        window.observe_sample(cycle(1, reading(7, cpu_ns=1_400_000_000), counters=bits))

        result = summary(window)

        assert result["cpu"]["usec"] == 400_000
        assert "sampling_over_budget" not in result["cpu"]["reason_codes"]


class TestTheGuestWireFormat:
    """The census record parses, using the guest's own serializer.

    Earlier versions of this test fed the parser a hand-written fixture, which
    matched the parser instead of the producer: a census record missing its
    outer closing brace kept every host test green while the real journal was
    unreadable. The bytes below are produced by compiling
    ``journal_window_sample`` out of ``guest-observer.c`` and running it, so a
    shape change is measured rather than assumed.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def guest_journal(cls, tmp_path_factory: pytest.TempPathFactory) -> bytes:
        return run_serializer(tmp_path_factory.mktemp("guest-serializer"))

    def test_every_record_the_guest_writes_is_valid_json(self, guest_journal: bytes) -> None:
        lines = [line for line in guest_journal.splitlines() if line.strip()]

        assert len(lines) >= 2
        for line in lines:
            decoded = json.loads(line)
            assert decoded["kind"] == "window_sample"
            assert isinstance(decoded["window"], dict)

    def test_the_host_reads_back_what_the_guest_wrote(self, guest_journal: bytes) -> None:
        samples = parse_window_samples(
            guest_journal,
            measurement_id="measurement-1",
            boot_id=BOOT,
        )

        # The opening cycle is an empty member and the periodic one carries two
        # tasks; both are real states and both must survive the round trip.
        assert len(samples) == 2
        assert [sample.sample_seq for sample in samples] == [0, 1]
        assert samples[0].tasks == ()
        assert len(samples[1].tasks) == 2
        assert {task.tid for task in samples[1].tasks} == {7, 8}
        assert samples[1].address_spaces == 2
        assert samples[1].pss_bytes == 8192

    def test_a_cycle_the_parser_cannot_use_is_visible_rather_than_silent(
        self, guest_journal: bytes
    ) -> None:
        """Truncating the guest's own bytes must lose the cycle, not fake one."""
        truncated = guest_journal[: len(guest_journal) // 2]

        samples = parse_window_samples(
            truncated,
            measurement_id="measurement-1",
            boot_id=BOOT,
        )

        assert len(samples) <= 1

    def test_records_for_another_measurement_are_ignored(self, guest_journal: bytes) -> None:
        samples = parse_window_samples(
            guest_journal,
            measurement_id="some-other-measurement",
            boot_id=BOOT,
        )

        assert samples == []


class TestProductionChainRegressions:
    """The cases the reviewer reproduced through the real entry points.

    Each of these failed while the unit tests around it passed, because those
    tests exercised a helper the production path never called or a shape the
    production path never used. They go through the same functions the
    supervisor does.
    """

    OPENED_NS = 1_000_000_000_000

    def window_record(self, *, tid: int, starttime_ticks: int, cpu_ns: int, seq: int = 1):
        import json as _json

        return _json.dumps(
            {
                "version": 2,
                "seq": seq,
                "kind": "window_sample",
                "mono_ns": self.OPENED_NS + seq * 1_000_000_000,
                "measurement_id": MEASUREMENT,
                "uid": 11001,
                "window": {
                    "sample_seq": seq,
                    "counters": WINDOW_CPU | WINDOW_STORAGE_IO | WINDOW_READING_COMPLETE,
                    "address_spaces": 1,
                    "pss_bytes": 0,
                    "pss_failed": 0,
                    "task_detail": [
                        {
                            "tid": tid,
                            "tgid": tid,
                            "uid": 11001,
                            "starttime": starttime_ticks,
                            "cpu_ns": cpu_ns,
                            "read_bytes": 0,
                            "write_bytes": 0,
                            "counters": WINDOW_CPU,
                        }
                    ],
                },
            }
        )

    def reduce(self, payload: bytes, *, opened_ns: int | None = None):
        from cloud_agent_host.member_resources import reduce_window

        origin = self.OPENED_NS if opened_ns is None else opened_ns
        samples = parse_window_samples(
            payload,
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
            opened_guest_mono_ns=origin,
        )
        return reduce_window(
            uid=11001,
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
            samples=samples,
            opened_guest_mono_ns=origin,
            state="final",
            duration_ms=0,
        )

    def test_a_child_born_inside_the_window_is_not_baselined_away(self) -> None:
        """The window owes everything a task born during it has consumed."""
        ticks = (self.OPENED_NS + 2_000_000_000) // 10_000_000
        first = self.window_record(tid=50, starttime_ticks=ticks, cpu_ns=900_000_000, seq=1)
        second = self.window_record(tid=50, starttime_ticks=ticks, cpu_ns=1_000_000_000, seq=2)

        result = self.reduce((first + "\n" + second + "\n").encode())

        # 900 ms was already spent when the window first saw it, and it grew by
        # another 100 ms. Baselining the first reading discarded the 900 ms.
        assert result["cpu"]["usec"] == 1_000_000
        assert result["cpu"]["status"] == STATUS_AVAILABLE

    def test_a_resident_task_is_still_baselined(self) -> None:
        """A task that predates the window is charged only its growth."""
        ticks = (self.OPENED_NS - 60_000_000_000) // 10_000_000
        first = self.window_record(tid=7, starttime_ticks=ticks, cpu_ns=90_000_000_000, seq=1)
        second = self.window_record(tid=7, starttime_ticks=ticks, cpu_ns=90_500_000_000, seq=2)

        result = self.reduce((first + "\n" + second + "\n").encode())

        assert result["cpu"]["usec"] == 500_000

    def test_the_prefix_report_is_accepted_by_the_api_schema(self) -> None:
        """A shortened window must not produce a report the API refuses.

        Keeping the true window end while claiming ``final`` violated the
        request schema, so the snapshot was rejected and the runner swallowed
        the rejection: the turn went unreported entirely.
        """
        from cloud_agent.schemas import WorkerTurnResourcesRequest

        ticks = (self.OPENED_NS - 60_000_000_000) // 10_000_000
        payload = (
            self.window_record(tid=7, starttime_ticks=ticks, cpu_ns=1_000_000, seq=1) + "\n"
        ).encode()
        samples = parse_window_samples(
            payload,
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
            opened_guest_mono_ns=self.OPENED_NS,
        )
        from cloud_agent_host.member_resources import reduce_window

        snapshot = reduce_window(
            uid=11001,
            measurement_id=MEASUREMENT,
            boot_id=BOOT,
            samples=samples,
            opened_guest_mono_ns=self.OPENED_NS,
            state="final",
            duration_ms=60_000,
        )
        snapshot["report_seq"] = 1

        assert snapshot["state"] == "collecting"
        assert snapshot["window"]["duration_ms"] == 60_000
        assert snapshot["window"]["observed_through_ms"] < 60_000
        WorkerTurnResourcesRequest.model_validate(snapshot)

    def test_an_unproven_filesystem_reports_no_io_rather_than_a_zero(self) -> None:
        """Two zeroes from an unverified filesystem are not a measured nothing."""
        ticks = (self.OPENED_NS - 60_000_000_000) // 10_000_000
        payload = (
            self.window_record(tid=7, starttime_ticks=ticks, cpu_ns=1_000_000) + "\n"
        ).encode()

        result = self.reduce(payload)

        assert result["storage_io"]["status"] == STATUS_UNAVAILABLE
        assert result["storage_io"]["read_bytes"] is None
        assert "filesystem_attribution_unverified" in result["storage_io"]["reason_codes"]


class TestTaskNumberReuse:
    """Two lives on one task number are two tasks, not one."""

    def test_a_reused_tid_produces_two_exits_with_their_own_totals(self) -> None:
        import json as _json

        from cloud_agent_host.member_resources import exit_events_from_journal

        lines = [
            {"kind": "fork", "pid": 50, "tgid": 50, "mono_ns": 10_000_000, "uid": 11001},
            {
                "kind": "exit",
                "pid": 50,
                "tgid": 50,
                "mono_ns": 20_000_000,
                "uid": 11001,
                "counters": 5,
                "cpu_ns": 100_000_000,
                "read_bytes": 10,
                "write_bytes": 0,
            },
            {
                "kind": "done",
                "pid": 50,
                "tgid": 50,
                "mono_ns": 20_000_000,
                "uid": 11001,
                "counters": 1,
                "cpu_ns": 100_000_000,
            },
            {"kind": "fork", "pid": 50, "tgid": 50, "mono_ns": 50_000_000, "uid": 11001},
            {
                "kind": "exit",
                "pid": 50,
                "tgid": 50,
                "mono_ns": 60_000_000,
                "uid": 11001,
                "counters": 5,
                "cpu_ns": 200_000_000,
                "read_bytes": 20,
                "write_bytes": 0,
            },
            {
                "kind": "done",
                "pid": 50,
                "tgid": 50,
                "mono_ns": 60_000_000,
                "uid": 11001,
                "counters": 1,
                "cpu_ns": 200_000_000,
            },
        ]
        payload = ("\n".join(_json.dumps(line) for line in lines) + "\n").encode()

        events = exit_events_from_journal(
            payload,
            boot_id=BOOT,
            measurement_id=MEASUREMENT,
            uid=11001,
        )

        # Merging the two lives reported only the second life's 200 ms.
        assert len(events) == 2
        assert sum(event.cpu_ns or 0 for event in events) == 300_000_000


class TestTheRunningAccumulator:
    """A crash must keep what was already measured, exactly once.

    The acceptance here is not "a checkpoint exists" but that the checkpoint
    carries the cycles already consumed, that feeding the same bytes twice
    cannot inflate it, and that a cycle the host could not read stays missing
    rather than becoming a zero.
    """

    MEAS = "accumulator-measurement"
    OPENED = 1_000_000_000_000

    def record(self, seq: int, *, cpu_ns: int, offset_seconds: int) -> str:
        import json as _json

        ticks = (self.OPENED - 60_000_000_000) // 10_000_000
        return _json.dumps(
            {
                "version": 2,
                "seq": seq,
                "kind": "window_sample",
                "mono_ns": self.OPENED + offset_seconds * 1_000_000_000,
                "measurement_id": self.MEAS,
                "uid": 11001,
                "window": {
                    "sample_seq": seq,
                    "counters": WINDOW_CPU
                    | WINDOW_STORAGE_IO
                    | WINDOW_READING_COMPLETE
                    | WINDOW_PSS
                    | WINDOW_PSS_COMPLETE
                    | WINDOW_ADDRESS_SPACES_DEDUPED,
                    "address_spaces": 1,
                    "pss_bytes": 4096,
                    "pss_failed": 0,
                    "task_detail": [
                        {
                            "tid": 7,
                            "tgid": 7,
                            "uid": 11001,
                            "starttime": ticks,
                            "cpu_ns": cpu_ns,
                            "read_bytes": 0,
                            "write_bytes": 0,
                            "counters": WINDOW_CPU,
                        }
                    ],
                },
            }
        )

    def accumulator(self) -> WindowAccumulator:
        return WindowAccumulator(
            uid=11001,
            measurement_id=self.MEAS,
            opened_guest_mono_ns=self.OPENED,
            boot_id=BOOT,
        )

    def test_a_checkpoint_carries_the_cycles_already_consumed(self) -> None:
        """What the guest measured survives into the running report."""
        window = self.accumulator()
        first = self.record(0, cpu_ns=1_000_000_000, offset_seconds=1) + "\n"
        window.consume(first.encode(), boot_id=BOOT, upto_offset=len(first))

        snapshot = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)

        # The task predates the window, so its first reading is this window's
        # baseline and the amount charged so far is zero -- a real zero, since
        # a cycle was read, rather than the unavailable of an empty reducer.
        assert snapshot["cpu"]["usec"] == 0
        assert snapshot["cpu"]["status"] == STATUS_AVAILABLE
        assert snapshot["memory"]["peak_bytes"] == 4096

    def test_growth_across_reads_is_charged_once(self) -> None:
        window = self.accumulator()
        first = self.record(0, cpu_ns=1_000_000_000, offset_seconds=1) + "\n"
        second = self.record(1, cpu_ns=1_500_000_000, offset_seconds=2) + "\n"
        window.consume(first.encode(), boot_id=BOOT, upto_offset=len(first))
        window.consume(second.encode(), boot_id=BOOT, upto_offset=len(first) + len(second))

        snapshot = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)

        # 1.0 s is the baseline and 0.5 s is the growth the second cycle saw.
        assert snapshot["cpu"]["usec"] == 500_000

    def test_replaying_the_same_bytes_cannot_inflate_the_total(self) -> None:
        """A re-read after a failed consumption must not double count."""
        window = self.accumulator()
        first = self.record(0, cpu_ns=1_000_000_000, offset_seconds=1) + "\n"
        window.consume(first.encode(), boot_id=BOOT, upto_offset=len(first))
        before = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)

        # The caller re-reads from an offset it had already consumed, which is
        # what happens when a consumer dies between reading and advancing.
        window.consume(first.encode(), boot_id=BOOT, upto_offset=len(first))
        after = window.snapshot(state="collecting", duration_ms=30_000, report_seq=2)

        # The reducer is stateful: one cycle observed twice contributes once,
        # because a sample sequence already seen adds no growth.
        assert after["cpu"]["usec"] == before["cpu"]["usec"]

    def test_a_window_that_measured_nothing_reports_nothing_rather_than_zero(self) -> None:
        """No cycle read yet is unmeasured, not a member that used nothing."""
        window = self.accumulator()

        snapshot = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)

        assert snapshot["cpu"]["usec"] is None
        assert snapshot["cpu"]["status"] == STATUS_UNAVAILABLE
        assert snapshot["window"]["duration_ms"] == 30_000

    def test_the_offset_only_moves_past_what_was_folded_in(self) -> None:
        """The caller can resume from the last consumed byte."""
        window = self.accumulator()
        first = self.record(0, cpu_ns=1_000_000_000, offset_seconds=1) + "\n"

        assert window.offset == 0
        window.consume(first.encode(), boot_id=BOOT, upto_offset=len(first))

        assert window.offset == len(first)
        assert window.cycles == 1

    def test_the_checkpoint_is_what_the_final_report_will_carry(self) -> None:
        """Both read the same state, so a crash loses only the unreached tail."""
        window = self.accumulator()
        first = self.record(0, cpu_ns=1_000_000_000, offset_seconds=1) + "\n"
        second = self.record(1, cpu_ns=2_000_000_000, offset_seconds=2) + "\n"
        window.consume(first.encode(), boot_id=BOOT, upto_offset=len(first))
        running = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)
        window.consume(second.encode(), boot_id=BOOT, upto_offset=len(first) + len(second))
        final = window.snapshot(state="final", duration_ms=30_000, report_seq=2)

        assert running["cpu"]["usec"] == 0
        assert final["cpu"]["usec"] == 1_000_000
        assert final["measurement_id"] == running["measurement_id"]


class TestSlicesAndLateRecords:
    """Identity spans slices, and a record that arrives late is still counted.

    Both cases below were introduced by the fix for the other: keeping identity
    across a read boundary is what made a late record merge into an instance
    that had already been reported, and the merge then had to report again.
    """

    MEAS = "slice-measurement"
    OPENED = 1_000_000_000_000

    def accumulator(self) -> WindowAccumulator:
        return WindowAccumulator(
            uid=11001,
            measurement_id=self.MEAS,
            opened_guest_mono_ns=self.OPENED,
            boot_id=BOOT,
        )

    def census(self, seq: int, cpu_ns: int) -> bytes:
        import json as _json

        ticks = (self.OPENED - 60_000_000_000) // 10_000_000
        record = _json.dumps(
            {
                "version": 2,
                "seq": seq,
                "kind": "window_sample",
                "mono_ns": self.OPENED + seq * 1_000_000_000,
                "measurement_id": self.MEAS,
                "uid": 11001,
                "window": {
                    "sample_seq": seq,
                    "counters": WINDOW_CPU | WINDOW_STORAGE_IO | WINDOW_READING_COMPLETE,
                    "address_spaces": 1,
                    "pss_bytes": 4096,
                    "pss_failed": 0,
                    "task_detail": [
                        {
                            "tid": 9,
                            "tgid": 9,
                            "uid": 11001,
                            "starttime": ticks,
                            "cpu_ns": cpu_ns,
                            "counters": WINDOW_CPU,
                        }
                    ],
                },
            }
        )
        return (record + "\n").encode()

    def test_a_census_and_its_exit_in_different_slices_are_one_instance(self) -> None:
        """The window must not charge a live reading and its exit separately."""
        import json as _json

        window = self.accumulator()
        census = self.census(0, 1_000_000_000)
        window.consume(census, boot_id=BOOT, upto_offset=len(census))
        settled = (
            _json.dumps(
                {
                    "kind": "exit",
                    "pid": 9,
                    "tgid": 9,
                    "uid": 11001,
                    "mono_ns": self.OPENED + 2_000_000_000,
                    "counters": 1,
                    "cpu_ns": 1_000_000_000,
                }
            )
            + "\n"
        ).encode()
        window.consume(settled, boot_id=BOOT, upto_offset=len(census) + len(settled))

        snapshot = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)

        # The census was this task's baseline and the settled exit restates the
        # same total: nothing was consumed inside the window.
        assert snapshot["cpu"]["usec"] == 0

    def test_a_done_record_after_its_exit_is_still_counted(self) -> None:
        """The exit reads zero CPU; the scheduler's runtime arrives later."""
        import json as _json

        window = self.accumulator()
        # The guest emits a fork record for every task started inside the
        # window, and it is that record which proves the settled figure below
        # is this window's work rather than a lifetime that predates it.
        escape = (
            _json.dumps(
                {
                    "kind": "fork",
                    "pid": 9,
                    "tgid": 9,
                    "uid": 11001,
                    "mono_ns": self.OPENED + 500_000_000,
                }
            )
            + "\n"
            + _json.dumps(
                {
                    "kind": "exit",
                    "pid": 9,
                    "tgid": 9,
                    "uid": 11001,
                    "mono_ns": self.OPENED + 1_000_000_000,
                    "counters": 5,
                    "cpu_ns": 0,
                    "read_bytes": 4,
                    "write_bytes": 0,
                }
            )
            + "\n"
        ).encode()
        window.consume(escape, boot_id=BOOT, upto_offset=len(escape))

        done = (
            _json.dumps(
                {
                    "kind": "done",
                    "pid": 9,
                    "tgid": 9,
                    "uid": 11001,
                    "mono_ns": self.OPENED + 1_000_000_000,
                    "counters": 1,
                    "cpu_ns": 700_000_000,
                }
            )
            + "\n"
        ).encode()
        window.consume(done, boot_id=BOOT, upto_offset=len(escape) + len(done))

        snapshot = window.snapshot(state="collecting", duration_ms=30_000, report_seq=1)

        # Emitting the instance at its exit and never reporting the restatement
        # lost this thread's CPU entirely.
        assert snapshot["cpu"]["usec"] == 700_000
