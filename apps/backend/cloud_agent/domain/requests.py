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


def request_fingerprint(kind: str, payload: dict[str, Any], *, sensitive: bool = False) -> str:
    """Hash what a submission asked for, so a replayed key must ask the same thing.

    A client resending a create or follow-up after a lost response carries the
    same ``Idempotency-Key``; the fingerprint is how the store tells that
    resend apart from the same key reused for different content. ``kind``
    keeps a create and a follow-up from ever fingerprinting alike.
    """
    # Keep historical hashes byte-compatible: existing nullable fields are part
    # of their historical hash. New optional extensions must not change it when
    # omitted. Centralize that rule instead of repeating it in endpoint callers.
    payload = {
        key: value
        for key, value in payload.items()
        if (key not in {"skill_ids", "plugins", "expected_latest_job_id"} or value is not None)
        and (key != "require_idle" or value is not False)
        and not (
            key in {"environment_id", "environment_revision", "global_revision"} and value is None
        )
        and not (key == "environment_overrides" and not value)
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
