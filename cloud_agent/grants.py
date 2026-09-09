"""Asking the gateway for the capability a sandbox runs with.

This is where the per-attempt model credential comes from now. Before the
split the control plane minted it itself, signed with a key it shared with the
gateway. It no longer holds that key, and should not: minting a credential
that spends someone's inference quota is the gateway's decision, and asking
for one is this service's.

Three capability operations, with a deliberate asymmetry:

- **mint** is idempotent on ``(job, attempt)``. A retry after a timeout gets
  the same grant back rather than a second capability nothing is tracking.
- **renew** is what keeps a long job alive. The grant's TTL is short, so a
  control plane that stops renewing — crashed, partitioned, or having decided
  the attempt is superseded — lets the capability die on its own.
- **revoke** is an *acceleration* of that, not the mechanism. It is allowed to
  fail: the TTL is the backstop, which is the only reason revoke needs no
  durable outbox behind it.

**usage** is read-only attribution. It returns a validated snapshot over an
explicit request-start window; unavailable or incomplete usage never changes
whether an attempt may run or how much inference it may request.

**The fence lives here, not at the gateway.** After the split the gateway holds
no attempt or lease state at all, so nothing there can notice that an attempt
was superseded. This service notices, and stops renewing. Anything that makes
renewal automatic — a background loop that does not consult the store — quietly
removes the fence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from cloud_agent.gateway import ENV_BASE_URL, ENV_DISPATCH_TOKEN, GatewayError
from cloud_agent.grant_usage import GrantUsageReport, parse_usage_report
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 5.0
ENV_GRANT_BASE_PATH = "GATEWAY_GRANT_BASE_PATH"
ENV_GRANT_BINDING_ID = "GATEWAY_GRANT_BINDING_ID"
_DEFAULT_GRANT_BASE_PATH = "/internal/agent-grants"


# ── The cadence and the lifetime are one decision ──────────────────────
#
# **Invariant: a grant must outlive several renewal passes.** Renewal runs on
# the reaper's pass, so the cadence is a scheduling constant and the TTL is a
# capability's lifetime — two numbers that live in different files and mean
# nothing apart. If the TTL is ever shorter than the cadence plus its slack, a
# running attempt loses its capability mid-run and the only symptom is a model
# call refused inside a sandbox, far from either constant.
#
# So the cadence is defined here, next to the lifetime derived from it, and
# ``cloud_agent.app.REAP_INTERVAL_S`` **is** this value rather than a second
# copy of it. ``tests/unit/test_grant_lifecycle.py`` fails if the two stop
# agreeing, if the headroom is spent, or if either number leaves the bounds the
# gateway enforces.

#: How often the control plane renews a live grant — the reaper's interval,
#: because the renewal pass rides on it.
RENEWAL_INTERVAL_S = 30.0

#: Renewal passes a live grant can lose before it expires under a running
#: attempt. Three is the least worth having (one slow gateway call, one missed
#: pass, one retry); four is that with a pass to spare.
RENEWAL_HEADROOM_PASSES = 4

#: The lifetime :meth:`GrantClient.mint` asks for. **Derived, never typed**: a
#: literal here is a number free to drift from the loop that keeps it alive.
GRANT_TTL_S = int(RENEWAL_INTERVAL_S * RENEWAL_HEADROOM_PASSES)

#: Mirrors of the gateway's own clamps, from the C5 work this service's grant
#: calls are written against (`serving/grants.py`: ``MAX_GRANT_TTL_S`` and
#: ``DEFAULT_GRANT_TTL_S``, which is also its renewal step). Mirrors because the
#: gateway is reached over HTTP only and neither number is on the wire — asking
#: for more than the maximum is silently clamped *down*, and a renewal always
#: buys the step from now whatever was asked for at mint. Both would therefore
#: break the invariant above without any call failing, which is why the test
#: asserts our numbers stay inside them.
GATEWAY_MAX_TTL_S = 900
GATEWAY_RENEWAL_STEP_S = 300


class GrantClient:
    """Manage inference grants and read their attributed gateway usage."""

    def __init__(
        self,
        base_url: str | None = None,
        dispatch_token: str | None = None,
        *,
        base_path: str | None = None,
        binding_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build a client.

        Args:
            base_url: Gateway origin; defaults to ``GATEWAY_BASE_URL``.
            dispatch_token: Service credential; defaults to
                ``GATEWAY_GRANT_DISPATCH_TOKEN``.
            base_path: Grant route prefix; defaults to
                ``GATEWAY_GRANT_BASE_PATH`` or ``/internal/agent-grants``.
            binding_id: Optional nonsecret installation identity, also read
                from ``GATEWAY_GRANT_BINDING_ID``. When supplied, this names
                the authority across URL, prefix and credential migrations.
            client: An existing httpx client, for tests and pooling.
        """
        self._base_url = (base_url or os.environ.get(ENV_BASE_URL) or "").rstrip("/")
        self._token = dispatch_token or os.environ.get(ENV_DISPATCH_TOKEN) or ""
        self._base_path = (
            base_path or os.environ.get(ENV_GRANT_BASE_PATH) or _DEFAULT_GRANT_BASE_PATH
        ).rstrip("/")
        if not self._base_path.startswith("/") or any(
            character in self._base_path for character in ("?", "#")
        ):
            raise ValueError("the grant base path must be an absolute URL path")
        identity = (
            binding_id if binding_id is not None else os.environ.get(ENV_GRANT_BINDING_ID, "")
        ).strip()
        binding_parts = (
            ["installation", identity]
            if identity
            else ["endpoint", self._base_url, self._base_path]
        )
        binding = json.dumps(binding_parts, separators=(",", ":"))
        self._binding_id = "gw_" + hashlib.sha256(binding.encode()).hexdigest()
        self._client = client

    @property
    def configured(self) -> bool:
        """Whether this deployment can ask for grants at all."""
        return bool(self._base_url and self._token)

    @property
    def binding_id(self) -> str:
        """Identify a gateway installation without storing its credential.

        An explicitly configured installation identity survives URL, prefix
        and token migrations. Operators must change it when the authority
        changes. Without one, URL and prefix form a conservative fallback;
        token rotation preserves either fingerprint.
        """
        return self._binding_id

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one authorized POST, or raise.

        Raises:
            GatewayError: If unconfigured, unreachable, refused, or unparsable.
        """
        if not self.configured:
            raise GatewayError(f"{ENV_BASE_URL} and {ENV_DISPATCH_TOKEN} must both be set")
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = httpx.Timeout(_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
        url = f"{self._base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.post(url, json=body, headers=headers, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc

        if response.status_code != 200:
            # The gateway's refusal reason is worth surfacing: "that user
            # cannot be granted capability" and "this deployment does not
            # federate capability" are different problems, and only one of
            # them is about the job.
            detail = ""
            error_type = None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        detail = str(error.get("message", ""))
                        raw_type = error.get("type")
                        error_type = str(raw_type) if raw_type else None
            except ValueError:
                detail = ""
            raise GatewayError(
                f"gateway answered {response.status_code} for {path}"
                + (f": {detail}" if detail else ""),
                status_code=response.status_code,
                error_type=error_type,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(f"gateway returned unparsable JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise GatewayError(f"gateway returned {type(payload).__name__} for {path}")
        return payload

    async def mint(
        self,
        *,
        user_id: str,
        job_id: str,
        attempt_id: str,
        models: list[str] | None = None,
        ttl_seconds: int | None = GRANT_TTL_S,
    ) -> dict[str, Any]:
        """Ask for the capability one attempt runs with.

        The returned scope is the **effective** one, which may be narrower than
        what was asked: the gateway clamps models to what the user's role may
        reach. Use what comes back, not what was sent — a job built around a
        model the grant does not carry fails at its first call.

        Args:
            user_id: The gateway's user identifier.
            job_id: This service's job id, opaque to the gateway.
            attempt_id: This service's attempt id.
            models: Requested models; ``None`` means everything the role allows.
            ttl_seconds: Requested lifetime, clamped by the gateway. Defaults to
                :data:`GRANT_TTL_S`, which is derived from the renewal cadence —
                see the invariant above. ``None`` is accepted and means "the
                gateway's default", a number this service does not know and
                cannot keep in step with its own loop; nothing here passes it.

        Returns:
            The grant, including its bearer ``token``.

        Raises:
            GatewayError: If the grant cannot be minted.
        """
        body: dict[str, Any] = {
            "user_id": user_id,
            "external_job_id": job_id,
            "external_attempt_id": str(attempt_id),
        }
        if models is not None:
            body["allowed_models"] = models
        # No `allowed_mcp`. It was sent on every mint until this was noticed,
        # and the gateway had already stopped declaring the field — pydantic
        # ignores what it does not know, so the request looked accepted and the
        # scope it named was never requested of anything. MCP is decided here
        # now, by the relay's scope check and the attempt fence.
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return await self._post(self._base_path, body)

    async def renew(self, grant_id: str) -> dict[str, Any]:
        """Extend a live grant by another bounded step.

        Call this only while the attempt is still current. Renewing an attempt
        the store has superseded is what would keep a zombie sandbox spending,
        and the gateway cannot catch it: it has no attempt state to check.

        Args:
            grant_id: The grant to extend.

        Returns:
            The renewed grant.

        Raises:
            GatewayError: If the grant is unknown, revoked, expired, or its
                owner is no longer active.
        """
        return await self._post(f"{self._base_path}/{quote(grant_id, safe='')}/renew")

    async def usage(self, grant_id: str, *, since: datetime, until: datetime) -> GrantUsageReport:
        """Read a revisable totals snapshot for one fixed request-start window.

        Both bounds are always sent explicitly. Reusing a fixed interval lets
        delayed request logs increase the snapshot without changing which
        turn it describes. Missing, old, or malformed reports raise instead
        of manufacturing zero usage.

        Raises:
            GatewayError: For invalid bounds, unavailable service, gateway
                refusal, an unsupported schema, or a mismatched report.
        """
        if (
            any(
                not isinstance(bound, datetime) or bound.tzinfo is None or bound.utcoffset() is None
                for bound in (since, until)
            )
            or since >= until
        ):
            raise GatewayError(
                "usage requires an ordered window with UTC offsets",
                error_type="usage_query_invalid",
            )
        if not self.configured:
            raise GatewayError("gateway usage is not configured", error_type="usage_unconfigured")
        since, until = since.astimezone(UTC), until.astimezone(UTC)
        path = f"{self._base_path}/{quote(grant_id, safe='')}/usage"
        params = {"since": since.isoformat(), "until": until.isoformat(), "detail": "totals"}
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = httpx.Timeout(_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S)
        try:
            if self._client is not None:
                response = await self._client.get(
                    f"{self._base_url}{path}", params=params, headers=headers, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(
                        f"{self._base_url}{path}", params=params, headers=headers
                    )
        except httpx.HTTPError:
            raise GatewayError(
                "gateway usage is unreachable", error_type="usage_unavailable"
            ) from None
        if response.status_code != 200:
            error_type = "usage_http_error"
            try:
                payload = response.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                raw_type = error.get("type") if isinstance(error, dict) else None
                if isinstance(raw_type, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", raw_type):
                    error_type = raw_type
            except ValueError:
                pass
            raise GatewayError(
                "gateway refused the usage query",
                status_code=response.status_code,
                error_type=error_type,
            )
        try:
            payload = response.json()
        except ValueError:
            raise GatewayError(
                "gateway returned invalid usage JSON", error_type="usage_invalid_response"
            ) from None
        return parse_usage_report(payload, grant_id=grant_id, since=since, until=until)

    async def revoke(self, grant_id: str) -> bool:
        """Withdraw a grant now rather than waiting for its TTL.

        Returns ``False`` instead of raising when the call fails. Revocation is
        an acceleration of something the TTL does anyway, so a failure here
        must not take down the caller — which is usually a cleanup path that
        has other work to finish.

        Args:
            grant_id: The grant to revoke.

        Returns:
            Whether the gateway confirmed the revocation.
        """
        try:
            await self._post(f"{self._base_path}/{quote(grant_id, safe='')}/revoke")
        except GatewayError:
            logger.warning(
                "agent_grant_revoke_failed",
                exc_info=True,
                extra={"event": "agent_grant_revoke_failed", "grant_id": grant_id},
            )
            return False
        return True
