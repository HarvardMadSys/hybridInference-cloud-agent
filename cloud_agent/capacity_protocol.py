"""The host-capacity block: its wire shape, and what the admin page may trust.

A runner reports CPU, memory, the capacity of the filesystem holding sandbox
writable layers, and its retained-container count as an **optional**
``capacity`` block on the heartbeat it already sends. Everything in this module
exists to keep that optionality real in both directions:

* **Outward**, the block is a closed vocabulary. Fixed keys, fixed state map,
  fixed missing-measurement reasons, a version number, a hard size ceiling. An
  8 KiB ceiling is not decoration — this block rides a liveness message, and a
  liveness message that can be made arbitrarily expensive is a way to take the
  pool down without touching a job.
* **Inward**, nothing here can refuse a heartbeat. A block that is absent,
  oversized, of an unknown version or simply wrong is dropped, logged, and the
  heartbeat proceeds. The control plane's job on that endpoint is to learn that
  a machine is alive; refusing to learn it because a monitoring field was
  malformed would trade a missing graph for a missing host.

The read side is the same data with the server's own accounting added: when the
observation arrived, when it was taken, and whether it is still fresh enough to
render as a current value. Those three are computed here rather than in the
route so that the pool response, the history response and their tests all agree
on what "12 seconds old" means.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from cloud_agent_contracts.host_capacity import (
    CPU_MISSING_COUNTER_RESET,
    CPU_MISSING_INVALID_SAMPLE,
    CPU_MISSING_SOURCE_UNAVAILABLE,
    CPU_MISSING_WARMING_UP,
    MEMORY_MISSING_INVALID_SAMPLE,
    MEMORY_MISSING_SOURCE_UNAVAILABLE,
    OTHER_STATES,
    SCHEMA_VERSION,
    SOURCE,
    STORAGE_MISSING_DISABLED,
    STORAGE_MISSING_SOURCE_UNAVAILABLE,
    STORAGE_MISSING_UNSUPPORTED,
    STORAGE_MISSING_UNVERIFIED,
    memory_usage,
    storage_bytes_in_range,
    storage_usage,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

#: The largest capacity block this service will look at, as UTF-8 bytes of its
#: JSON. The block carries counts and scalars; nothing legitimate approaches
#: this, and the ceiling is what stops a hostile runner from making every
#: heartbeat a memory event.
MAX_CAPACITY_BYTES = 8 * 1024

#: Older than this and a stored observation is shown as stale rather than as a
#: current value. Three heartbeat intervals, matching the pool's own
#: missed-beat threshold: a host that has stopped reporting is not a host whose
#: last reading should keep being drawn in green.
FRESH_AFTER_SECONDS = 90.0

#: How old an observation may be when it is reported and still be written into
#: history. Three heartbeat intervals: a runner whose sampler has been stalled
#: for minutes is still reporting a real measurement, and it is still taken as
#: the current reading — where the page shows its age beside it — but a curve
#: has no way to say how late a point was, and a gap is the honest drawing of a
#: minute nothing arrived for.
CAPACITY_HISTORY_MAX_AGE_S = 90.0

#: History retention. The page serves at most 24 hours, so anything older is
#: deleted rather than merely hidden.
CAPACITY_HISTORY_RETENTION_S = 24 * 60 * 60.0

#: One historical point per observation minute, and the windows the API serves.
HISTORY_RESOLUTION_SECONDS = 60
HISTORY_RANGES: dict[str, int] = {"1h": 3600, "6h": 21600, "24h": 86400}
DEFAULT_HISTORY_RANGE = "1h"

#: A 24-hour window at one point per minute needs 1441 buckets; the ceiling is
#: what keeps a hand-built query string from asking for a year of them.
MAX_HISTORY_POINTS = 1500

#: JSON-safe integer bound. Memory figures are byte counts, and a value beyond
#: this cannot survive a JavaScript client unchanged, so it is refused rather
#: than silently rounded somewhere the reader cannot see.
_MAX_JSON_INT = 2**53 - 1

#: No C0 control byte and no DEL. A namespace is an identifier, and the one
#: that matters here is U+0000: PostgreSQL cannot encode it inside JSONB, so a
#: block carrying it is refused by the database rather than by this service —
#: which, without this rule, is a failure that takes the heartbeat's own write
#: down with it.
_NO_CONTROL_CHARACTERS = r"^[^\x00-\x1f\x7f]*$"

CpuMissingReason = Literal[
    "warming_up",
    "counter_reset",
    "source_unavailable",
    "invalid_sample",
]
MemoryMissingReason = Literal["source_unavailable", "invalid_sample"]
StorageMissingReason = Literal[
    "disabled",
    "unsupported",
    "unverified",
    "source_unavailable",
    "invalid_sample",
]

#: The two reasons the control plane produces rather than relays. ``invalid_sample``
#: is written here when a storage sub-block arrived but did not validate;
#: ``not_reported`` is what a runner that has no storage capability at all reads
#: as — including every snapshot stored before this field existed.
STORAGE_MISSING_INVALID_SAMPLE: Final = "invalid_sample"
STORAGE_MISSING_NOT_REPORTED: Final = "not_reported"
StorageAdminMissingReason = Literal[
    "disabled",
    "unsupported",
    "unverified",
    "source_unavailable",
    "invalid_sample",
    "not_reported",
]

# A Literal has to spell its members, so the runner's constants and this type
# are two statements of one fact. Tied together here rather than left to drift:
# a reason the runner can report and this type rejects would fail validation on
# a heartbeat the runner considers valid.
assert set(get_args(CpuMissingReason)) == {
    CPU_MISSING_WARMING_UP,
    CPU_MISSING_COUNTER_RESET,
    CPU_MISSING_SOURCE_UNAVAILABLE,
    CPU_MISSING_INVALID_SAMPLE,
}
assert set(get_args(MemoryMissingReason)) == {
    MEMORY_MISSING_SOURCE_UNAVAILABLE,
    MEMORY_MISSING_INVALID_SAMPLE,
}
assert set(get_args(StorageMissingReason)) == {
    STORAGE_MISSING_DISABLED,
    STORAGE_MISSING_UNSUPPORTED,
    STORAGE_MISSING_UNVERIFIED,
    STORAGE_MISSING_SOURCE_UNAVAILABLE,
    STORAGE_MISSING_INVALID_SAMPLE,
}
assert set(get_args(StorageAdminMissingReason)) == set(get_args(StorageMissingReason)) | {
    STORAGE_MISSING_NOT_REPORTED
}

#: The vocabulary a runner's ``missing.storage`` may name, as a set. The
#: validator below checks membership itself rather than letting the field's
#: `Literal` do it, because a `Literal` failure is a *model* failure: it would
#: take the CPU, the memory and the inventory down with a storage reason this
#: control plane happens not to know.
STORAGE_MISSING_REASONS = frozenset(get_args(StorageMissingReason))
InventoryStatus = Literal["available", "partial", "unavailable"]
Freshness = Literal["fresh", "stale", "unavailable"]


class HostCapacityInventory(BaseModel):
    """Container entities one runner counted on its own Docker host.

    Counts are of **containers**, not of chat memberships: a Fork adds a member
    to a group that already holds one container, so counting memberships would
    report a machine as busier every time somebody branched a conversation.
    """

    namespace: str | None = Field(
        None,
        max_length=255,
        pattern=_NO_CONTROL_CHARACTERS,
        description=(
            "Deployment namespace the counts were scoped to. Never empty for "
            "a counted run, and never carrying a control byte: this string is "
            "stored as JSONB, which cannot represent U+0000 at all, and a "
            "monitoring field must not be able to fail the write that carries "
            "the liveness signal."
        ),
    )
    status: InventoryStatus = Field(
        ...,
        description=(
            "available: every in-scope container was counted. partial: the "
            "count is a lower bound, and reason says why. unavailable: no "
            "count was established, so the numbers are null rather than zero."
        ),
    )
    running: int | None = Field(None, ge=0, le=1_000_000)
    stopped: int | None = Field(None, ge=0, le=1_000_000)
    other_states: dict[str, int | None] | None = Field(
        None,
        description=(
            "Fixed-key breakdown of every Docker state outside running and "
            "exited. A paused container is here, not in stopped: it has not "
            "released its memory."
        ),
    )
    reason: str | None = Field(
        None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Bounded identifier explaining a partial or unavailable status.",
    )
    unscoped_legacy_containers: int | None = Field(
        None,
        ge=0,
        le=1_000_000,
        description=(
            "Chat containers carrying no namespace label. They cannot be "
            "attributed to a deployment, so they are counted in no total."
        ),
    )
    duplicate_sandbox_id: bool = Field(
        False,
        description=(
            "One sandbox id was seen on more than one container. Both are "
            "counted — both hold resources — and the anomaly is surfaced."
        ),
    )

    @field_validator("other_states")
    @classmethod
    def _normalize_states(cls, value: dict[str, int | None] | None) -> dict[str, int | None] | None:
        """Keep only the fixed state vocabulary, in a stable order."""
        if value is None:
            return None
        unknown = sorted(set(value) - set(OTHER_STATES))
        if unknown:
            raise ValueError(f"unknown container states: {unknown}")
        return {name: value.get(name) for name in OTHER_STATES}

    @model_validator(mode="after")
    def _counts_match_status(self) -> HostCapacityInventory:
        """Require counts exactly where the status promises them."""
        if self.status == "unavailable":
            if self.running is not None or self.stopped is not None:
                raise ValueError("an unavailable inventory reports no counts")
        elif self.status == "available":
            if self.running is None or self.stopped is None:
                raise ValueError("an available inventory reports running and stopped")
            if self.other_states is None or any(
                count is None for count in self.other_states.values()
            ):
                raise ValueError("an available inventory reports every state")
        elif not self.reason:
            # A partial count without its reason is the dangerous shape: it
            # looks like a total, and the page has nothing to qualify it with.
            raise ValueError("a partial inventory says why it is partial")
        return self


class HostCapacityMissing(BaseModel):
    """Why each measurement is absent, independently of the others.

    ``storage`` is absent as well as null on a runner that predates the
    sub-block: silence about a capability that does not exist is not the same
    statement as a runner that has the capability and reported a gap, and the
    admin view is where those two are told apart (``not_reported``).
    """

    cpu: CpuMissingReason | None = None
    memory: MemoryMissingReason | None = None
    storage: StorageMissingReason | None = None


class HostCapacityStorage(BaseModel):
    """The filesystem that holds sandbox writable layers, as one runner read it.

    Block space in the ``df`` sense: ``total`` is the filesystem's size,
    ``free`` includes the blocks reserved for root, ``available`` excludes
    them. All three are kept because the percentage is taken over
    ``used + available`` — collapsing ``free`` into ``total - available`` would
    report every healthy filesystem as partly full with blocks nobody wrote.

    Strict integers on purpose: a JSON ``true`` or ``"1024"`` coerced to a
    number would put a figure on an operator's screen that no machine ever
    measured, and the coercion is invisible by the time it is rendered.
    """

    source: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_]{1,64}$",
        description=(
            "Identifier of the method that produced the reading, e.g. "
            "'linux_overlay_root_statvfs'. Deliberately not an enum: a new "
            "probe must not require a control-plane release, and the page "
            "never branches on this value."
        ),
    )
    target: str | None = Field(
        None,
        max_length=256,
        pattern=_NO_CONTROL_CHARACTERS,
        description=(
            "Human-readable label for the filesystem measured — the host mount "
            "root the deployment attested, not the Docker data root, which can "
            "be a different disk. Tooltip only; no logic reads it."
        ),
    )
    total_bytes: StrictInt = Field(..., ge=0, le=_MAX_JSON_INT)
    free_bytes: StrictInt = Field(..., ge=0, le=_MAX_JSON_INT)
    available_bytes: StrictInt = Field(..., ge=0, le=_MAX_JSON_INT)

    @model_validator(mode="after")
    def _bytes_are_ordered(self) -> HostCapacityStorage:
        """Require the one relation a real filesystem always satisfies."""
        if not storage_bytes_in_range(
            total_bytes=self.total_bytes,
            free_bytes=self.free_bytes,
            available_bytes=self.available_bytes,
        ):
            raise ValueError("storage bytes are outside available <= free <= total")
        return self


class HostCapacityReport(BaseModel):
    """One host-capacity observation, exactly as a runner reports it.

    Every rule here is about not letting an unavailable measurement be mistaken
    for a real one. A null CPU percentage carries the reason it is null; a
    missing ``MemAvailable`` produces no memory figure at all rather than
    falling back to ``MemFree``, which answers a different question; and a host
    whose boot id could not be read reports neither CPU nor memory, because
    without it two machines' numbers are indistinguishable.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    sample_id: str = Field(
        ...,
        min_length=36,
        max_length=36,
        description="Identity of this observation. A replay of the same sample never refreshes.",
    )
    sample_age_ms: int = Field(
        ...,
        ge=0,
        le=300_000,
        description=(
            "How old the observation was when it was sent, by the runner's "
            "monotonic clock. The control plane derives the sampled instant "
            "from it and its own receipt time; it never trusts a runner clock."
        ),
    )
    linux_boot_id: str | None = Field(
        None,
        min_length=36,
        max_length=36,
        description="Boot id of the Docker host, or null when it could not be read.",
    )
    source: Literal["linux_host_proc_and_docker_list"] = Field(
        SOURCE,
        description="How the observation was taken. A different method is a different source.",
    )
    cpu_percent: float | None = Field(
        None,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        description=(
            "Non-idle share of the interval across every CPU, including steal "
            "and excluding iowait. Not multiplied by the CPU count."
        ),
    )
    cpu_logical_count: int | None = Field(None, ge=1, le=65_536)
    cpu_window_seconds: float | None = Field(
        None,
        gt=0.0,
        le=90.0,
        description="Length of the interval the CPU percentage averages.",
    )
    memory_total_bytes: int | None = Field(None, gt=0, le=_MAX_JSON_INT)
    memory_available_bytes: int | None = Field(None, ge=0, le=_MAX_JSON_INT)
    storage: HostCapacityStorage | None = Field(
        None,
        description=(
            "Capacity of the filesystem holding sandbox writable layers. Null "
            "means no reading was published this round, and missing.storage "
            "says why. Validated locally: a wrong sub-block costs this field, "
            "never the CPU and memory beside it."
        ),
    )
    inventory: HostCapacityInventory
    missing: HostCapacityMissing

    @field_validator("sample_id", "linux_boot_id")
    @classmethod
    def _canonical_uuid(cls, value: str | None) -> str | None:
        """Require a real UUID, normalized to its canonical spelling.

        Both identifiers are compared for equality — one to reject a replayed
        sample, the other to decide whether two reports describe the same boot
        — so anything that is not canonical would compare unequal to itself.
        """
        if value is None:
            return None
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("not a UUID") from exc

    @model_validator(mode="before")
    @classmethod
    def _degrade_unusable_storage(cls, data: Any) -> Any:
        """Turn any storage-shaped fault into an explicit missing sample.

        The storage half is validated *here* rather than by the model's own
        field validation, and the difference is what an operator sees.
        Rejecting the whole report over one bad sub-block would cost that host
        its CPU, its memory and its container count — three independent facts
        about a machine that is, after all, still alive and still claiming
        work.

        Every way storage can be wrong is caught here, not just an unparsable
        number, because a fault the model's own validation sees is a fault that
        takes the whole block with it:

        * a sub-block that does not validate;
        * a sub-block and a ``missing.storage`` at the same time — the two
          statements contradict each other, and the block cannot say which is
          true;
        * a ``missing.storage`` outside the closed vocabulary. A newer runner
          inventing a reason must not cost this control plane the CPU figures
          it does understand; the page would show the unknown string as
          "Not measured" either way.

        All three become ``storage = null`` plus ``missing.storage =
        "invalid_sample"``: a runner that sent something wrong is a different
        thing from a runner that cannot measure at all, and the page says so.

        The storage field is also the limit of what this hook owns. A block
        whose ``missing`` map is itself absent or is not an object has nothing
        wrong with its *storage*; it is missing a required field, and ordinary
        field validation drops it exactly as it drops the same block with a
        perfectly good storage sub-block. Substituting a map of this control
        plane's own invention would make whether the report is accepted at all
        depend on whether its storage half happened to be malformed.
        """
        if not isinstance(data, dict):
            return data
        storage = data.get("storage")
        missing = data.get("missing")
        reason: Any = None
        if isinstance(missing, dict):
            reason = missing.get("storage")
        elif isinstance(missing, HostCapacityMissing):
            reason = missing.storage
        if storage is None:
            # Nothing to publish: either the runner said nothing about storage
            # at all — an older runner — or it named a gap from the closed
            # vocabulary. Anything else is the fault handled below.
            if reason is None or (isinstance(reason, str) and reason in STORAGE_MISSING_REASONS):
                return data
        else:
            try:
                HostCapacityStorage.model_validate(storage)
            except ValidationError:
                pass
            else:
                if reason is None:
                    return data
        if isinstance(missing, dict):
            degraded = dict(missing)
        elif isinstance(missing, HostCapacityMissing):
            degraded = missing.model_dump()
        else:
            # Nothing about the storage field is repairable from here: there is
            # no map to write the reason into. ``missing`` is required, so the
            # block is dropped by ordinary field validation, and it is left
            # untouched so that it is dropped for that reason alone.
            return data
        degraded["storage"] = STORAGE_MISSING_INVALID_SAMPLE
        return {**data, "storage": None, "missing": degraded}

    @model_validator(mode="after")
    def _measurements_agree_with_their_reasons(self) -> HostCapacityReport:
        """Check the fields against the reasons the runner gave for them."""
        if (self.cpu_percent is None) != (self.missing.cpu is not None):
            raise ValueError("cpu_percent and missing.cpu disagree")
        if (self.memory_total_bytes is None) != (self.missing.memory is not None):
            raise ValueError("memory_total_bytes and missing.memory disagree")
        if self.storage is not None and self.missing.storage is not None:
            raise ValueError("a storage sample and missing.storage disagree")
        if self.memory_total_bytes is None and self.memory_available_bytes is not None:
            raise ValueError("memory_available_bytes without memory_total_bytes")
        if (
            self.memory_total_bytes is not None
            and self.memory_available_bytes is not None
            and self.memory_available_bytes > self.memory_total_bytes
        ):
            raise ValueError("memory_available_bytes exceeds memory_total_bytes")
        if self.cpu_percent is not None and self.cpu_window_seconds is None:
            raise ValueError("cpu_percent without its sampling window")
        if self.linux_boot_id is None and (
            self.cpu_percent is not None
            or self.cpu_logical_count is not None
            or self.memory_total_bytes is not None
        ):
            raise ValueError("host measurements without a boot id")
        return self


