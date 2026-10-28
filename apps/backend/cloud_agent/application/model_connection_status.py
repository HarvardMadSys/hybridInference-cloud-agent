"""What each model on an owner's connection can do, as its owner is shown it.

Derived only from the per-model verification facts (``domain.model_connections``),
or from one verification that has not been saved. A model is

- ``ready``: verified, with at least one agent that can use it;
- ``unverified``: no facts for the connection as it is now (a saved model
  always has them; this is a backstop);
- ``failed``: its verification could not determine any usable API (the
  provider was unreachable, refused the key, rate limited, failed ...);
- ``unsupported``: the provider answered, and no API completed a streaming
  tool round trip.

The agents a model is ``ready`` for are exactly what ``require_usable``
accepts, so the settings page, the composer and every execution gate agree.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.application.model_connection_check import CHECK_MESSAGES
from cloud_agent.domain.model_connections import (
    FAILED,
    current_check,
    model_paths,
)
from cloud_agent_shared.model_connections import PROTOCOLS

#: When several APIs could not be checked, the reason an owner can act on first.
_FAILURE_ORDER = (
    "auth",
    "blocked_address",
    "tls",
    "redirected",
    "unreachable",
    "rate_limited",
    "upstream_error",
    "too_large",
    "unexpected_status",
    "stream_error",
    "incomplete_stream",
)

READY = "ready"
UNVERIFIED = "unverified"
FAILED_STATUS = "failed"
UNSUPPORTED_STATUS = "unsupported"


def _protocol_results(entry: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    results = entry.get("protocols") if entry is not None else None
    if not isinstance(results, dict):
        return {}
    shown: dict[str, dict[str, Any]] = {}
    for protocol in PROTOCOLS:
        result = results.get(protocol)
        if isinstance(result, dict) and isinstance(result.get("category"), str):
            category = result["category"]
            shown[protocol] = {
                "status": str(result.get("status") or ""),
                "category": category,
                "message": CHECK_MESSAGES.get(category, ""),
                "http_status": result.get("http_status"),
            }
    return shown


def _unusable_reason(results: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """``failed`` or ``unsupported``, and the category that explains it."""
    failures = {r["category"] for r in results.values() if r["status"] == FAILED}
    if failures:
        category = next((c for c in _FAILURE_ORDER if c in failures), sorted(failures)[0])
        return FAILED_STATUS, category
    chat = results.get("chat_completions")
    category = chat["category"] if chat else next(iter(results.values()))["category"]
    return UNSUPPORTED_STATUS, category


def model_status(row: dict[str, Any], model: str) -> dict[str, Any]:
    """One listed model's state, the agents it serves and how, and its diagnostics."""
    paths = model_paths(row, model)
    entry = current_check(row, model)
    results = _protocol_results(entry)
    status_category: tuple[str, str | None]
    if entry is None:
        status_category = (UNVERIFIED, None)
    elif paths:
        status_category = (READY, None)
    elif results:
        status_category = _unusable_reason(results)
    else:
        status_category = (UNSUPPORTED_STATUS, "invalid_response")
    status, category = status_category
    return {
        "model": model,
        "status": status,
        "category": category,
        "message": CHECK_MESSAGES.get(category, "") if category else "",
        "runtimes": sorted(paths),
        "paths": [
            {
                "runtime": runtime,
                "mode": plan.mode,
                "client_protocol": plan.client_protocol,
                "upstream_protocol": plan.upstream_protocol,
            }
            for runtime, plan in sorted(paths.items())
        ],
        "protocols": results,
        "checked_at": entry.get("checked_at") if entry is not None else None,
    }


def verification_status(model: str, result: dict[str, Any]) -> dict[str, Any]:
    """What one verification, saved or not, says about ``model``: the same view as a saved one."""
    return model_status(
        {
            "models": [model],
            "config_revision": 0,
            "model_checks": {model: {**result, "revision": 0}},
        },
        model,
    )


def connection_statuses(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every listed model's state, in the owner's order."""
    return [model_status(row, model) for model in row.get("models") or []]


def usable_runtimes(row: dict[str, Any]) -> dict[str, list[str]]:
    """Each listed model with the agents that can run it now; a model with none is left out."""
    usable = {model: sorted(model_paths(row, model)) for model in row.get("models") or []}
    return {model: runtimes for model, runtimes in usable.items() if runtimes}


__all__ = [
    "FAILED_STATUS",
    "READY",
    "UNSUPPORTED_STATUS",
    "UNVERIFIED",
    "connection_statuses",
    "model_status",
    "usable_runtimes",
    "verification_status",
]
