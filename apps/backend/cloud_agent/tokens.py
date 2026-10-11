"""Per-attempt capability tokens for agent-sandbox workers (issue #1041).

Workers run inside the sandbox and must report events back, but must never
hold a long-lived or cross-task credential. Per the adjudicated credential
principle: *long-lived, high-privilege or cross-task credentials never enter
the agent environment; only short-lived, single-task, capped, instantly
revocable capability tokens do.*

This module mints exactly that: a stateless HMAC token bound to one
``(job_id, attempt_id, lease_generation)`` triple. It is:

- **write-only in scope** — it authorizes the worker endpoints for its own
  attempt and nothing else;
- **self-revoking** — the token carries the fencing triple, so the moment the
  reaper supersedes that attempt every store write it can attempt is rejected.
  Revocation needs no token blacklist: the fence in the attempt tables is the
  authority.

Signed with this service's **own** secret, ``AGENT_CONTROL_TOKEN_SECRET``.

Before the split these tokens derived from the gateway's ``API_KEY_SECRET``,
which is the coupling the split exists to remove: sharing a signing root means
either service can mint the other's credentials, and rotating one breaks both.
The gateway never sees this value, and this service never sees the gateway's.

The domain-separation prefix survives the re-key for the same reason it existed:
a signature over it is meaningless as anything else.

The ``model`` scope is **still here, and still dead weight** — nothing mints
one any more, because the runner asks the gateway for a grant instead. It is
kept so the moved tests run unmodified, which is what makes them evidence that
nothing else changed. Note what that means in the meantime: a ``model``-scoped
token minted here is signed by a root the gateway does not know, so it buys
nothing. That is the correct failure, but it is a failure, not a working path.

**Re-keying invalidates every token in flight.** That is a cutover concern, not
a bug: an attempt holding a token signed by the old root is an attempt from
before the migration, and it must not keep writing to the new store. Drain
before cutover; see MIGRATION.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

# Domain separation: a signature over this prefix is meaningless anywhere else.
_TOKEN_PREFIX = "ajt"
_SIGNING_CONTEXT = b"agent-job-worker-token:v1"

# Host credentials get their own prefix — see ``mint_host_token``.
_HOST_TOKEN_PREFIX = "aht"


class InvalidAgentToken(Exception):
    """Raised when a worker token is malformed, mis-signed, or truncated."""


ENV_SIGNING_SECRET = "AGENT_CONTROL_TOKEN_SECRET"


def _signing_key() -> bytes:
    """Return the HMAC key derived from this service's own secret.

    Raises:
        ValueError: If the secret is unset. Refusing to mint is the only safe
            answer — a default would make every deployment share a signing
            root, which is worse than the coupling this replaced.
    """
    from cloud_agent.settings import setting_env

    secret = (setting_env(ENV_SIGNING_SECRET) or "").encode()
    if not secret:
        raise ValueError(f"{ENV_SIGNING_SECRET} must be set to mint agent worker tokens")
    return hashlib.sha256(_SIGNING_CONTEXT + secret).digest()


def _b64encode(raw: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    """Decode URL-safe base64 that had its padding stripped."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


# Three scopes, because the runner and the sandbox need different powers — and
# the sandbox's two powers come from two different services.
#
# ``full`` is held by the runner (a trusted process outside the sandbox): it
# reports events, renews the lease and drives terminal
# transitions.
#
# ``mcp`` is what a sandbox carries to reach tools, and it is minted and
# verified **here** — this service owns the MCP registry, its upstream
# credentials and the attempt fence, so it is the only place that can decide a
# tool call. Model calls are a different credential entirely: the gateway's
# ``agr`` grant, which this service never mints and the relay never accepts.
# The two are deliberately not interchangeable, so a leaked MCP token buys no
# inference and a leaked grant buys no tools.
#
# ``model`` is the sandbox's *previous* inference credential, kept while jobs
# started before the cutover are still running. New attempts get a grant
# instead. It is removed with the gateway's legacy MCP route at H4.
SCOPE_FULL = "full"
SCOPE_MODEL = "model"
SCOPE_MCP = "mcp"
_VALID_SCOPES = (SCOPE_FULL, SCOPE_MODEL, SCOPE_MCP)

