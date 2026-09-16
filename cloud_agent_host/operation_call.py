"""One broker call's measurement fragment, and the answer it returns.

The broker contributes *durations* to a selection the control plane made. It
never decides on its own that an operation is worth measuring: it sees one call
at a time and cannot tell which chat or user it belongs to. So the selection
arrives with the request, is validated against this process's own switch, run
and deadline, and is answered per call through :class:`BrokerCall`.

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

from cloud_agent_host.operation_profiling import (
    PROFILING_ANSWER_CALL_ID_HEADER,
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

logger = logging.getLogger(__name__)

#: Producer name the broker writes into a fragment it contributes.
PRODUCER = "broker"

#: The headers a broker answers in, for the one route whose answer cannot
#: travel in a body. Name-bound here so the broker route reads as the contract
#: it is; the wire names themselves stay in the shared collector.
RESPONSE_CALL_ID_HEADER = PROFILING_ANSWER_CALL_ID_HEADER
RESPONSE_RECORDED_HEADER = PROFILING_ANSWER_RECORDED_HEADER
RESPONSE_TRUNCATED_HEADER = PROFILING_ANSWER_TRUNCATED_HEADER
RESPONSE_STAGES_HEADER = PROFILING_ANSWER_STAGES_HEADER

#: Longest encoded stage list a broker will put in a header. Bounded because a
#: header has no framing to recover from and an oversized one is dropped rather
#: than sent.
MAX_STAGES_HEADER_CHARS = 2048


@dataclass
class BrokerCall:
    """The measurement fragment for one broker call.

    An instance always exists -- :func:`closed` answers for a request that
    carried no usable selection -- so a route can time its work without
    branching on whether an experiment is running.
    """

    recorder: StageRecorder | None = None
    call_id: str | None = None

    @property
    def recording(self) -> bool:
        """Return whether this call is collecting anything."""
        return self.recorder is not None

    def stage(self, name: str) -> AbstractContextManager[None]:
        """Time one sub-stage of this call."""
        if self.recorder is None:
            return nullcontext()
        return substage(self.recorder, name)

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
UNSELECTED = BrokerCall()


def open_call(context: Any, *, operation: str) -> BrokerCall:
    """Validate an inherited selection and open a fragment for one call.

    ``operation`` is what this route measures, so a selection carried over from
    a different operation is refused rather than reported under the wrong name.
    The run's own operation list is still what decides: this only stops a
    fragment being filed under a name this route does not own.
    """
    parsed = parse_context(context)
    if parsed is None:
        return UNSELECTED
    if operation and str(parsed.get("operation") or "") not in {"", operation}:
        return UNSELECTED
    recorder = start_stage_recording(parsed, producer=PRODUCER)
    if recorder is None:
        return UNSELECTED
    return BrokerCall(recorder=recorder, call_id=context_call_id(parsed))


def closed() -> BrokerCall:
    """Return a call that is not collecting."""
    return UNSELECTED


__all__ = [
    "MAX_STAGES_HEADER_CHARS",
    "PRODUCER",
    "RESPONSE_CALL_ID_HEADER",
    "RESPONSE_RECORDED_HEADER",
    "RESPONSE_STAGES_HEADER",
    "RESPONSE_TRUNCATED_HEADER",
    "UNSELECTED",
    "BrokerCall",
    "answer_from_headers",
    "closed",
    "open_call",
]
