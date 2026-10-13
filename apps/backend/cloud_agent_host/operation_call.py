"""One sandbox manager call's measurement fragment, and the answer it returns.

The sandbox manager contributes *durations* to a selection the control plane made. It
never decides on its own that an operation is worth measuring: it sees one call
at a time and cannot tell which chat or user it belongs to. So the selection
arrives with the request, is validated against this process's own switch, run
and deadline, and is answered per call through :class:`SandboxManagerCall`.

Two rules shape everything here:

* **A malformed selection disables collection, never the call.** Every failure
  to parse or validate answers "not collecting" and leaves the file read or the
  terminal keystroke to behave exactly as it did before this module existed.
* **An answer is about collecting, not about how much was collected.** A call
  that legitimately has no stage to time -- a directory that was already warm, a
  terminal that only needed reattaching -- still answers, with no stages. Only
  that way can a caller tell a normal skip apart from a producer that never
  reported at all.
"""

from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from cloud_agent_shared.operation_profiling import (
    PROFILING_ANSWER_CALL_ID_HEADER,
    PROFILING_ANSWER_OPERATION_HEADER,
    PROFILING_ANSWER_RECORDED_HEADER,
    PROFILING_ANSWER_STAGES_HEADER,
    PROFILING_ANSWER_TRUNCATED_HEADER,
    StageRecorder,
    answer_from_headers,
    context_call_id,
    parse_context,
    start_stage_recording,
    substage,
)
from cloud_agent_shared.sandbox_manager_compat import SANDBOX_MANAGER_PRODUCER

logger = logging.getLogger(__name__)

#: Producer name the sandbox manager writes into a fragment it contributes.
PRODUCER = SANDBOX_MANAGER_PRODUCER

#: The headers a sandbox manager answers in, for the one route whose answer cannot
#: travel in a body. Name-bound here so the sandbox manager route reads as the contract
#: it is; the wire names themselves stay in the shared collector.
RESPONSE_CALL_ID_HEADER = PROFILING_ANSWER_CALL_ID_HEADER
RESPONSE_RECORDED_HEADER = PROFILING_ANSWER_RECORDED_HEADER
RESPONSE_TRUNCATED_HEADER = PROFILING_ANSWER_TRUNCATED_HEADER
RESPONSE_STAGES_HEADER = PROFILING_ANSWER_STAGES_HEADER
RESPONSE_OPERATION_HEADER = PROFILING_ANSWER_OPERATION_HEADER

#: Longest encoded stage list a sandbox manager will put in a header. Bounded because a
#: header has no framing to recover from and an oversized one is dropped rather
#: than sent.
MAX_STAGES_HEADER_CHARS = 2048


