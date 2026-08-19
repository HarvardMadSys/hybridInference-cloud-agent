"""The HTTP client for everything this service asks the gateway.

Before the split these were imports. The rule now is that the gateway is
reachable over HTTP only, so every question that used to be answered by
reading its registries or its users table comes through here.

**Every failure is closed.** A gateway that is down, slow, or answering
strangely must never widen what this service permits: an unreachable catalog
resolves to *no* models, not every model, and an unreadable user status
resolves to "cannot use the service", not "fine". The alternative fails open
at exactly the moment nobody is watching.

Authorization is the shared dispatch token. It is a service credential, so it
travels only on this path and never toward a sandbox or a browser.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)

ENV_BASE_URL = "GATEWAY_BASE_URL"
ENV_DISPATCH_TOKEN = "GATEWAY_GRANT_DISPATCH_TOKEN"

#: Generous enough for a busy gateway, bounded because a control plane blocked
#: on a lookup is a control plane that has stopped scheduling.
_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 5.0


class GatewayError(Exception):
    """The gateway could not answer, or answered unusably.

    ``status_code`` is the gateway's, when there was one, and ``error_type``
    is the machine-readable reason from its error body. ``None`` for the
    status means the request never got an answer — unreachable, timed out,
    unparsable — and that distinction is load-bearing for any caller deciding
    whether to give up: a 4xx says *this request* is wrong and will stay
    wrong, while ``None`` says nothing about the request at all and everything
    about the network. Treating them alike turns a minute of gateway downtime
    into a fleet-wide abort.

    ``error_type`` matters for the same reason one level down. Status codes at
    this gateway are overloaded — 404 is both "no such grant" and "this
    deployment exposes no internal endpoints" — so a caller that reads only
    the status cannot tell a dead subject from its own misconfiguration.
    Callers whose reaction is destructive should key off the type.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type

    @property
    def definitive(self) -> bool:
        """Whether retrying this exact request could ever succeed.

        A 408 or 429 is the gateway asking for patience, and 5xx is the gateway
        being unwell — neither is a verdict on the request. This says only that
        the gateway answered and refused; it does not say *what* it refused,
        which is what :attr:`error_type` is for.
        """
        if self.status_code is None:
            return False
        if self.status_code in (408, 429):
            return False
        return 400 <= self.status_code < 500


