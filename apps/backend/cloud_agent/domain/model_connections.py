"""Owner model connections: what a connection may hold and when a turn may use one.

Pure rules, no I/O. The address checks that need a resolver live in
``application.model_connections``; here is what the owner typed, normalized,
and the one predicate every path that hands a connection to a turn asks.

**Which APIs a model is reached on is per model, and only verified.** A model
runs only on the native APIs its own verification at the connection's current
``config_revision`` showed streaming a tool round trip (``model_checks``). The
save that lists a model, or changes the key or Base URL, verifies it first and
refuses to save a model no agent could use, so a saved model always carries
its facts; one without them (never verified, or verified against an earlier
key or Base URL) runs on nothing. Codex and Claude Code reach a model whose
only verified API is Chat Completions through the member's compatibility
helper; that path is derived here, never stored. There is no manual
declaration of APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from cloud_agent_shared.model_connections import (
    CONNECTION_ID,
    MAX_BASE_URL_CHARS,
    MAX_MODELS,
    MODEL_ID,
    PROTOCOLS,
    ProtocolPlan,
    runtime_plans,
    runtime_protocol,
)

MAX_NAME_CHARS = 64

UnusableReason = Literal[
    "not_found",
    "disabled",
    "deleted",
    "model_not_listed",
    "runtime_unsupported",
    "model_unverified",
    "protocol_unsupported",
    "effort_unsupported",
    "changed",
    "feature_disabled",
]

#: What the owner is told when a turn cannot run on its connection. Each names
#: the owner's own action that would fix it; none falls back to anything.
UNUSABLE_MESSAGES: dict[str, str] = {
    "not_found": "That model connection does not exist.",
    "disabled": "This task's model connection is disabled. Enable it, or choose another model.",
    "deleted": "This task's model connection was deleted. Choose another model.",
    "model_not_listed": "That model is not listed on its connection. Add it, or choose another.",
    "runtime_unsupported": "This agent cannot run on a model connection.",
    "model_unverified": (
        "This model has not been verified on its connection yet, or the connection's key or "
        "Base URL changed since. Verify it in Settings, or choose another model."
    ),
    "protocol_unsupported": (
        "This model's verification found no way for this agent to use it. Verify it again, "
        "or choose another agent or model."
    ),
    "effort_unsupported": (
        "Reasoning effort cannot be set for a model connection; leave it on Auto."
    ),
    "feature_disabled": (
        "This deployment has not turned on model connections. Contact the deployment administrator."
    ),
    "changed": (
        "This task's model connection changed while the turn was starting. "
        "The turn did not run on the old key."
    ),
}


class ModelConnectionInvalid(ValueError):
    """What the owner typed is not a connection this platform can hold."""


class ModelConnectionConflict(Exception):
    """The owner already has a live connection with that name."""


class ModelConnectionAccessEnded(Exception):
    """The local owner no longer has Agent access when a verified save commits."""


class ModelConnectionUnusable(Exception):
    """A turn cannot run on the connection it names, for a reason its owner can act on."""

    def __init__(self, reason: UnusableReason) -> None:
        """Carry the reason; the message is the owner-facing sentence for it."""
        super().__init__(UNUSABLE_MESSAGES[reason])
        self.reason: UnusableReason = reason


def clean_name(value: str) -> str:
    """A display name: trimmed, one line, bounded."""
    name = " ".join(value.split())
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        raise ModelConnectionInvalid(f"A name is 1 to {MAX_NAME_CHARS} characters.")
    return name


def clean_base_url(value: str) -> str:
    """The Base URL as the sandbox CLI and the check will both use it.

    HTTPS on the default port only: a sandbox reaches public TCP 443 and the key
    must not travel in the clear. No credentials, query or fragment, since a
    CLI appends its own path (``/chat/completions``, ``/responses``,
    ``/v1/messages`` after one trailing ``/v1`` -- see ``anthropic_base_url``)
    to it. The trailing slash is dropped so they join the same way everywhere.
    """
    raw = value.strip()
    if not raw or len(raw) > MAX_BASE_URL_CHARS:
        raise ModelConnectionInvalid("Enter the provider's Base URL.")
    if any(ord(ch) <= 32 or ord(ch) > 126 for ch in raw):
        raise ModelConnectionInvalid("A Base URL cannot contain spaces or non-ASCII characters.")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ModelConnectionInvalid("That Base URL cannot be parsed.") from exc
    if parts.scheme.lower() != "https":
        raise ModelConnectionInvalid("A Base URL must use https://.")
    if not parts.hostname:
        raise ModelConnectionInvalid("A Base URL needs a host.")
    if parts.username or parts.password:
        raise ModelConnectionInvalid("Put the key in the API key field, not in the URL.")
    if port not in (None, 443):
        raise ModelConnectionInvalid("A sandbox reaches HTTPS on port 443 only.")
    if parts.query or parts.fragment or raw.endswith(("?", "#")):
        raise ModelConnectionInvalid("A Base URL cannot carry a query or fragment.")
    host = parts.hostname.lower()
    if ":" in host:
        raise ModelConnectionInvalid("A sandbox reaches IPv4 only; use a host name.")
    path = parts.path.rstrip("/")
    return f"https://{host}{path}"


def clean_models(values: list[str]) -> list[str]:
    """The model ids an owner typed: trimmed, de-duplicated in order, bounded."""
    models: list[str] = []
    for raw in values:
        model = raw.strip()
        if not model:
            continue
        if not MODEL_ID.fullmatch(model):
            raise ModelConnectionInvalid(f"{model[:80]!r} is not a model id.")
        if model not in models:
            models.append(model)
    if not 1 <= len(models) <= MAX_MODELS:
        raise ModelConnectionInvalid(f"List 1 to {MAX_MODELS} model ids.")
    return models


def is_connection_id(value: object) -> bool:
    """Whether ``value`` has the shape of a connection id."""
    return isinstance(value, str) and CONNECTION_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class TurnSource:
    """Which model a turn runs, and where: ``connection_id`` ``None`` is the Gateway."""

    model: str
    connection_id: str | None = None


#: A verification outcome for one native API: the round trip worked, the
#: provider answered that it cannot, or it could not be determined.
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
FAILED = "failed"


def current_check(connection: dict[str, Any], model: str) -> dict[str, Any] | None:
    """``model``'s verification, if it tested the connection as it is now; else ``None``."""
    checks = connection.get("model_checks")
    entry = checks.get(model) if isinstance(checks, dict) else None
    if not isinstance(entry, dict):
        return None
    if entry.get("revision") != connection.get("config_revision"):
        return None
    return entry


