"""Safe compute errors, request identity, and public projections."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa


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


def discard_request_id(allocation_id: str) -> str:
    """The one release request of an abandoned replacement node."""
    return "discard:" + allocation_id


def _openssh_public_key(algorithm: str, body: str) -> str:
    """Return ``algorithm body`` when the blob matches its label and type."""
    try:
        key = serialization.load_ssh_public_key(f"{algorithm} {body}".encode())
        embedded = base64.b64decode(body, validate=True)
    except (ValueError, UnsupportedAlgorithm, binascii.Error):
        raise ValueError("invalid") from None
    name_length = int.from_bytes(embedded[:4], "big")
    labelled = embedded[4 : 4 + name_length].decode(errors="replace") == algorithm
    supported = isinstance(key, ed25519.Ed25519PublicKey | ec.EllipticCurvePublicKey) or (
        isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048
    )
    if not labelled or not supported:
        raise ValueError("unsupported")
    return f"{algorithm} {body}"


def normalize_user_public_key(value: str) -> str:
    """Return one OpenSSH user public key, keeping a short printable comment.

    CloudLab installs this on a new experiment for the SSH user. A private key,
    more than one line, or a line that does not parse as its type is refused.
    """
    if any(character in value for character in "\r\n") or len(value) > 8192:
        raise ValueError("SSH public key must be one OpenSSH public key")
    parts = value.split()
    if len(parts) < 2:
        raise ValueError("SSH public key must be one OpenSSH public key")
    try:
        canonical = _openssh_public_key(parts[0], parts[1])
    except ValueError:
        raise ValueError("SSH public key must be one OpenSSH public key") from None
    comment = " ".join(parts[2:])
    printable = all(32 <= ord(character) <= 126 for character in comment)
    if comment and (len(comment) > 128 or not printable):
        raise ValueError("SSH public key must be one OpenSSH public key")
    return f"{canonical} {comment}" if comment else canonical


def normalize_host_keys(keys: list[str]) -> list[str]:
    """Return a node's SSH host public keys as sorted, comment-free, parsed keys.

    Accepts Ed25519, NIST ECDSA and RSA of at least 2048 bits. Anything else,
    or a line that does not parse as the key type it claims, is refused.
    """
    normalized: set[str] = set()
    for line in keys:
        parts = line.split() if isinstance(line, str) and len(line) <= 8192 else []
        if len(parts) < 2:
            raise ComputeRefusal(
                "invalid_host_keys", "Host keys must be OpenSSH public keys.", status=422
            )
        try:
            canonical = _openssh_public_key(parts[0], parts[1])
        except ValueError as exc:
            message = (
                "Host key type is not supported."
                if str(exc) == "unsupported"
                else "Host keys must be OpenSSH public keys."
            )
            raise ComputeRefusal("invalid_host_keys", message, status=422) from None
        normalized.add(canonical)
    if not normalized:
        raise ComputeRefusal("invalid_host_keys", "No host key was supplied.", status=422)
    return sorted(normalized)


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
            # Node moves: which replacement types failed and how, and how long
            # the copies took. Never node addresses, keys or remote output.
            "attempts",
            "precopy_bytes",
            "precopy_seconds",
            "final_copy_bytes",
            "final_copy_seconds",
            "verified_at",
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
    if (
        record.get("kind") == "rotate"
        and record.get("state") in {"pending", "waiting", "blocked"}
        # Once the old node stops, only the worker's rollback restores service.
        and not internal.get("source_stopping")
        and not internal.get("source_stopped")
        and not internal.get("rebound")
        and not release_committed
    ):
        result["allowed_actions"].append("cancel")
    return result