@dataclass
class SandboxManagerCall:
    """The measurement fragment for one sandbox manager call.

    An instance always exists -- :func:`closed` answers for a request that
    carried no usable selection -- so a route can time its work without
    branching on whether an experiment is running.
    """

    recorder: StageRecorder | None = None
    call_id: str | None = None
    #: Whether this route filed the call itself, once its result was known.
    #: Distinct from the recorder's operation, which may already have been
    #: settled by the context: only a decision made *here* is an answer about
    #: what the call turned out to be.
    decided: bool = False

    @property
    def recording(self) -> bool:
        """Return whether this call is collecting anything."""
        return self.recorder is not None

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage of this call."""
        if self.recorder is None:
            return nullcontext()
        return substage(self.recorder, name)

    def set_operation(self, operation: str) -> bool:
        """File this call under the operation its result turned out to be.

        A file path is collected before its kind is known, so the operation is
        decided here, once the response exists. The answer says which one,
        because it is the caller's run that decides whether that operation is
        worth reporting at all.

        Returns whether it was accepted. A refused operation leaves the call
        unnamed, so the answer reports no operation at all rather than one this
        run does not select.
        """
        if self.recorder is None:
            return False
        accepted = self.recorder.set_operation(operation)
        if accepted:
            self.decided = True
        return accepted

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return ``body`` with this call's answer merged into it.

        The answer says this process finished collecting for this call. It is
        never omitted when a selection was honoured, even if no stage was timed,
        because the caller has to be able to tell "collected nothing" from
        "never reported".
        """
        if self.recorder is None:
            return body
        body["profiling_call_id"] = self.call_id
        body["profiling_recorded"] = True
        if self.decided:
            body["profiling_operation"] = self.recorder.operation
        if self.recorder.stopped_early():
            body["profiling_truncated"] = True
        stages = [dict(item) for item in self.recorder.stages]
        if stages:
            body["stages"] = stages
        return body

    def headers(self, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return response headers answering this call, for a streaming route.

        A stream's body is the terminal's own bytes, so its answer travels in
        headers. An unselected call contributes nothing and the response is
        exactly what it was before.
        """
        headers = dict(extra or {})
        if self.recorder is None or not self.call_id:
            return headers
        headers[RESPONSE_CALL_ID_HEADER] = self.call_id
        headers[RESPONSE_RECORDED_HEADER] = "1"
        if self.decided:
            headers[RESPONSE_OPERATION_HEADER] = self.recorder.operation
        if self.recorder.stopped_early():
            headers[RESPONSE_TRUNCATED_HEADER] = "1"
        if self.recorder.stages:
            try:
                encoded = json.dumps(self.recorder.stages, separators=(",", ":"), default=str)
            except (TypeError, ValueError):  # pragma: no cover - stages are plain data
                encoded = ""
            if encoded and len(encoded) <= MAX_STAGES_HEADER_CHARS:
                headers[RESPONSE_STAGES_HEADER] = encoded
        return headers


#: One shared instance, so the unselected path allocates nothing per request.
UNSELECTED = SandboxManagerCall()


def parse_operations(operations: Any) -> frozenset[str]:
    """Return the operation names a context offers, bounded and de-duplicated."""
    if isinstance(operations, str):
        return frozenset({operations.strip()}) if operations.strip() else frozenset()
    if isinstance(operations, (list, tuple, set, frozenset)):
        named = frozenset(str(item).strip() for item in operations)
        return frozenset(item for item in named if item)
    return frozenset()


def open_call(context: Any, *, operations: Any) -> SandboxManagerCall:
    """Validate an inherited selection and open a fragment for one call.

    ``operations`` are the operations this route may file the call under. A
    selection carried over from a different one is refused rather than reported
    under the wrong name, and when more than one is offered -- a file path is
    collected before its kind is known -- the route files the call itself with
    :meth:`SandboxManagerCall.set_operation` once its response exists. The run's own
    operation list still decides; this only stops a fragment being filed under
    a name this route does not own.
    """
    parsed = parse_context(context)
    if parsed is None:
        return UNSELECTED
    offered = parse_operations(operations)
    named = str(parsed.get("operation") or "").strip()
    if named and named not in offered:
        return UNSELECTED
    recorder = start_stage_recording(parsed, producer=PRODUCER)
    if recorder is None:
        return UNSELECTED
    if recorder.operation and recorder.operation not in offered:
        # The selection is for an operation this route does not serve. The
        # context's own list is not enough: the route states what it offers,
        # and a fragment must belong to a call that was actually made here.
        return UNSELECTED
    call = SandboxManagerCall(recorder=recorder, call_id=context_call_id(parsed))
    if not recorder.operation and len(recorder.allowed) == 1 and named in offered:
        # The context already named exactly one operation this run selects, so
        # there is nothing left for this route to decide after the call: filing
        # it now is the same answer, stated once.
        call.set_operation(named)
    return call


def closed() -> SandboxManagerCall:
    """Return a call that is not collecting."""
    return UNSELECTED


__all__ = [
    "MAX_STAGES_HEADER_CHARS",
    "PRODUCER",
    "RESPONSE_CALL_ID_HEADER",
    "RESPONSE_OPERATION_HEADER",
    "RESPONSE_RECORDED_HEADER",
    "RESPONSE_STAGES_HEADER",
    "RESPONSE_TRUNCATED_HEADER",
    "UNSELECTED",
    "SandboxManagerCall",
    "answer_from_headers",
    "closed",
    "open_call",
    "parse_operations",
]