def parse_capacity_block(raw: object) -> HostCapacityReport | None:
    """Validate an optional capacity block, or decide to discard it.

    Called before the heartbeat model validates, so that a block this control
    plane cannot use becomes *absent* rather than a 422. Losing a monitoring
    field is a gap in a graph; losing the heartbeat is a host that looks dead
    while it is working.

    Args:
        raw: The value the request carried under ``capacity``.

    Returns:
        The validated block, or ``None`` when it must be dropped: absent, not
        an object, too large, an unknown version, or internally inconsistent.
        A storage sub-block that fails validation does not drop the block —
        it is replaced by ``missing.storage = "invalid_sample"`` so the CPU,
        the memory and the inventory beside it survive.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _log_dropped("not_an_object", detail=type(raw).__name__)
        return None
    try:
        encoded = json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        _log_dropped("not_serializable")
        return None
    if len(encoded) > MAX_CAPACITY_BYTES:
        _log_dropped("too_large", detail=str(len(encoded)))
        return None
    try:
        return HostCapacityReport.model_validate(raw)
    except ValidationError as exc:
        first: Mapping[str, Any] = exc.errors()[0] if exc.errors() else {}
        _log_dropped(
            "invalid",
            detail=f"{'.'.join(str(part) for part in first.get('loc', ()))}:{first.get('type')}",
        )
        return None


def _log_dropped(reason: str, *, detail: str | None = None) -> None:
    """Record a discarded capacity block without recording its contents."""
    logger.warning(
        "agent_host_capacity_block_dropped",
        extra={
            "event": "agent_host_capacity_block_dropped",
            "capacity_drop_reason": reason,
            "capacity_drop_detail": detail,
        },
    )


class AgentHostCapacity(BaseModel):
    """A stored observation as the admin page reads it.

    The runner's own fields, plus the server's accounting: when the block
    arrived, when the observation was taken, how old that makes it now, and
    whether it is fresh enough to render as a current value. ``freshness`` is
    ``unavailable`` only in the sense the page renders when this object is
    absent altogether — a host that has never sent a usable block.
    """

    schema_version: int
    source: str
    sample_id: str
    linux_boot_id: str | None = None
    cpu_percent: float | None = None
    cpu_logical_count: int | None = None
    cpu_window_seconds: float | None = None
    cpu_missing_reason: str | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    memory_used_bytes: int | None = None
    memory_used_percent: float | None = None
    memory_missing_reason: str | None = None
    storage_total_bytes: int | None = None
    storage_free_bytes: int | None = None
    storage_available_bytes: int | None = None
    storage_used_bytes: int | None = None
    storage_used_percent: float | None = None
    storage_source: str | None = None
    storage_target: str | None = None
    storage_missing_reason: StorageAdminMissingReason | None = None
    inventory: HostCapacityInventory | None = None
    received_at: datetime
    sampled_at: datetime
    sample_age_seconds: float
    freshness: Freshness


def admin_capacity_view(
    *,
    snapshot: object,
    received_at: datetime | None,
    sampled_at: datetime | None,
    now: datetime,
) -> AgentHostCapacity | None:
    """Turn a stored snapshot into the admin view, or ``None``.

    The stored JSON is re-validated rather than trusted. One corrupt row must
    cost that host its capacity numbers and nothing else: the page an operator
    opens to fix a bad host is the page that must not 500 on it.

    Args:
        snapshot: The ``capacity_snapshot`` column, already decoded.
        received_at: When the control plane accepted the block.
        sampled_at: When the observation was taken, by the control plane's
            clock.
        now: The database's time for this read.

    Returns:
        The view, or ``None`` when there is no usable stored observation.
    """
    if snapshot is None or received_at is None or sampled_at is None:
        return None
    try:
        report = HostCapacityReport.model_validate(snapshot)
    except ValidationError:
        logger.warning(
            "agent_host_capacity_snapshot_unusable",
            extra={"event": "agent_host_capacity_snapshot_unusable"},
        )
        return None
    usage = memory_usage(
        total_bytes=report.memory_total_bytes, available_bytes=report.memory_available_bytes
    )
    storage = report.storage
    storage_figures = (
        None
        if storage is None
        else storage_usage(
            total_bytes=storage.total_bytes,
            free_bytes=storage.free_bytes,
            available_bytes=storage.available_bytes,
        )
    )
    if storage is not None and storage_figures is None:
        # Present and well-formed enough to store, but with no denominator to
        # take a percentage of. That is a bad sample, not a missing one, and
        # the page says so rather than drawing a 0% filesystem.
        storage = None
        storage_missing_reason: StorageAdminMissingReason | None = STORAGE_MISSING_INVALID_SAMPLE
    elif storage is None:
        # A runner that reported a gap keeps its own reason; one that has no
        # storage capability at all — including every snapshot written before
        # the field existed — reads as not reported.
        storage_missing_reason = report.missing.storage or STORAGE_MISSING_NOT_REPORTED
    else:
        storage_missing_reason = None
    age = max(0.0, (_as_utc(now) - _as_utc(sampled_at)).total_seconds())
    return AgentHostCapacity(
        schema_version=report.schema_version,
        source=report.source,
        sample_id=report.sample_id,
        linux_boot_id=report.linux_boot_id,
        cpu_percent=report.cpu_percent,
        cpu_logical_count=report.cpu_logical_count,
        cpu_window_seconds=report.cpu_window_seconds,
        cpu_missing_reason=report.missing.cpu,
        memory_total_bytes=report.memory_total_bytes,
        memory_available_bytes=report.memory_available_bytes,
        memory_used_bytes=None if usage is None else usage.used_bytes,
        memory_used_percent=None if usage is None else usage.used_percent,
        memory_missing_reason=report.missing.memory,
        storage_total_bytes=None if storage is None else storage.total_bytes,
        storage_free_bytes=None if storage is None else storage.free_bytes,
        storage_available_bytes=None if storage is None else storage.available_bytes,
        storage_used_bytes=None if storage_figures is None else storage_figures.used_bytes,
        storage_used_percent=None if storage_figures is None else storage_figures.used_percent,
        storage_source=None if storage is None else storage.source,
        storage_target=None if storage is None else storage.target,
        storage_missing_reason=storage_missing_reason,
        inventory=report.inventory,
        received_at=received_at,
        sampled_at=sampled_at,
        sample_age_seconds=age,
        freshness="fresh" if age <= FRESH_AFTER_SECONDS else "stale",
    )


def _as_utc(moment: datetime) -> datetime:
    """Read a datetime as UTC, treating a naive one as already UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


