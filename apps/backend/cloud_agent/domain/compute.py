"""Safe compute errors, request identity, and public projections."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class ComputeRefusal(Exception):
    """A typed, intentionally public reason why a command cannot proceed."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def fingerprint(kind: str, parameters: dict[str, Any]) -> str:
    """Bind a request ID to the exact command, excluding generated resource IDs."""
    encoded = json.dumps(
        {"kind": kind, "parameters": parameters},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def public_connection(record: dict[str, Any]) -> dict[str, Any]:
    """A whitelist prevents ciphertext or future secret fields reaching browsers."""
    result: dict[str, Any] = {
        key: record.get(key)
        for key in (
            "id",
            "name",
            "provider_id",
            "configuration",
            "status",
            "masked_summary",
            "revision",
            "observed_at",
            "updated_at",
        )
    }
    result["blocked_reasons"] = (
        [
            {
                "code": "connection_unverified",
                "message": "Validate this connection before deployment.",
            }
        ]
        if record.get("status") != "verified"
        else []
    )
    return result


def public_operation(record: dict[str, Any], *, release_committed: bool = False) -> dict[str, Any]:
    """Expose progress, never private requests, worker fences, or profile contents."""
    result: dict[str, Any] = {
        key: record.get(key)
        for key in (
            "id",
            "request_id",
            "kind",
            "allocation_id",
            "state",
            "step",
            "message",
            "created_at",
            "updated_at",
        )
    }
    result["message"] = result["message"] or ""
    internal = record.get("result") or {}
    result["result"] = {
        key: internal[key]
        for key in (
            "checks",
            "blocked_reasons",
            "nodes_ready",
            "target_expires_at",
            "actual_expires_at",
            "external_id",
        )
        if key in internal
    }
    result["blocked_reasons"] = internal.get("blocked_reasons", [])
    result["allowed_actions"] = []
    if record.get("state") in {"blocked", "failed", "unknown"}:
        result["allowed_actions"].append("retry")
    if (
        record.get("kind") == "retire"
        and record.get("state") in {"pending", "waiting", "blocked"}
        and not internal.get("external_pending")
        and not release_committed
    ):
        result["allowed_actions"].append("cancel")
    return result
