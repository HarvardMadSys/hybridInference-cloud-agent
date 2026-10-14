"""Attempt, tool, process, and per-member turn resource measurement models."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Final, Literal

from pydantic import BaseModel, Field, model_validator

from cloud_agent_shared.shared_protocol import SHARED_MEMBER_ACCOUNTING_VERSION


class ToolCallResources(BaseModel):
    """Timing and attributable process resources for one tool call.

    Current shared turns leave whole-container sample fields empty because
    sibling members can execute concurrently. Kata may still attach exact
    exit-time accounting for child processes attributed to this call. Older
    rows can contain periodic sample windows; both shapes remain readable.

    A call that ran alongside others could not have its readings separated
    from theirs, so its numbers arrive in the ``shared`` fields with
    ``overlap_n`` above one. Those are upper bounds and must be presented as
    such; a reader that only understands the exclusive fields correctly shows
    nothing for them.
    """

    call_seq: int
    tool_use_id: str
    tool_name: str
    #: ``exact`` when the sandbox's own hooks marked the boundaries,
    #: ``message`` when the window is only when the platform parsed the
    #: call's events — which brackets the execution from outside.
    timing_source: str = "message"
    #: Null for a call that never returned — an outcome, not a missing number.
    duration_ms: int | None = None
    #: Readings taken inside this call's window. Below two, nothing is measured.
    sample_count: int = 0
    #: Calls whose windows shared these readings, including this one.
    overlap_n: int = 1
    #: How much of the call the readings actually span, so a reader can see
    #: that the numbers below are a floor rather than the whole call.
    measured_span_ms: int | None = None
    cpu_usec_exclusive: int | None = None
    cpu_usec_shared: int | None = None
    mem_peak_exclusive_bytes: int | None = None
    mem_peak_shared_bytes: int | None = None
    #: Kernel-observed processes whose starts plausibly belong to this call.
    #: Zero proves none only when the attempt's process observation is complete.
    process_count: int = 0
    #: What the processes this call alone started consumed, from the guest
    #: kernel's exit-time accounting. Independent of the sampler and of the
    #: call's length: a 35 ms call whose child exited has exact numbers here.
    #: CPU and I/O are sums over those processes; the peak is the largest
    #: single process. Null where no exited process was attributed to the
    #: call alone, or where the kernel does not account that counter.
    process_cpu_usec: int | None = None
    process_mem_peak_bytes: int | None = None
    process_io_read_bytes: int | None = None
    process_io_write_bytes: int | None = None
    #: True when a process this call may own was left out of the numbers:
    #: shared with an overlapping call, or still running at turn end. The
    #: numbers above are then floors, and a reader must say so.
    process_resources_partial: bool = False


class ProcessObservation(BaseModel):
    """One process created during a turn, observed by the Kata guest kernel."""

    process_seq: int
    uid: int
    pid: int
    tgid: int
    ppid: int | None = None
    comm: str
    executable: str | None = None
    #: Start relative to the turn's guest-observer cursor, not wall clock.
    started_offset_ms: int
    duration_ms: int | None = None
    exit_code: int | None = None
    observed_fork: bool = False
    observed_exec: bool = False
    observed_exit: bool = False
    #: Every plausible call when tool windows overlap; empty is unassigned.
    tool_call_seqs: list[int] = Field(default_factory=list)
    #: Exit-time accounting for the whole thread group, excluding children.
    #: Null until the leader exited, and null for any counter the guest
    #: kernel does not expose; never zero for something unmeasured.
    cpu_usec: int | None = None
    mem_peak_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    #: Bytes passed through read/write, which count pipes and caches too.
    io_rchar_bytes: int | None = None
    io_wchar_bytes: int | None = None


#: One metric's completeness, stated per metric rather than as a whole-turn
#: percentage. ``available`` means complete *within its declared method and
#: scope*: it never claims zero sampling error, zero boundary error, or that
#: the number covers the platform's whole cost. ``partial`` is a measured lower
#: bound for an additive quantity. ``unavailable`` carries no number at all.
ResourceMetricStatus = Literal["available", "partial", "unavailable"]

#: Every way a ``member_execution_window`` measurement can be incomplete. A
#: bounded enum, not free text: the admin UI maps each code to one fixed
#: sentence, so no UID, lease token, kernel field name or exception string can
#: reach a page through this field.
TurnResourceReasonCode = Literal[
    "missing_thread_baseline",
    "counter_regressed",
    "thread_identity_conflict",
    "observer_restarted",
    "ring_loss",
    "filesystem_attribution_unverified",
    "pss_unreadable",
    "address_space_share_unverified",
    "sampling_over_budget",
    "window_prefix_retained",
    "observer_unavailable",
    "not_a_kata_guest",
    #: A run whose attempts were measured by different capabilities. Their
    #: numbers do not share a method, so they are never added into one total.
    "source_mismatch",
    #: The live census and the settled exits disagree by more than the
    #: reconciliation tolerance. Both measure the same work, so a difference
    #: that large means one of them is wrong; the larger is reported as a
    #: floor rather than as an exact number.
    "estimates_diverged",
]

#: The only scope this schema describes. A member execution window bills what
#: one member did inside a shared sandbox; it is not the platform cost of a
#: task, and it must never be presented as one.
TURN_RESOURCE_SCOPE: Final[Literal["member_execution_window"]] = "member_execution_window"

#: The capability string that produced a summary. Summaries from different
#: sources are never added together.
TURN_RESOURCE_SOURCE: Final[Literal["kata-guest-member-window-v1"]] = "kata-guest-member-window-v1"

#: Reported before the sandbox has closed the window, and final once it has.
TurnResourceState = Literal["collecting", "final"]


class TurnCpuResources(BaseModel):
    """One member's own scheduler time inside the execution window.

    The sum over every tracked work thread of its ``se.sum_exec_runtime``
    delta. A thread that ran on two cores for ten seconds contributes about
    twenty seconds, so this may legitimately exceed wall-clock duration.
    Never derived from a utilisation percentage, and never summed from tool
    rows in place of the whole turn.
    """

    status: ResourceMetricStatus
    method: Literal["live_and_exit_task_counters"]
    usec: int | None = Field(None, ge=0)
    #: Sandbox CPU quota captured at window open, shared by all its members.
    #: Older snapshots omit it; current host configuration cannot fill it in.
    # Omitting unknown capacity preserves identical retries of pre-upgrade snapshots.
    capacity_cores: float | None = Field(
        None, gt=0, allow_inf_nan=False, strict=True, exclude_if=lambda value: value is None
    )
    reason_codes: list[TurnResourceReasonCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_the_number(self) -> TurnCpuResources:
        """Refuse a status that its own evidence contradicts."""
        if self.status == "unavailable" and self.usec is not None:
            raise ValueError("an unavailable CPU measurement cannot carry a value")
        if self.status == "available" and self.usec is None:
            raise ValueError("an available CPU measurement needs a value")
        return self


class TurnMemoryResources(BaseModel):
    """The sampled peak of the member's proportional resident memory.

    ``peak_bytes`` is the maximum over census cycles of the sum of PSS across
    the member's distinct address spaces. PSS shares mapped resident pages
    between users instead of billing each of them in full, but it is not
    machine memory: it excludes the VM kernel, unmapped page cache, host
    services and special page classes, and RSS is never substituted for it.
    Sampling cannot see between ticks, so even a complete plan stays labelled
    ``sampled``.
    """

    status: ResourceMetricStatus
    method: Literal["sampled_member_pss"]
    peak_bytes: int | None = Field(None, ge=0)
    sample_interval_ms: int = Field(..., gt=0)
    #: Cycles in which every one of the member's address spaces read cleanly.
    #: A cycle with one failed read is not a complete cycle and cannot set the
    #: peak.
    valid_samples: int = Field(0, ge=0)
    #: Cycles the schedule intended for ``window.duration_ms``.
    expected_samples: int = Field(0, ge=0)
    max_gap_ms: int = Field(0, ge=0)
    reason_codes: list[TurnResourceReasonCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_the_number(self) -> TurnMemoryResources:
        """Refuse a peak with no complete cycle, and a cycle count that inverts."""
        if self.status == "unavailable" and self.peak_bytes is not None:
            raise ValueError("an unavailable memory measurement cannot carry a peak")
        if self.status == "available" and self.peak_bytes is None:
            raise ValueError("an available memory measurement needs a peak")
        if self.valid_samples > self.expected_samples:
            raise ValueError("valid PSS cycles cannot exceed the planned cycles")
        return self


class TurnStorageIoResources(BaseModel):
    """Storage I/O the kernel charged to the member's own tasks.

    This is task I/O accounting, not what the host disk physically did: page
    cache, writeback, the filesystem and Kata virtiofs can all move where a
    byte is attributed. ``rchar``/``wchar`` are deliberately absent — they
    count pipes, sockets and terminals too, so they cannot stand in for
    storage I/O.
    """

    status: ResourceMetricStatus
    method: Literal["task_io_accounting"]
    read_bytes: int | None = Field(None, ge=0)
    write_bytes: int | None = Field(None, ge=0)
    reason_codes: list[TurnResourceReasonCode] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_the_numbers(self) -> TurnStorageIoResources:
        """Refuse a status its own counters contradict."""
        measured = self.read_bytes is not None or self.write_bytes is not None
        if self.status == "unavailable" and measured:
            raise ValueError("an unavailable storage measurement cannot carry counters")
        if self.status != "unavailable" and not measured:
            raise ValueError("a measured storage window needs at least one counter")
        return self


class TurnResourceWindow(BaseModel):
    """Where the measurement actually reached, on the guest's monotonic clock."""

    duration_ms: int = Field(..., ge=0)
    #: How far data reached before the window was frozen. Below ``duration_ms``
    #: this is a prefix, and the missing tail is never filled in.
    observed_through_ms: int = Field(..., ge=0)
    #: What the open and close boundary scans themselves cost. They are the
    #: measurement's own overhead, so they are reported rather than hidden.
    open_scan_ms: int = Field(0, ge=0)
    close_scan_ms: int = Field(0, ge=0)

    @model_validator(mode="after")
    def observed_cannot_exceed_the_window(self) -> TurnResourceWindow:
        """A prefix is shorter than or equal to the window it is part of."""
        if self.observed_through_ms > self.duration_ms:
            raise ValueError("an observation cannot reach past the window's end")
        return self


