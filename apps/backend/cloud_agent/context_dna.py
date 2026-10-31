"""DNA bands: the items each request carried, in order, keyed by identity.

Adapted from bowenliang123/dsh-context ``src/client/dna.ts`` (Apache-2.0,
commit 32982aa6aa2c70fbda6b81281de98a72ea625001): a request is drawn as the
stacked items it carries, keyed so two requests can be compared item by
item. There every item is one the Harness assembled. Here the items are the
stored content the reconstruction assigns to the request (the selection of
:func:`cloud_agent.context_request.walk_composition`, by the same helpers,
without building a composition entry per item): turn prompts and
stored events, each a recorded estimate, keyed by the stored event's id (a
prompt by its turn). CLI system prompts and tool schemas are not items.

Bands are read for a window of requests at a time (the trend's visible
bars), from one bounded read, never for the whole history at once. Each
row's category and size are worked out once per window, not per request.
"""

from __future__ import annotations

from operator import itemgetter
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from cloud_agent.context_request import RequestScope, event_category, row_reason, row_tokens

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloud_agent.context_request import LocatedRequest

#: Requests one DNA read covers.
MAX_DNA_REQUESTS = 60
#: Items one request's bands list; past it, the earliest are folded into one.
#: The trend is 168 px tall, so a column never shows more bands than this.
MAX_DNA_ITEMS = 200


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


#: A content row with its category and estimated size, worked out once.
PreparedRow = tuple["Mapping[str, Any]", str, int]


def prepare_rows(events: Sequence[Mapping[str, Any]]) -> list[PreparedRow]:
    """The window's content rows, each categorized and sized once for every request."""
    prepared: list[PreparedRow] = []
    for row in events:
        category = event_category(row)
        if category is not None:
            prepared.append((row, category, row_tokens(row)))
    return prepared


def bands_for(
    located: LocatedRequest,
    *,
    prompts: Sequence[Mapping[str, Any]],
    rows: Sequence[PreparedRow],
) -> DnaRequest:
    """One request's items, from rows prepared for a wider window.

    The same selection as :func:`walk_composition`: the carried turns'
    prompts, then the carried attempts' content in transcript order, less
    what was compacted away, belongs to subagents or is this request's own
    output.
    """
    carried = located.carried_attempts
    point = located.point
    scope = RequestScope(
        attempt_id=point.attempt_id,
        seq=point.seq,
        usage_id=point.usage_id,
        previous_report_seq=located.previous_report_seq,
        report_after_response=located.report_after_response,
        boundary_key=located.boundary_key,
    )
    items: list[tuple[str, str, int]] = []
    jobs = set(located.carried_jobs)
    for turn in prompts:
        if str(turn["job_id"]) not in jobs:
            continue
        if scope.boundary_key is not None and (int(turn["turn_no"]), -1, 0) < scope.boundary_key:
            continue
        items.append((f"p{turn['job_id']}", "user_messages", row_tokens(turn)))
    selected: list[tuple[tuple[int, int, int], str, str, int]] = []
    for row, category, tokens in rows:
        owner = carried.get(int(row["attempt_id"]))
        seq = int(row.get("seq") or 0)
        # Rows were read for the window's last request: this request's own
        # attempt stops at this request, as its single read would.
        if owner is None or (int(row["attempt_id"]) == point.attempt_id and seq > point.seq):
            continue
        key = (owner[1], owner[2], seq)
        if row_reason(row, key, scope) is None:
            selected.append((key, f"e{row.get('id')}", category, tokens))
    selected.sort(key=itemgetter(0))
    items.extend((name, category, tokens) for _, name, category, tokens in selected)
    folded = items[: max(0, len(items) - MAX_DNA_ITEMS)]
    return DnaRequest(
        index=point.index,
        event_id=int(point.event_id or 0),
        items=items[len(folded) :],
        folded_items=len(folded),
        folded_tokens=sum(tokens for _, _, tokens in folded),
    )
