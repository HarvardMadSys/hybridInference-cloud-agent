"""Stable API for opt-in timing of platform operations.

Producer imports continue to use this module. Implementation lives in the
``profiling`` package: ``schema`` and ``models`` define the vocabulary,
``runs`` validates descriptors, ``policy`` owns control and admission,
``measurement`` collects and freezes records, ``stages`` supplies observers,
``context`` validates process handoffs, and ``export`` drains a bounded queue.

The default-off path allocates no collector and reads no clock. Collection
keeps the experiment's original manifest until its end boundary; a Stop ends
new selection, while expiry truncates already selected work. Log and JSONL
writes happen on the export thread.

Field semantics and boundaries are specified in
``docs/agents/specs/2026-09-14-sandbox-startup-measurement-design.zh-CN.md`` and
``docs/agents/specs/2026-09-14-platform-operation-measurement-inventory.zh-CN.md``.
"""

from __future__ import annotations

import logging
import threading as threading
import time as time

from .profiling import export as _export
from .profiling.context import (
    answer_from_headers as answer_from_headers,
    attach_selection as attach_selection,
    context_call_id as context_call_id,
    expect_fragment as expect_fragment,
    handoff_context as handoff_context,
    measurement_context as measurement_context,
    parse_context as parse_context,
    start_stage_recording as start_stage_recording,
)
from .profiling.export import (
    _emit as _emit,
    _Exporter as _Exporter,
    close_sink as close_sink,
    flush_sink as flush_sink,
)
from .profiling.measurement import (
    attach_remote_stages as attach_remote_stages,
    bind as bind,
    collection_complete as collection_complete,
    discard as discard,
    finalize as finalize,
    fragment_truncated as fragment_truncated,
    frozen_record as frozen_record,
    mark as mark,
    record_fragment as record_fragment,
    set_attributes as set_attributes,
    span as span,
    start_operation as start_operation,
    to_record as to_record,
)
from .profiling.models import (
    ControlState as ControlState,
    Measurement as Measurement,
    ProfilingRun as ProfilingRun,
)
from .profiling.policy import (
    PolicyProvider as PolicyProvider,
    RuntimePolicyStore as RuntimePolicyStore,
    active_run as active_run,
    budget_state as budget_state,
    control_state as control_state,
    install_policy_provider as install_policy_provider,
    policy_provider as policy_provider,
    policy_store as policy_store,
    profile_mode as profile_mode,
    reason as reason,
    refresh_policy as refresh_policy,
    reset_cache as reset_cache,
    reset_policy as reset_policy,
)
from .profiling.runs import (
    manifest_for_measurement as manifest_for_measurement,
    manifest_for_run as manifest_for_run,
    parse_run as parse_run,
)
from .profiling.schema import (
    BOUNDARIES_BY_OPERATION as BOUNDARIES_BY_OPERATION,
    BOUNDARY_VERSION as BOUNDARY_VERSION,
    CLOCK_DOMAIN_ID as CLOCK_DOMAIN_ID,
    COVERAGE_BROWSER_INTERACTION as COVERAGE_BROWSER_INTERACTION,
    COVERAGE_BY_OPERATION as COVERAGE_BY_OPERATION,
    COVERAGE_HOST_STARTUP as COVERAGE_HOST_STARTUP,
    COVERAGE_SERVER_REQUEST as COVERAGE_SERVER_REQUEST,
    DEFAULT_PRODUCER as DEFAULT_PRODUCER,
    DEFAULT_ROOT_STAGE as DEFAULT_ROOT_STAGE,
    ENV_PROFILING_ENABLED as ENV_PROFILING_ENABLED,
    ENV_PROFILING_MODE as ENV_PROFILING_MODE,
    ENV_PROFILING_RUN as ENV_PROFILING_RUN,
    ENV_PROFILING_SINK as ENV_PROFILING_SINK,
    MAX_ATTRIBUTE_CHARS as MAX_ATTRIBUTE_CHARS,
    MAX_CONTEXT_CHARS as MAX_CONTEXT_CHARS,
    MAX_REMOTE_STAGES as MAX_REMOTE_STAGES,
    MAX_RUN_ID_CHARS as MAX_RUN_ID_CHARS,
    MAX_SELECTIONS as MAX_SELECTIONS,
    MAX_SPANS as MAX_SPANS,
    MODE_ADMIN as MODE_ADMIN,
    MODE_DISABLED as MODE_DISABLED,
    MODE_ENVIRONMENT as MODE_ENVIRONMENT,
    OUTCOMES as OUTCOMES,
    PRODUCER_INSTANCE_ID as PRODUCER_INSTANCE_ID,
    PROFILING_ANSWER_CALL_ID_HEADER as PROFILING_ANSWER_CALL_ID_HEADER,
    PROFILING_ANSWER_OPERATION_HEADER as PROFILING_ANSWER_OPERATION_HEADER,
    PROFILING_ANSWER_RECORDED_HEADER as PROFILING_ANSWER_RECORDED_HEADER,
    PROFILING_ANSWER_STAGES_HEADER as PROFILING_ANSWER_STAGES_HEADER,
    PROFILING_ANSWER_TRUNCATED_HEADER as PROFILING_ANSWER_TRUNCATED_HEADER,
    PROFILING_CONTEXT_HEADER as PROFILING_CONTEXT_HEADER,
    PROFILING_MODES as PROFILING_MODES,
    RUN_ID_PATTERN as RUN_ID_PATTERN,
    SCHEMA_VERSION as SCHEMA_VERSION,
    SELECTION_TTL_S as SELECTION_TTL_S,
    SINK_QUEUE_MAX as SINK_QUEUE_MAX,
    SOURCE_ADMIN as SOURCE_ADMIN,
    SOURCE_ENVIRONMENT as SOURCE_ENVIRONMENT,
    STARTUP_OPERATION as STARTUP_OPERATION,
    STARTUP_OUTCOME_ALIAS as STARTUP_OUTCOME_ALIAS,
    STOP_REASON_EXPIRED as STOP_REASON_EXPIRED,
    STOP_REASON_STOPPED as STOP_REASON_STOPPED,
    STOP_REASON_UNAVAILABLE as STOP_REASON_UNAVAILABLE,
    TARGET_KEYS as TARGET_KEYS,
)
from .profiling.stages import (
    StageObserver as StageObserver,
    StageRecorder as StageRecorder,
    attributed_stage as attributed_stage,
    stage_observer as stage_observer,
    substage as substage,
)