class WorkerTurnResourcesRequest(BaseModel):
    """One cumulative ``member_execution_window`` snapshot for one attempt.

    This is an independent report, not a variant of
    :class:`WorkerResourcesRequest`. It never writes, replaces or deletes that
    report's tool-call and process rows, and it is deliberately not bound by
    the guard that keeps ``shared-member-v1`` free of container totals: the
    numbers here are attributed per member UID, which that guard exists
    because the *container* sampler cannot do.

    The body is a snapshot, not a batch of deltas. ``report_seq`` is monotonic
    per ``measurement_id``, so a retry re-sends the same cumulative state and a
    reordered or duplicated delivery cannot accumulate.
    """

    schema_version: Literal[1] = 1
    measurement_id: str = Field(..., min_length=8, max_length=64)
    report_seq: int = Field(..., ge=1)
    state: TurnResourceState
    scope: Literal["member_execution_window"] = TURN_RESOURCE_SCOPE
    source: Literal["kata-guest-member-window-v1"] = TURN_RESOURCE_SOURCE
    window: TurnResourceWindow
    cpu: TurnCpuResources
    memory: TurnMemoryResources
    storage_io: TurnStorageIoResources

    @model_validator(mode="after")
    def a_final_report_states_a_complete_window(self) -> WorkerTurnResourcesRequest:
        """Keep ``final`` meaning what it says.

        A final report whose observation stopped short of its own window would
        render as a finished turn with a silently short tail. Such a report
        stays ``collecting`` (plus a ``window_prefix_retained`` reason code) so
        the stored shape keeps telling the truth even after the attempt ends.
        """
        if self.state == "final" and self.window.observed_through_ms < self.window.duration_ms:
            raise ValueError("a final turn resource report cannot describe a window prefix")
        return self


