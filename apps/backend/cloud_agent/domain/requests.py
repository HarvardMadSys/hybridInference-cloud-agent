"""Idempotency for a submitted request, as a pure function.

A client resending a create or follow-up after a lost response carries the same
``Idempotency-Key``. The fingerprint is how the store tells that resend apart
from the same key reused for different content.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

#: The top-level fields whose value, null included, has always been part of a
#: fingerprint: a create's and a thread message's own fields, plus the thread a
#: message is for. Fingerprints already stored against live keys hash these, so
#: they stay exactly as they are. Frozen on purpose — a field added to either
#: request later is not listed here, and is therefore left out while unset.
#: Top-level only: a field added inside a nested value (``goal``, an
#: ``mcp_bindings`` entry) still changes the hash of a request carrying one,
#: and dropping nested nulls instead would change every hash stored with them.
_HISTORICAL_FIELDS = frozenset(
    {
        "attachment_ids",
        "base_ref",
        "base_sha",
        "goal",
        "mcp_bindings",
        "metadata",
        "model",
        "prompt",
        "reasoning_effort",
        "repo",
        "runtime",
        "setup_script",
        "task_prompt",
        "thread_id",
    }
)

#: Later fields whose omitted value is ``False`` rather than null.
_FALSE_WHEN_OMITTED = frozenset({"require_idle"})
#: Later fields whose omitted value is an empty list rather than null.
_EMPTY_WHEN_OMITTED = frozenset({"environment_overrides"})


def _omitted(key: str, value: Any) -> bool:
    """Whether a field added after the frozen set carries its omitted value."""
    return (
        value is None
        or (key in _FALSE_WHEN_OMITTED and value is False)
        or (key in _EMPTY_WHEN_OMITTED and not value)
    )


def request_fingerprint(kind: str, payload: dict[str, Any], *, sensitive: bool = False) -> str:
    """Hash what a submission asked for, so a replayed key must ask the same thing.

    A client resending a create or follow-up after a lost response carries the
    same ``Idempotency-Key``; the fingerprint is how the store tells that
    resend apart from the same key reused for different content. ``kind``
    keeps a create and a follow-up from ever fingerprinting alike.
    """
    # Keep historical hashes byte-compatible: existing nullable fields are part
    # of their historical hash. New optional extensions must not change it when
    # omitted. Centralize that rule instead of repeating it in endpoint callers:
    # callers hash ``model_dump()``, which names every declared field, so a
    # field added to a request model reaches here as null on every send — and
    # a retry that spans the deploy adding it must still match its first send.
    payload = {
        key: value
        for key, value in payload.items()
        if key in _HISTORICAL_FIELDS or not _omitted(key, value)
    }
    canonical = json.dumps(
        {"kind": kind, **payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    if sensitive:
        from cloud_agent.environments import EnvironmentEncryptionError
        from cloud_agent.settings import setting_env
        from cloud_agent.source_control import ENV_ENCRYPTION_KEY

        material = setting_env(ENV_ENCRYPTION_KEY, "")
        if not material:
            raise EnvironmentEncryptionError("Environment encryption is unavailable")
        key = hashlib.sha256(
            b"hybridinference/environment-request/v1\0" + material.encode()
        ).digest()
        return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_client_request(client_request_id: str | None, fingerprint: str | None) -> None:
    """A submission key travels with its fingerprint, or not at all."""
    if (client_request_id is None) != (fingerprint is None):
        raise ValueError("client_request_id and client_request_fingerprint come together")
    if client_request_id is not None and not 1 <= len(client_request_id) <= 256:
        raise ValueError("client_request_id must be 1-256 characters")