class AgentHostCapacityPoint(BaseModel):
    """One minute of the history: an observation, or an explicit gap.

    A bucket with no observation carries nulls rather than zeroes, and that is
    the whole point of the shape — a machine that was measured at 3% and a
    machine nobody measured are different facts, and a zero would draw the
    second one as the first.
    """

    bucket_at: datetime
    sampled_at: datetime | None = None
    linux_boot_id: str | None = None
    cpu_percent: float | None = None
    cpu_logical_count: int | None = None
    cpu_window_seconds: float | None = None
    cpu_missing_reason: str | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    memory_used_bytes: int | None = None
    memory_used_percent: float | None = None
    memory_missing_reason: str | None = None


class AgentHostCapacityHistoryResponse(BaseModel):
    """The stored history of one enrollment's current capacity epoch."""

    host: str
    capacity_epoch: str | None = Field(
        None,
        description=(
            "The enrollment generation these points belong to. A re-enrollment "
            "rotates it, which is how a client knows to drop an older curve."
        ),
    )
    since: datetime
    until: datetime
    observed_at: datetime = Field(
        ..., description="When the control plane read this window, by its own clock."
    )
    resolution_seconds: int = HISTORY_RESOLUTION_SECONDS
    semantics: Literal["sampled_observation"] = Field(
        default="sampled_observation",
        description=(
            "Each point is one observation per minute, not a continuous "
            "average and not a peak. A CPU point averages the interval before "
            "it; a memory point is instantaneous."
        ),
    )
    points: list[AgentHostCapacityPoint] = Field(default_factory=list)


