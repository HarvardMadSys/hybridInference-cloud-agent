"""Which reasoning levels a model on an owner's connection takes, from its provider's word.

Pure rules, no I/O: ``application.model_connection_efforts`` reads the
listings; here is how a listing is read, how the facts are stored, and which
levels a (connection, model, runtime) triple is offered.

**Only a provider's own declaration counts.** Two documented formats are read,
each for the API family it describes:

- the Anthropic Models API (``GET /v1/models``) answers, per model,
  ``capabilities.effort``: ``supported`` and one ``{"supported": bool}`` per
  level (``low``, ``medium``, ``high``, ``xhigh`` -- which may be ``null`` --
  and ``max``). It describes Anthropic Messages' ``output_config.effort``.
- an OpenAI-style ``GET /models`` may carry, per model, the ``reasoning``
  object OpenRouter documents: ``supported_efforts`` lists the levels;
  ``null`` means every level that API accepts; an object without the key
  means the model exposes no effort selection, and ``mandatory`` means
  ``none`` must not be sent. It describes the OpenAI-shaped parameter of both
  OpenAI APIs at that Base URL: Chat Completions' ``reasoning_effort`` and
  Responses' ``reasoning.effort``.

Anything else -- a listing that is missing, refused or unreadable, a model it
does not list, an entry that declares nothing, a connection whose facts were
never read -- is ``unknown``. Unknown is not "unsupported": it offers only
Auto, and says why, so the owner can refresh once their provider declares it.
No level is inferred from a model's name, assumed for every model, or taken
from an HTTP request having been accepted: a provider may accept a parameter
and ignore it.

A runtime is offered the declared levels its pinned CLI can put on the wire
(``cloud_agent_shared.runtime_catalog``), for the *upstream* protocol of its
plan -- on a translated plan, Chat Completions -- in the runtime's own order,
weakest first. Nothing is renamed or rounded to a neighbour on the way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.model_connections import (
    ANTHROPIC_MESSAGES,
    CHAT_COMPLETIONS,
    PROTOCOLS,
    RESPONSES,
)
from cloud_agent_shared.runtime_catalog import runtime_reasoning_efforts

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_shared.model_connections import ProtocolPlan

#: The provider declared these levels.
EFFORT_SUPPORTED = "supported"
#: The provider declared that the model takes no level.
EFFORT_UNSUPPORTED = "unsupported"
#: Nothing the provider said settles it.
EFFORT_UNKNOWN = "unknown"
EFFORT_STATUSES = (EFFORT_SUPPORTED, EFFORT_UNSUPPORTED, EFFORT_UNKNOWN)

#: Where a fact came from.
SOURCE_ANTHROPIC = "anthropic_models_api"
SOURCE_OPENAI_LISTING = "models_listing_reasoning"

#: Why a fact is ``unknown`` (or why a declared model offers an agent nothing).
REASON_NOT_READ = "not_read"
REASON_LISTING_UNAVAILABLE = "listing_unavailable"
REASON_NOT_LISTED = "not_listed"
REASON_NOT_DECLARED = "not_declared"
REASON_AGENT_CANNOT_EXPRESS = "agent_cannot_express"

#: The levels the Anthropic Models API describes, in its own words.
ANTHROPIC_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: What ``supported_efforts: null`` stands for: every value the OpenAI-shaped
#: parameter takes, as OpenRouter documents it.
OPENAI_LISTING_ALL_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
#: Which listing describes which API.
_ANTHROPIC_PROTOCOLS = (ANTHROPIC_MESSAGES,)
_OPENAI_PROTOCOLS = (CHAT_COMPLETIONS, RESPONSES)
#: A level is a short lower-case word; anything else in a listing is ignored.
_MAX_LEVEL_CHARS = 16


def _level(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= _MAX_LEVEL_CHARS and value.isascii():
        word = value.strip().lower()
        if word.isalpha():
            return word
    return None


def fact(
    status: str, *, levels: list[str] | None = None, source: str = "", reason: str = ""
) -> dict[str, Any]:
    """One stored fact: ``{status, levels, source, reason}``."""
    return {"status": status, "levels": list(levels or []), "source": source, "reason": reason}


def unknown(reason: str, *, source: str = "") -> dict[str, Any]:
    """A fact that settles nothing, with why."""
    return fact(EFFORT_UNKNOWN, source=source, reason=reason)


def _entries(listing: object) -> dict[str, dict[str, Any]]:
    """A listing's ``data`` entries by model id; empty when it has none."""
    data = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(data, list):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in data:
        model = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(model, str) and model not in entries:
            entries[model] = entry
    return entries