class TurnResourceSummary(BaseModel):
    """One attempt's whole-turn member execution window, as stored and read.

    Derived from the stored snapshot rather than recomputed: the list view, the
    detail view and any later aggregation share one reducer. ``state`` is
    derived too — a stored ``collecting`` snapshot for an attempt that is
    already terminal reads as ``interrupted``, with ``observed_through_ms``
    kept so the UI can say how far it actually got instead of rounding it up
    to the end of the task.
    """

    measurement_id: str
    state: Literal["collecting", "final", "interrupted"]
    source: str
    received_at: datetime | None = None
    window: TurnResourceWindow
    cpu: TurnCpuResources
    memory: TurnMemoryResources
    storage_io: TurnStorageIoResources


class AttemptResources(BaseModel):
    """Attempt telemetry across historical samples and current guest observations.

    Every aggregate counter is optional and ``coverage`` is not. Current shared
    member turns intentionally report ``coverage = 0`` with null aggregate
    counters, then carry per-tool timing and optional UID-filtered Kata process
    rows. A reader must render that as "turn totals unavailable", not zero use.

    ``turn_summary`` is separate from all of the above. It is the whole-turn
    member execution window: this member's own CPU and PSS peak, reported by
    the guest helper through its own endpoint. It is populated independently,
    so a turn may have a complete ``turn_summary`` with no tool rows, or tool
    rows with no ``turn_summary`` at all.
    """

    cpu_usec: int | None = None
    mem_peak_bytes: int | None = None
    mem_avg_bytes: int | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    net_rx_bytes: int | None = None
    net_tx_bytes: int | None = None
    sample_count: int = 0
    #: Fraction of the attempt window actually sampled, 0..1.
    coverage: float = 0.0
    sampler_version: str | None = None
    #: How long the turn spent inside tool calls — the union of their
    #: windows, never the sum, so it cannot exceed the turn. Null for a turn
    #: recorded before this was measured, which is not the same as zero.
    tool_time_ms: int | None = None
    #: One row per tool call this attempt issued, in the order issued.
    tool_calls: list[ToolCallResources] = Field(default_factory=list)
    #: True when the attempt issued more calls than ``tool_calls`` reports. A
    #: cap nobody is told about reads exactly like a shorter turn.
    tool_calls_truncated: bool = False
    #: Null where the sandbox was not a Kata guest or its helper was unavailable.
    process_observer: str | None = None
    #: Only true when the observer spanned the turn with no lost/truncated events.
    process_observation_complete: bool = False
    process_events_lost: int = 0
    processes_truncated: bool = False
    processes: list[ProcessObservation] = Field(default_factory=list)
    #: The whole-turn member execution window, or ``None`` for a turn recorded
    #: before it was collected. ``None`` is "not measured here", never zero.
    turn_summary: TurnResourceSummary | None = None


