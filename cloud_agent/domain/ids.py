"""Identifier minting for the objects the control plane creates.

Ids are opaque and prefixed by kind, which is what makes a log line readable
without a join. They never encode meaning beyond that prefix.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any


def _new_job_id() -> str:
    """Generate a new agent job id."""
    return f"ajob_{secrets.token_hex(8)}"


def _new_thread_id() -> str:
    """Generate a stable conversation id shared by a thread's runs."""
    return f"athr_{secrets.token_hex(8)}"


def _new_sandbox_id() -> str:
    """Generate the platform identity of one retained Docker sandbox."""
    return f"asbx_{secrets.token_hex(16)}"


def _new_session_fork_id() -> str:
    """Generate one shared native-session fork operation id."""
    return f"asfork_{secrets.token_hex(16)}"


def request_fingerprint(kind: str, payload: dict[str, Any]) -> str:
    """Hash what a submission asked for, so a replayed key must ask the same thing.

    A client resending a create or follow-up after a lost response carries the
    same ``Idempotency-Key``; the fingerprint is how the store tells that
    resend apart from the same key reused for different content. ``kind``
    keeps a create and a follow-up from ever fingerprinting alike.
    """
    canonical = json.dumps(
        {"kind": kind, **payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