def anthropic_effort(entry: dict[str, Any]) -> dict[str, Any]:
    """What one Anthropic Models API entry declares about ``output_config.effort``."""
    capabilities = entry.get("capabilities")
    effort = capabilities.get("effort") if isinstance(capabilities, dict) else None
    if not isinstance(effort, dict) or not isinstance(effort.get("supported"), bool):
        return unknown(REASON_NOT_DECLARED, source=SOURCE_ANTHROPIC)
    if not effort["supported"]:
        return fact(EFFORT_UNSUPPORTED, source=SOURCE_ANTHROPIC)
    levels = [
        level
        for level in ANTHROPIC_LEVELS
        if isinstance(effort.get(level), dict) and effort[level].get("supported") is True
    ]
    if not levels:
        # "Supported", with no level marked: nothing an owner could pick.
        return unknown(REASON_NOT_DECLARED, source=SOURCE_ANTHROPIC)
    return fact(EFFORT_SUPPORTED, levels=levels, source=SOURCE_ANTHROPIC)


def listing_effort(entry: dict[str, Any]) -> dict[str, Any]:
    """What one OpenAI-style ``/models`` entry's ``reasoning`` object declares."""
    reasoning = entry.get("reasoning")
    if not isinstance(reasoning, dict):
        return unknown(REASON_NOT_DECLARED, source=SOURCE_OPENAI_LISTING)
    if "supported_efforts" not in reasoning:
        return fact(EFFORT_UNSUPPORTED, source=SOURCE_OPENAI_LISTING)
    declared = reasoning["supported_efforts"]
    if declared is None:
        levels = list(OPENAI_LISTING_ALL_LEVELS)
    elif isinstance(declared, list):
        levels = []
        for value in declared:
            level = _level(value)
            if level is not None and level not in levels:
                levels.append(level)
    else:
        return unknown(REASON_NOT_DECLARED, source=SOURCE_OPENAI_LISTING)
    if reasoning.get("mandatory") is True and "none" in levels:
        # The model always reasons and refuses ``none``.
        levels.remove("none")
    if not levels:
        return fact(EFFORT_UNSUPPORTED, source=SOURCE_OPENAI_LISTING)
    return fact(EFFORT_SUPPORTED, levels=levels, source=SOURCE_OPENAI_LISTING)


@dataclass(frozen=True)
class ListingRead:
    """One listing's outcome: its parsed body, or the category it failed with."""

    body: object = None
    failure: str = ""