class GatewayClient:
    """Calls the gateway's internal contracts on behalf of this service."""

    def __init__(
        self,
        base_url: str | None = None,
        dispatch_token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Build a client.

        Args:
            base_url: Gateway origin; defaults to ``GATEWAY_BASE_URL``.
            dispatch_token: Service credential; defaults to
                ``GATEWAY_GRANT_DISPATCH_TOKEN``.
            client: An existing httpx client, for tests and for sharing a
                connection pool.
        """
        self._base_url = (base_url or os.environ.get(ENV_BASE_URL) or "").rstrip("/")
        self._token = dispatch_token or os.environ.get(ENV_DISPATCH_TOKEN) or ""
        self._client = client

    @property
    def configured(self) -> bool:
        """Whether this deployment knows how to reach a gateway at all."""
        return bool(self._base_url and self._token)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one authorized GET, or raise.

        Raises:
            GatewayError: If unconfigured, unreachable, refused, or unparsable.
        """
        if not self.configured:
            raise GatewayError(f"{ENV_BASE_URL} and {ENV_DISPATCH_TOKEN} must both be set")
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
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc

        if response.status_code != 200:
            raise GatewayError(
                f"gateway answered {response.status_code} for {path}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayError(f"gateway returned unparsable JSON for {path}") from exc
        if not isinstance(body, dict):
            raise GatewayError(f"gateway returned {type(body).__name__} for {path}")
        return body

    async def model_catalog(self, user_id: str) -> list[str]:
        """Return the chat models this user's agent jobs may call.

        Deliberately not ``GET /v1/models``: that route cannot authenticate a
        service credential and would answer with the *anonymous* catalog,
        silently narrowing every non-free user to what a stranger sees.

        Args:
            user_id: The gateway's user identifier.

        Returns:
            Canonical model ids, in the gateway's registry order.

        Raises:
            GatewayError: If the catalog cannot be read. Callers that must not
                fail hard should use :func:`agent_visible_models`, which
                converts this into an empty catalog.
        """
        models, _aliases = await self.model_catalog_with_aliases(user_id)
        return models

    async def model_catalog_with_aliases(self, user_id: str) -> tuple[list[str], dict[str, str]]:
        """Return the catalog and the gateway's alias translation table.

        The table is how this service resolves an alias without reimplementing
        the gateway's rules: it maps a name a user may type to the canonical id
        the gateway routes it to.

        An **older gateway sends no `aliases` field**, and that reads as an
        empty table rather than an error — the catalog is still correct, and a
        deployment mid-rollout should narrow to canonical-only rather than stop
        creating jobs.

        Args:
            user_id: The gateway's user identifier.

        Returns:
            ``(models, aliases)``, both empty-safe. Entries whose target is not
            in ``models`` are dropped: the gateway already filters them, and
            trusting an unfiltered one would resolve to a model this user
            cannot reach — refused a moment later with nothing to explain it.

        Raises:
            GatewayError: If the catalog cannot be read.
        """
        body = await self._get("/internal/model-catalog", {"user_id": user_id})
        raw_models = body.get("models")
        if not isinstance(raw_models, list):
            raise GatewayError("gateway returned no model list")
        models = [str(model) for model in raw_models]

        raw_aliases = body.get("aliases")
        if not isinstance(raw_aliases, dict):
            return models, {}
        visible = set(models)
        aliases = {
            str(alias): str(canonical)
            for alias, canonical in raw_aliases.items()
            if str(canonical) in visible and str(alias) not in visible
        }
        return models, aliases

    async def model_reasoning_efforts(self, user_id: str) -> dict[str, list[str]]:
        """Which models accept a reasoning effort, and which values.

        A gateway that predates the field answers nothing, which reads as "no
        model here takes one" — the composer then offers no such control, its
        behaviour before this existed. Failing closed on absence is the same
        choice :func:`agent_visible_models` makes about the catalog itself: a
        control the platform cannot honour must not be offered.

        Args:
            user_id: The gateway's user identifier.

        Returns:
            ``{model_id: [values]}``, empty-safe, restricted to models this
            user can actually reach.

        Raises:
            GatewayError: If the catalog cannot be read.
        """
        body = await self._get("/internal/model-catalog", {"user_id": user_id})
        raw_models = body.get("models")
        visible = {str(model) for model in raw_models} if isinstance(raw_models, list) else set()
        raw = body.get("reasoning_efforts")
        if not isinstance(raw, dict):
            return {}
        efforts: dict[str, list[str]] = {}
        for model, values in raw.items():
            if str(model) not in visible or not isinstance(values, list):
                continue
            levels = [str(value) for value in values]
            if levels:
                efforts[str(model)] = levels
        return efforts

    async def user_status(self, user_id: str) -> dict[str, Any]:
        """Report whether a user exists, may sign in, and with what role.

        Args:
            user_id: The gateway's user identifier.

        Returns:
            The gateway's status record.

        Raises:
            GatewayError: If the status cannot be read.
        """
        return await self._get(f"/internal/users/{user_id}/status")

    # There was an `mcp_registry()` here, calling `/internal/mcp-registry`.
    # The ownership amendment moved the registry, its credentials and its proxy
    # to this service, and the gateway's endpoint went with them — so the
    # method could only ever 404, while its docstring still said a sandbox
    # reaches MCP through the gateway's proxy. Read `registry_for_user()` from
    # `cloud_agent.mcp_registry` instead; nothing here asks the gateway about
    # tools, which is the whole point of the amendment.