logger = logging.getLogger(__name__)


def finish(
    measurement: Measurement | None,
    *,
    outcome: str,
    failure_stage: str | None = None,
) -> None:
    """Emit the measurement. Safe to call twice and safe on ``None``.

    Idempotent in both directions: a measurement already finalized at its end
    boundary is emitted from those frozen values, and one that never called
    :func:`finalize` is finalized here -- so a caller that forgets cannot make
    a record and a response disagree, it can only move the boundary earlier.
    """
    if measurement is None or measurement.finished:
        return
    finalize(measurement, outcome)
    outcome = measurement.frozen_outcome
    measurement.finished = True
    # The record about to be emitted is what the reserved slot was for, so
    # `discard` can no longer return it.
    measurement.reserved = False
    if not measurement.selected:
        return
    if measurement.run_target and not measurement.bound:
        # A scoped run reports only attempts whose scope was proved; emitting
        # an unbound measurement would claim a sample the run never selected.
        logger.warning(
            "operation profiling dropped an unbound measurement (run %s, operation %s)",
            measurement.run_id,
            measurement.operation,
        )
        return
    record = frozen_record(measurement, outcome=outcome, failure_stage=failure_stage)
    if record is not None:
        _emit(record)


def __getattr__(name: str) -> _Exporter | None:
    """Expose the live exporter handle for existing process diagnostics."""
    if name == "_EXPORTER":
        return _export._EXPORTER
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BOUNDARIES_BY_OPERATION",
    "BOUNDARY_VERSION",
    "CLOCK_DOMAIN_ID",
    "COVERAGE_BROWSER_INTERACTION",
    "COVERAGE_BY_OPERATION",
    "COVERAGE_HOST_STARTUP",
    "COVERAGE_SERVER_REQUEST",
    "DEFAULT_PRODUCER",
    "DEFAULT_ROOT_STAGE",
    "ENV_PROFILING_ENABLED",
    "ENV_PROFILING_MODE",
    "ENV_PROFILING_RUN",
    "ENV_PROFILING_SINK",
    "MAX_CONTEXT_CHARS",
    "MAX_REMOTE_STAGES",
    "MAX_RUN_ID_CHARS",
    "MAX_SELECTIONS",
    "MAX_SPANS",
    "MODE_ADMIN",
    "MODE_DISABLED",
    "MODE_ENVIRONMENT",
    "OUTCOMES",
    "PRODUCER_INSTANCE_ID",
    "PROFILING_ANSWER_CALL_ID_HEADER",
    "PROFILING_ANSWER_OPERATION_HEADER",
    "PROFILING_ANSWER_RECORDED_HEADER",
    "PROFILING_ANSWER_STAGES_HEADER",
    "PROFILING_ANSWER_TRUNCATED_HEADER",
    "PROFILING_CONTEXT_HEADER",
    "PROFILING_MODES",
    "RUN_ID_PATTERN",
    "SCHEMA_VERSION",
    "SELECTION_TTL_S",
    "SINK_QUEUE_MAX",
    "SOURCE_ADMIN",
    "SOURCE_ENVIRONMENT",
    "STARTUP_OPERATION",
    "STARTUP_OUTCOME_ALIAS",
    "STOP_REASON_EXPIRED",
    "STOP_REASON_STOPPED",
    "STOP_REASON_UNAVAILABLE",
    "TARGET_KEYS",
    "ControlState",
    "Measurement",
    "PolicyProvider",
    "ProfilingRun",
    "RuntimePolicyStore",
    "StageObserver",
    "StageRecorder",
    "active_run",
    "answer_from_headers",
    "attach_remote_stages",
    "attach_selection",
    "attributed_stage",
    "bind",
    "budget_state",
    "close_sink",
    "collection_complete",
    "context_call_id",
    "control_state",
    "discard",
    "expect_fragment",
    "finalize",
    "finish",
    "flush_sink",
    "fragment_truncated",
    "frozen_record",
    "handoff_context",
    "install_policy_provider",
    "manifest_for_measurement",
    "manifest_for_run",
    "mark",
    "measurement_context",
    "parse_context",
    "parse_run",
    "policy_provider",
    "policy_store",
    "profile_mode",
    "reason",
    "record_fragment",
    "refresh_policy",
    "reset_cache",
    "reset_policy",
    "set_attributes",
    "span",
    "stage_observer",
    "start_operation",
    "start_stage_recording",
    "substage",
    "to_record",
]