def _from_listing(
    read: ListingRead,
    model: str,
    source: str,
    declared: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    if read.failure or read.body is None:
        return unknown(REASON_LISTING_UNAVAILABLE, source=source)
    entry = _entries(read.body).get(model)
    if entry is None:
        return unknown(REASON_NOT_LISTED, source=source)
    return declared(entry)


def effort_facts(
    models: list[str], *, anthropic: ListingRead, openai: ListingRead
) -> dict[str, dict[str, dict[str, Any]]]:
    """Every model's facts, per API, from the two listings' outcomes."""
    facts: dict[str, dict[str, dict[str, Any]]] = {}
    for model in models:
        per_protocol: dict[str, dict[str, Any]] = {}
        messages = _from_listing(anthropic, model, SOURCE_ANTHROPIC, anthropic_effort)
        listed = _from_listing(openai, model, SOURCE_OPENAI_LISTING, listing_effort)
        for protocol in _ANTHROPIC_PROTOCOLS:
            per_protocol[protocol] = messages
        for protocol in _OPENAI_PROTOCOLS:
            per_protocol[protocol] = dict(listed)
        facts[model] = per_protocol
    return facts


def stored_fact(entry: dict[str, Any] | None, protocol: str) -> dict[str, Any]:
    """The stored fact for ``protocol`` in one model's check entry, validated.

    An entry written before effort facts existed, or anything malformed, is
    ``unknown`` / ``not_read``.
    """
    efforts = entry.get("efforts") if isinstance(entry, dict) else None
    raw = efforts.get(protocol) if isinstance(efforts, dict) else None
    if not isinstance(raw, dict) or raw.get("status") not in EFFORT_STATUSES:
        return unknown(REASON_NOT_READ)
    declared = raw.get("levels")
    levels: list[Any] = declared if isinstance(declared, list) else []
    clean = [level for level in (_level(value) for value in levels) if level is not None]
    status = str(raw["status"])
    if status == EFFORT_SUPPORTED and not clean:
        return unknown(REASON_NOT_DECLARED, source=str(raw.get("source") or ""))
    return fact(
        status,
        levels=clean if status == EFFORT_SUPPORTED else [],
        source=str(raw.get("source") or ""),
        reason=str(raw.get("reason") or ""),
    )


@dataclass(frozen=True)
class EffortOffer:
    """What a composer offers for one (connection, model, runtime), and why."""

    status: str
    #: Selectable levels, weakest first, in the runtime's vocabulary. Empty
    #: unless ``status`` is ``supported``. Auto is always available besides.
    levels: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The ``/config`` form."""
        return {
            "status": self.status,
            "levels": list(self.levels),
            "reason": self.reason,
            "source": self.source,
        }


def effort_offer(entry: dict[str, Any] | None, *, runtime: str, plan: ProtocolPlan) -> EffortOffer:
    """The levels ``runtime`` may run a model at on ``plan``, from that model's check entry.

    The declared levels of the plan's upstream API, narrowed to what the
    runtime's pinned CLI expresses, in the runtime's order.
    """
    declared = stored_fact(entry, plan.upstream_protocol)
    if declared["status"] != EFFORT_SUPPORTED:
        return EffortOffer(declared["status"], reason=declared["reason"], source=declared["source"])
    vocabulary = runtime_reasoning_efforts().get(runtime, [])
    levels = [level for level in vocabulary if level in declared["levels"]]
    if not levels:
        return EffortOffer(
            EFFORT_UNSUPPORTED, reason=REASON_AGENT_CANNOT_EXPRESS, source=declared["source"]
        )
    return EffortOffer(EFFORT_SUPPORTED, levels=levels, source=declared["source"])


def facts_view(entry: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Every API's stored fact for one model, for its owner's settings page."""
    return {protocol: stored_fact(entry, protocol) for protocol in PROTOCOLS}


__all__ = [
    "ANTHROPIC_LEVELS",
    "EFFORT_STATUSES",
    "EFFORT_SUPPORTED",
    "EFFORT_UNKNOWN",
    "EFFORT_UNSUPPORTED",
    "OPENAI_LISTING_ALL_LEVELS",
    "REASON_AGENT_CANNOT_EXPRESS",
    "REASON_LISTING_UNAVAILABLE",
    "REASON_NOT_DECLARED",
    "REASON_NOT_LISTED",
    "REASON_NOT_READ",
    "SOURCE_ANTHROPIC",
    "SOURCE_OPENAI_LISTING",
    "EffortOffer",
    "ListingRead",
    "anthropic_effort",
    "effort_facts",
    "effort_offer",
    "facts_view",
    "listing_effort",
    "stored_fact",
    "unknown",
]