class ResourceMetricBrief(BaseModel):
    """One metric's status alone, for the Runs list's compact summary."""

    status: ResourceMetricStatus
    reason_codes: list[TurnResourceReasonCode] = Field(default_factory=list)


class RunResourceSummary(BaseModel):
    """The compact per-turn resource projection the Runs list shows.

    Flattened scalars only, so listing a page of runs stays one bounded query
    over exactly that page and never reads event or process tables. The detail
    view carries the full :class:`TurnResourceSummary`; this exists so the list
    does not have to.
    """

    cpu_usec: int | None = None
    memory_peak_bytes: int | None = None
    state: Literal["collecting", "final", "interrupted"]
    observed_through_ms: int | None = None
    cpu: ResourceMetricBrief
    memory: ResourceMetricBrief
    storage_io: ResourceMetricBrief
    #: How many attempts the numbers cover, and whether any of them is missing
    #: a summary. A total over four of five executions is not the run's total.
    attempts_measured: int = 0
    attempts_total: int = 0


class AgentAttemptSummary(BaseModel):
    """Identity, continuity and duration without unselected resource details."""

    attempt_id: int
    attempt_no: int
    continuity_mode: str | None = None
    context_status: Literal["new", "live", "restored", "lost"] | None = None
    duration_ms: int | None = None


class AgentAttemptTelemetry(AgentAttemptSummary):
    """Measurements fetched for one selected execution attempt."""

    resources: AttemptResources | None = None