def resolve_history_range(value: str) -> int | None:
    """Return the seconds a supported range names, or ``None``.

    Args:
        value: The requested range.

    Returns:
        The window length, or ``None`` when the range is not one this API
        serves. Callers answer ``None`` with 422 rather than substituting a
        default, because a client that asked for a year and got an hour would
        draw a chart whose axis disagrees with its label.
    """
    return HISTORY_RANGES.get(value)


def history_point_grid(
    *,
    rows: Sequence[Mapping[str, Any]],
    since: datetime,
    until: datetime,
    resolution_seconds: int = HISTORY_RESOLUTION_SECONDS,
) -> list[AgentHostCapacityPoint]:
    """Expand stored rows into one point per bucket, gaps included.

    The grid is generated on the read side rather than stored, so a missing
    minute stays visibly missing instead of being interpolated across. Nothing
    here carries a value forward: a machine that was not measured has no
    reading, and inventing one would hide exactly the outage the chart exists
    to show.

    Args:
        rows: Stored points, each keyed by ``bucket_at`` and the scalar
            columns.
        since: First bucket of the window, inclusive.
        until: Last bucket of the window, inclusive.
        resolution_seconds: Bucket width.

    Returns:
        One point per bucket from ``since`` to ``until``. Rows outside the
        window are ignored, and at most :data:`MAX_HISTORY_POINTS` are
        returned.
    """
    by_bucket = {_as_utc(row["bucket_at"]): row for row in rows}
    step = timedelta(seconds=resolution_seconds)
    points: list[AgentHostCapacityPoint] = []
    cursor = _as_utc(since)
    last = _as_utc(until)
    while cursor <= last and len(points) < MAX_HISTORY_POINTS:
        row = by_bucket.get(cursor)
        points.append(_point_from_row(cursor, row))
        cursor += step
    return points


