"""DNA bands: the items each request carried, in order, keyed by identity.

Adapted from bowenliang123/dsh-context ``src/client/dna.ts`` (Apache-2.0,
commit 32982aa6aa2c70fbda6b81281de98a72ea625001): a request is drawn as the
stacked items it carries, keyed so two requests can be compared item by
item. There every item is one the Harness assembled. Here the items are the
stored content the reconstruction assigns to the request
(:func:`cloud_agent.context_request.walk_composition`): turn prompts and
stored events, each a recorded estimate, keyed by the stored event's id (a
prompt by its turn). CLI system prompts and tool schemas are not items.

Bands are read for a window of requests at a time (the trend's visible
bars), from one bounded read, never for the whole history at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_request import RequestScope, walk_composition

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.context_request import LocatedRequest

#: Requests one DNA read covers.
MAX_DNA_REQUESTS = 60
#: Items one request's bands list; past it, the earliest are folded into one.
MAX_DNA_ITEMS = 600


class DnaRequest(BaseModel):
    """One request's items, in transcript order: ``[key, category, tokens]``."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    event_id: int
    items: list[tuple[str, str, int]]
    #: Earliest items folded together past ``MAX_DNA_ITEMS`` (their tokens).
    folded_items: int = Field(default=0, ge=0)
    folded_tokens: int = Field(default=0, ge=0)


class DnaBands(BaseModel):
    """DNA bands for a window of requests; recorded estimates throughout."""

    model_config = ConfigDict(extra="forbid")

    basis: str = "stored_events_transcript_order"
    requests: list[DnaRequest]
    #: The bounded event read stopped early: earlier items may be missing.
    events_capped: bool = False


def item_key(entry: Any) -> str:
    """A stored event's id, or the turn a prompt opens."""
    return f"e{entry.event_id}" if entry.event_id is not None else f"p{entry.job_id}"


def bands_for(
    located: LocatedRequest,
    *,
    prompts: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> DnaRequest:
    """One request's items from rows read for a wider window."""
    carried = located.carried_attempts
    point = located.point
    ranked = []
    for row in events:
        owner = carried.get(int(row["attempt_id"]))
        # Rows were read for the window's last request: this request's own
        # attempt stops at this request, as its single read would.
        later = int(row["attempt_id"]) == point.attempt_id and int(row.get("seq") or 0) > point.seq
        if owner is not None and not later:
            ranked.append(
                {**row, "job_id": owner[0], "turn_no": owner[1], "attempt_rank": owner[2]}
            )
    ranked.sort(key=lambda row: (row["turn_no"], row["attempt_rank"], int(row.get("seq") or 0)))
    tool_names = {
        str(row["call_id"]): str(row.get("name") or "")
        for row in ranked
        if row.get("event_type") == "tool_use" and row.get("call_id")
    }
    scope = RequestScope(
        attempt_id=point.attempt_id,
        seq=point.seq,
        usage_id=point.usage_id,
        previous_report_seq=located.previous_report_seq,
        report_after_response=located.report_after_response,
        boundary_key=located.boundary_key,
    )
    turns = [row for row in prompts if str(row["job_id"]) in set(located.carried_jobs)]
    walk = walk_composition(
        scope=scope, turns=turns, events=ranked, superseded={}, tool_names=tool_names
    )
    items = [(item_key(entry), entry.category, entry.tokens) for entry in walk.entries]
    folded = items[: max(0, len(items) - MAX_DNA_ITEMS)]
    return DnaRequest(
        index=point.index,
        event_id=int(point.event_id or 0),
        items=items[len(folded) :],
        folded_items=len(folded),
        folded_tokens=sum(tokens for _, _, tokens in folded),
    )
