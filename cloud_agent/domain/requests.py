"""Idempotency for a submitted request, as a pure function.

A client resending a create or follow-up after a lost response carries the same
``Idempotency-Key``. The fingerprint is how the store tells that resend apart
from the same key reused for different content.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def request_fingerprint(kind: str, payload: dict[str, Any]) -> str:
    """Hash what a submission asked for, so a replayed key must ask the same thing.

    A client resending a create or follow-up after a lost response carries the
    same ``Idempotency-Key``; the fingerprint is how the store tells that
    resend apart from the same key reused for different content. ``kind``
    keeps a create and a follow-up from ever fingerprinting alike.
    """
    # Keep pre-skills hashes byte-compatible: existing nullable fields are part
    # of their historical hash. New optional extensions must not change it when
    # omitted. Centralize that rule instead of repeating it in endpoint callers.
    payload = {
        key: value for key, value in payload.items() if key != "skill_ids" or value is not None
    }
    canonical = json.dumps(
        {"kind": kind, **payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_client_request(client_request_id: str | None, fingerprint: str | None) -> None:
    """A submission key travels with its fingerprint, or not at all."""
    if (client_request_id is None) != (fingerprint is None):
        raise ValueError("client_request_id and client_request_fingerprint come together")
    if client_request_id is not None and not 1 <= len(client_request_id) <= 256:
        raise ValueError("client_request_id must be 1-256 characters")