def _point_from_row(bucket_at: datetime, row: Mapping[str, Any] | None) -> AgentHostCapacityPoint:
    """Build one history point, with nulls for a bucket nobody observed.

    The row comes straight from asyncpg, which hands a ``UUID`` column back as
    a ``UUID`` object and not as text — so the boot id is normalized here
    rather than left for the response model, which expects a string and would
    otherwise refuse the whole window. The same conversion the write path uses
    is applied in both directions, so an identifier cannot be canonical on the
    way in and different on the way out.
    """
    if row is None:
        return AgentHostCapacityPoint(bucket_at=bucket_at)
    usage = memory_usage(
        total_bytes=row.get("memory_total_bytes"), available_bytes=row.get("memory_available_bytes")
    )
    return AgentHostCapacityPoint(
        bucket_at=bucket_at,
        sampled_at=row.get("sampled_at"),
        linux_boot_id=_uuid_text(row.get("linux_boot_id")),
        cpu_percent=row.get("cpu_percent"),
        cpu_logical_count=row.get("cpu_logical_count"),
        cpu_window_seconds=row.get("cpu_window_seconds"),
        cpu_missing_reason=row.get("cpu_missing_reason"),
        memory_total_bytes=row.get("memory_total_bytes"),
        memory_available_bytes=row.get("memory_available_bytes"),
        memory_used_bytes=None if usage is None else usage.used_bytes,
        memory_used_percent=None if usage is None else usage.used_percent,
        memory_missing_reason=row.get("memory_missing_reason"),
    )