# ``host`` is the third scope, and it is deliberately *not* in the tuple above.
#
# A worker token names an attempt; a host token names a machine, and lives for
# as long as that machine is enrolled. Putting them in one namespace would mean
# ``mint_worker_token(scope="host")`` could produce a machine credential bound
# to an attempt, and ``parse_worker_token`` would have to be trusted to notice.
# Separate prefixes make the two mutually unparseable, so the check is
# structural rather than a comparison somebody has to remember to write.
SCOPE_HOST = "host"


def mint_worker_token(
    *,
    job_id: str,
    attempt_id: int,
    lease_generation: int,
    scope: str = SCOPE_FULL,
) -> str:
    """Mint a capability token bound to one attempt's fencing triple."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES}, got {scope!r}")
    payload = json.dumps(
        {"j": job_id, "a": attempt_id, "g": lease_generation, "s": scope},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return f"{_TOKEN_PREFIX}.{_b64encode(payload)}.{_b64encode(signature)}"


def parse_worker_token(token: str) -> dict[str, Any]:
    """Verify a worker token and return its fencing triple.

    Returns ``{"job_id", "attempt_id", "lease_generation", "scope"}``. Raises
    :class:`InvalidAgentToken` on any structural or signature problem — the
    caller maps that to 401. Tokens minted before scopes existed parse as
    ``full``, which is the conservative reading for an already-signed token.
    """
    if not token:
        raise InvalidAgentToken("empty token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        raise InvalidAgentToken("malformed token")
    try:
        payload = _b64decode(parts[1])
        signature = _b64decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise InvalidAgentToken("undecodable token") from exc

    expected = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidAgentToken("bad signature")

    try:
        claims = json.loads(payload)
        scope = str(claims.get("s", SCOPE_FULL))
        if scope not in _VALID_SCOPES:
            raise InvalidAgentToken(f"unknown scope {scope!r}")
        return {
            "job_id": str(claims["j"]),
            "attempt_id": int(claims["a"]),
            "lease_generation": int(claims["g"]),
            "scope": scope,
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidAgentToken("bad claims") from exc


class InvalidHostToken(Exception):
    """Raised when a host credential is malformed, mis-signed, or truncated."""


def mint_host_token(*, host: str) -> str:
    """Mint the credential a host receives when it enrolls.

    Unlike a worker token this is not self-revoking — there is no attempt whose
    fence expires under it — so revocation is external: the control plane
    stores only ``sha256`` of this string, and clearing that column kills the
    credential. That is why a nonce is included. Two enrollments of the same
    host must produce two different credentials, otherwise revoking one and
    re-issuing would hand back the token that was just revoked.
    """
    payload = json.dumps(
        {"h": host, "n": secrets.token_hex(8), "s": SCOPE_HOST},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return f"{_HOST_TOKEN_PREFIX}.{_b64encode(payload)}.{_b64encode(signature)}"


def parse_host_token(token: str) -> dict[str, Any]:
    """Verify a host credential and return the machine it names.

    Returns ``{"host", "scope"}``. The signature proves this service minted it;
    it does **not** prove the credential is still valid. The caller must still
    compare its hash against the host row, because that comparison is the only
    thing revocation can act on.
    """
    if not token:
        raise InvalidHostToken("empty token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _HOST_TOKEN_PREFIX:
        raise InvalidHostToken("malformed token")
    try:
        payload = _b64decode(parts[1])
        signature = _b64decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise InvalidHostToken("undecodable token") from exc

    expected = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidHostToken("bad signature")

    try:
        claims = json.loads(payload)
        if str(claims.get("s")) != SCOPE_HOST:
            raise InvalidHostToken("not a host credential")
        host = str(claims["h"])
        if not host:
            raise InvalidHostToken("bad claims")
        return {"host": host, "scope": SCOPE_HOST}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidHostToken("bad claims") from exc