class WorkerToolCallResources(BaseModel):
    """One tool call's slice of a closed window, reported by the runner.

    Exclusive and shared counters are mutually exclusive by construction, and
    the store holds that as a constraint: a row carrying both has lost the
    only distinction these two columns exist to preserve.
    """

    call_seq: int = Field(..., ge=1)
    tool_use_id: str = Field(..., max_length=200)
    tool_name: str = Field(..., max_length=120)
    timing_source: Literal["message", "exact"] = "message"
    duration_ms: int | None = Field(None, ge=0)
    sample_count: int = Field(0, ge=0)
    overlap_n: int = Field(1, ge=1)
    measured_span_ms: int | None = Field(None, ge=0)
    cpu_usec_exclusive: int | None = Field(None, ge=0)
    cpu_usec_shared: int | None = Field(None, ge=0)
    mem_peak_exclusive_bytes: int | None = Field(None, ge=0)
    mem_peak_shared_bytes: int | None = Field(None, ge=0)
    process_count: int = Field(0, ge=0)
    process_cpu_usec: int | None = Field(None, ge=0)
    process_mem_peak_bytes: int | None = Field(None, ge=0)
    process_io_read_bytes: int | None = Field(None, ge=0)
    process_io_write_bytes: int | None = Field(None, ge=0)
    process_resources_partial: bool = False

    @property
    def has_process_resources(self) -> bool:
        """Whether any exit-accounted counter was charged to this call."""
        return (
            self.process_cpu_usec is not None
            or self.process_mem_peak_bytes is not None
            or self.process_io_read_bytes is not None
            or self.process_io_write_bytes is not None
        )

    @model_validator(mode="after")
    def counters_match_the_window_evidence(self) -> WorkerToolCallResources:
        """A worker cannot relabel shared or unmeasured counters as exact."""
        exclusive = self.cpu_usec_exclusive is not None or self.mem_peak_exclusive_bytes is not None
        shared = self.cpu_usec_shared is not None or self.mem_peak_shared_bytes is not None
        if exclusive and shared:
            raise ValueError("a tool row cannot mix exclusive and shared counters")
        if self.overlap_n == 1 and shared:
            raise ValueError("a non-overlapping tool row cannot carry shared counters")
        if self.overlap_n > 1 and exclusive:
            raise ValueError("an overlapping tool row cannot carry exclusive counters")
        if self.sample_count < 2 and (exclusive or shared or self.measured_span_ms is not None):
            raise ValueError("fewer than two samples cannot support a tool measurement")
        if self.process_count == 0 and (
            self.has_process_resources or self.process_resources_partial
        ):
            raise ValueError("a call with no attributed process cannot carry process resources")
        return self


class WorkerProcessObservation(BaseModel):
    """One bounded guest-kernel process identity reported by the sandbox manager."""

    process_seq: int = Field(..., ge=1)
    uid: int = Field(..., ge=0)
    pid: int = Field(..., ge=1)
    tgid: int = Field(..., ge=1)
    ppid: int | None = Field(None, ge=1)
    comm: str = Field(..., min_length=1, max_length=64)
    executable: str | None = Field(None, max_length=512)
    started_offset_ms: int = Field(..., ge=0)
    duration_ms: int | None = Field(None, ge=0)
    exit_code: int | None = Field(None, ge=0, le=255)
    observed_fork: bool = False
    observed_exec: bool = False
    observed_exit: bool = False
    tool_call_seqs: list[int] = Field(default_factory=list, max_length=500)
    cpu_usec: int | None = Field(None, ge=0)
    mem_peak_bytes: int | None = Field(None, ge=0)
    io_read_bytes: int | None = Field(None, ge=0)
    io_write_bytes: int | None = Field(None, ge=0)
    io_rchar_bytes: int | None = Field(None, ge=0)
    io_wchar_bytes: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def counters_need_an_observed_exit(self) -> WorkerProcessObservation:
        """Exit-time accounting cannot describe a process that has not exited."""
        if not self.observed_exit and any(
            value is not None
            for value in (
                self.cpu_usec,
                self.mem_peak_bytes,
                self.io_read_bytes,
                self.io_write_bytes,
                self.io_rchar_bytes,
                self.io_wchar_bytes,
            )
        ):
            raise ValueError("a process that has not exited cannot carry exit counters")
        return self