#: The names a caller of this protocol module may rely on.
__all__ = [
    "AgentHostCapacity",
    "AgentHostCapacityHistoryResponse",
    "AgentHostCapacityPoint",
    "CapacityHistoryPointWrite",
    "HostCapacityInventory",
    "HostCapacityMissing",
    "HostCapacityReport",
    "HostCapacityStorage",
    "admin_capacity_view",
    "bucket_for",
    "history_point_grid",
    "history_point_write_from_block",
    "parse_capacity_block",
    "resolve_history_range",
]


@dataclass(frozen=True)
class CapacityHistoryPointWrite:
    """One observation destined for the bounded history table.

    Scalar fields only. The 8 KiB heartbeat block is deliberately not copied
    into history: a 24-hour window is 1,441 rows per host, and storing the
    whole document for each of them would make the chart the largest table in
    the database.
    """

    bucket_at: datetime
    sampled_at: datetime
    sample_id: str
    linux_boot_id: str | None
    cpu_percent: float | None
    cpu_logical_count: int | None
    cpu_window_seconds: float | None
    cpu_missing_reason: str | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    memory_missing_reason: str | None


def bucket_for(sampled_at: datetime) -> datetime:
    """Return the UTC minute a sampled instant belongs to.

    Args:
        sampled_at: The instant the observation was taken.

    Returns:
        That instant floored to the minute, in UTC. The bucket is derived from
        the *observation* and not from the write, so a delayed report lands in
        the minute it measured rather than in the minute it arrived.
    """
    return _as_utc(sampled_at).replace(second=0, microsecond=0)