def supported_protocols(result: dict[str, Any] | None) -> list[str]:
    """The native APIs one verification found working, in the platform's order."""
    results = result.get("protocols") if isinstance(result, dict) else None
    if not isinstance(results, dict):
        return []
    return [
        protocol
        for protocol in PROTOCOLS
        if isinstance(results.get(protocol), dict) and results[protocol].get("status") == SUPPORTED
    ]


def verified_protocols(connection: dict[str, Any], model: str) -> list[str]:
    """The native APIs ``model`` runs on: those its verification at this revision found."""
    return supported_protocols(current_check(connection, model))


def usable_runtimes(result: dict[str, Any]) -> list[str]:
    """The agents one verification result lets run its model; empty refuses the save."""
    return sorted(runtime_plans(supported_protocols(result)))


def model_paths(connection: dict[str, Any], model: str) -> dict[str, ProtocolPlan]:
    """Every runtime ``model`` can run on through ``connection``, with its plan.

    Native first, then Codex and Claude Code over Chat Completions through the
    helper (``protocol_plan``). Empty for a model that is not listed, not
    verified, or verified with no API an agent can use.
    """
    if model not in (connection.get("models") or []):
        return {}
    return runtime_plans(verified_protocols(connection, model))


def require_usable(
    connection: dict[str, Any] | None,
    *,
    runtime: str,
    model: str,
    reasoning_effort: str | None = None,
) -> ProtocolPlan:
    """The plan ``runtime`` will use on ``connection`` for ``model``, or refuse.

    The plan is ``model_paths``': for this model, the runtime's own protocol
    when it was verified, else one of the two translations onto
    a verified Chat Completions.

    The one predicate admission, follow-ups, waiting messages, goal rounds,
    claims, residencies and Fork all ask, and the composer offers exactly what
    it accepts. ``connection`` is the owner's own row: callers read it scoped
    to the turn's owner, so another owner's connection is ``not_found`` here
    exactly as a missing one is.
    """
    if connection is None:
        raise ModelConnectionUnusable("not_found")
    if connection.get("deleted_at") is not None:
        raise ModelConnectionUnusable("deleted")
    if not connection.get("enabled"):
        raise ModelConnectionUnusable("disabled")
    if runtime_protocol(runtime) is None:
        raise ModelConnectionUnusable("runtime_unsupported")
    if model not in (connection.get("models") or []):
        raise ModelConnectionUnusable("model_not_listed")
    if current_check(connection, model) is None:
        raise ModelConnectionUnusable("model_unverified")
    plan = model_paths(connection, model).get(runtime)
    if plan is None:
        raise ModelConnectionUnusable("protocol_unsupported")
    if reasoning_effort is not None:
        raise ModelConnectionUnusable("effort_unsupported")
    return plan


__all__ = [
    "FAILED",
    "MAX_NAME_CHARS",
    "SUPPORTED",
    "UNSUPPORTED",
    "UNUSABLE_MESSAGES",
    "ModelConnectionAccessEnded",
    "ModelConnectionConflict",
    "ModelConnectionInvalid",
    "ModelConnectionUnusable",
    "TurnSource",
    "UnusableReason",
    "clean_base_url",
    "clean_models",
    "clean_name",
    "current_check",
    "is_connection_id",
    "model_paths",
    "require_usable",
    "supported_protocols",
    "usable_runtimes",
    "verified_protocols",
]