class WorkerResourcesRequest(BaseModel):
    """One closed attempt's resource/timing telemetry, reported by the runner.

    Historical sampled counters are cumulative deltas, not rates. Current
    shared-member reports intentionally omit them because a container delta
    includes concurrent siblings. ``sampler_version`` identifies that semantic
    shape; ``coverage = 0`` and absent counters stay visibly unavailable while
    tool timing and guest process rows can still be present.
    """

    cpu_usec: int | None = Field(None, ge=0)
    mem_peak_bytes: int | None = Field(None, ge=0)
    mem_avg_bytes: int | None = Field(None, ge=0)
    io_read_bytes: int | None = Field(None, ge=0)
    io_write_bytes: int | None = Field(None, ge=0)
    net_rx_bytes: int | None = Field(None, ge=0)
    net_tx_bytes: int | None = Field(None, ge=0)
    sample_count: int = Field(0, ge=0)
    coverage: float = Field(0.0, ge=0.0, le=1.0)
    sampler_version: str = Field(..., max_length=64)
    #: Bounded so one turn cannot post an unbounded list; the runner caps at
    #: the same number and says so through ``tool_calls_truncated``.
    tool_calls: list[WorkerToolCallResources] = Field(default_factory=list, max_length=500)
    tool_calls_truncated: bool = False
    tool_time_ms: int | None = Field(None, ge=0)
    process_observer: Literal["kata-guest-ebpf-v1"] | None = None
    process_observation_complete: bool = False
    process_events_lost: int = Field(0, ge=0)
    processes_truncated: bool = False
    processes: list[WorkerProcessObservation] = Field(
        default_factory=list,
        max_length=4096,
    )

    @model_validator(mode="after")
    def completeness_flags_match_their_evidence(self) -> WorkerResourcesRequest:
        """Refuse combinations that would make a gap render as certainty."""
        call_seqs = [call.call_seq for call in self.tool_calls]
        if len(call_seqs) != len(set(call_seqs)):
            raise ValueError("tool call sequence numbers must be unique")
        process_seqs = [process.process_seq for process in self.processes]
        if len(process_seqs) != len(set(process_seqs)):
            raise ValueError("process sequence numbers must be unique")

        known_call_seqs = set(call_seqs)
        observed_process_counts = dict.fromkeys(known_call_seqs, 0)
        for process in self.processes:
            referenced = process.tool_call_seqs
            if len(referenced) != len(set(referenced)):
                raise ValueError("a process cannot reference the same tool call twice")
            if not set(referenced).issubset(known_call_seqs):
                raise ValueError("a process cannot reference an absent tool call")
            for call_seq in referenced:
                observed_process_counts[call_seq] += 1
        if any(
            call.process_count != observed_process_counts[call.call_seq] for call in self.tool_calls
        ):
            raise ValueError("tool process_count must match the attached process rows")

        if self.sampler_version == SHARED_MEMBER_ACCOUNTING_VERSION:
            aggregates = (
                self.cpu_usec,
                self.mem_peak_bytes,
                self.mem_avg_bytes,
                self.io_read_bytes,
                self.io_write_bytes,
                self.net_rx_bytes,
                self.net_tx_bytes,
            )
            sampled_tool_evidence = any(
                call.sample_count != 0
                or call.measured_span_ms is not None
                or call.cpu_usec_exclusive is not None
                or call.cpu_usec_shared is not None
                or call.mem_peak_exclusive_bytes is not None
                or call.mem_peak_shared_bytes is not None
                for call in self.tool_calls
            )
            if (
                any(value is not None for value in aggregates)
                or self.sample_count != 0
                or self.coverage != 0.0
                or sampled_tool_evidence
            ):
                raise ValueError(
                    "shared-member-v1 cannot attribute sampled container counters to one member"
                )
        if self.tool_calls_truncated:
            has_call_measurement = any(
                call.sample_count > 0
                or call.measured_span_ms is not None
                or call.cpu_usec_exclusive is not None
                or call.cpu_usec_shared is not None
                or call.mem_peak_exclusive_bytes is not None
                or call.mem_peak_shared_bytes is not None
                for call in self.tool_calls
            )
            if has_call_measurement or self.tool_time_ms is not None:
                raise ValueError(
                    "a truncated tool-window list cannot support attribution or a time split"
                )
        if (
            any(call.duration_ms is None for call in self.tool_calls)
            and self.tool_time_ms is not None
        ):
            raise ValueError("an unfinished tool call cannot support a complete time split")
        if self.process_observation_complete and (
            self.process_observer is None
            or self.process_events_lost != 0
            or self.processes_truncated
        ):
            raise ValueError("complete process observation contradicts its loss metadata")
        if self.process_observer is None and (
            self.processes
            or self.process_events_lost > 0
            or self.processes_truncated
            or any(
                call.process_count > 0
                or call.has_process_resources
                or call.process_resources_partial
                for call in self.tool_calls
            )
        ):
            raise ValueError("process rows require a named guest observer")
        return self