def history_point_write_from_block(
    block: Mapping[str, Any], *, sampled_at: datetime, sample_id: str
) -> CapacityHistoryPointWrite | None:
    """Convert one accepted capacity block into an insertable history point.

    Every bound below repeats one ``HostCapacityReport`` already enforced, and
    the repetition is the point: the constraints that matter here are the
    history table's own, and a value outside them has to become ``NULL``
    rather than an exception. The statement this runs beside is the one
    keeping a live machine's reading, and a number nobody can store must cost
    its own field and nothing else.

    Args:
        block: The capacity block as it was stored, already validated.
        sampled_at: The instant the accepting write stamped the snapshot with.
            Taken from that write rather than re-derived here, so the point and
            the snapshot are provably the same observation.
        sample_id: The sample identity that write stored, for the same reason.

    Returns:
        The point, or ``None`` when the identity is unusable: an instant that
        is not one, or a sample id that is not a UUID. Both are impossible from
        the write path and neither is worth raising over.
    """
    if not isinstance(sampled_at, datetime):
        return None
    identity = _uuid_text(sample_id)
    if identity is None:
        return None
    missing = block.get("missing")
    reasons: Mapping[str, Any] = missing if isinstance(missing, dict) else {}
    total_bytes = _bounded_int(block.get("memory_total_bytes"), low=1)
    available_bytes = _bounded_int(block.get("memory_available_bytes"), low=0)
    # The table stores both memory figures or neither, so a pair that is half
    # present or self-contradictory becomes no memory reading at all. Dropping
    # the pair here rather than letting the CHECK refuse it is what keeps the
    # CPU half of the same observation.
    if total_bytes is None or available_bytes is None or available_bytes > total_bytes:
        total_bytes, available_bytes = None, None
    return CapacityHistoryPointWrite(
        bucket_at=bucket_for(sampled_at),
        sampled_at=sampled_at,
        sample_id=identity,
        linux_boot_id=_uuid_text(block.get("linux_boot_id")),
        cpu_percent=_bounded_float(block.get("cpu_percent"), low=0.0, high=100.0),
        cpu_logical_count=_bounded_int(block.get("cpu_logical_count"), low=1),
        cpu_window_seconds=_bounded_float(
            block.get("cpu_window_seconds"), low=0.0, high=90.0, strict_low=True
        ),
        cpu_missing_reason=_bounded_reason(reasons.get("cpu")),
        memory_total_bytes=total_bytes,
        memory_available_bytes=available_bytes,
        memory_missing_reason=_bounded_reason(reasons.get("memory")),
    )


def _uuid_text(value: object) -> str | None:
    """Return a canonical UUID string, or ``None`` for anything else."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _bounded_float(
    value: object, *, low: float, high: float, strict_low: bool = False
) -> float | None:
    """Parse a float inside a range, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < low or number > high or (strict_low and number <= low):
        return None
    return number


def _bounded_int(value: object, *, low: int) -> int | None:
    """Parse an integer at or above a floor, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number >= low else None


def _bounded_reason(value: object) -> str | None:
    """Return a missing-measurement reason if it fits the stored vocabulary."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > 64 or not trimmed.replace("_", "").isalnum():
        return None
    return trimmed


def history_window(*, now: datetime, seconds: int) -> tuple[datetime, datetime]:
    """Return the aligned window a history request reads.

    Args:
        now: The database's clock for this request.
        seconds: Window length.

    Returns:
        ``(since, until)``, both floored to the minute and anchored to the
        server's time. The current partial minute is excluded: its observation
        may not have been recorded yet, and a trailing empty point would read
        as a gap that is really just the present.
    """
    until = _as_utc(now).replace(second=0, microsecond=0)
    return until - timedelta(seconds=seconds), until
