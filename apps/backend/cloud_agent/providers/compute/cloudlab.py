"""CloudLab Portal API adapter.

Wire contract: https://gitlab.flux.utah.edu/emulab/portal-api/-/blob/master/openapi.json
The public spec's example server is a development HTTP endpoint. Production
must supply its verified HTTPS endpoint; credentials never follow redirects.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from contextlib import suppress
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from xml.etree import ElementTree

import httpx
from pydantic import Field, ValidationError, field_validator, model_validator

from cloud_agent.domain.compute import normalize_user_public_key
from cloud_agent.mcp.http_body import IDENTITY_ENCODING_HEADERS, iter_response_bytes
from cloud_agent.ports.external.compute_provider import (
    AcquireRequest,
    AcquisitionResolution,
    ComputeProvider,
    ComputeValue,
    ExtendRequest,
    NodeObservation,
    OperationIntent,
    ProviderCapabilities,
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
    SshConnection,
    UnsupportedCapability,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.ports.external.compute_provider import SecretResolver


class CloudLabConfig(ComputeValue):
    """CloudLab experiment scope and credential references, never token values."""

    credential_ref: str = Field(min_length=1)
    project: str = Field(min_length=1, max_length=128)
    external_id: str | None = None
    name: str | None = Field(default=None, max_length=16, pattern=r"^[a-zA-Z][a-zA-Z0-9-]*$")
    profile_name: str | None = None
    profile_project: str | None = None
    group: str | None = None
    duration: int = Field(default=4, ge=1, le=168)
    bindings: dict[str, Any] = Field(default_factory=dict)
    # Empty preserves the profile's hardware choice for existing allocations.
    node_types: list[
        Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]{1,32}$")]
    ] = Field(default_factory=list, max_length=20)
    node_type_binding: str = Field(
        default="phystype", min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
    )
    ssh_username: str | None = Field(default=None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")
    # Extra public key CloudLab installs for this experiment's SSH user.
    ssh_public_key: str | None = Field(default=None, max_length=8192)
    ssh_credential_ref: str | None = None
    ssh_known_hosts_ref: str | None = None
    # "known_hosts": every node key must already be in the administrator's
    # verified list. The registry also uses this old, rollback-readable value
    # with a reserved reference for explicitly unchecked CloudLab SSH.
    # "registration" pins the key supplied by the node's first-boot script.
    host_key_source: Literal["known_hosts", "registration"] = "known_hosts"

    @model_validator(mode="after")
    def validate_node_types(self) -> CloudLabConfig:
        """Reject ambiguous preference orders before any allocation request."""
        if len(self.node_types) != len(set(self.node_types)):
            raise ValueError("CloudLab preferred node types must be distinct")
        return self

    @field_validator("ssh_public_key")
    @classmethod
    def validate_ssh_public_key(cls, value: str | None) -> str | None:
        """Keep a blank key out of the persisted configuration."""
        if value is None or not value.strip():
            return None
        return normalize_user_public_key(value.strip())

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, value: str | None) -> str | None:
        """Canonicalize an experiment UUID before constructing a request URL."""
        return str(UUID(value)) if value else None


#: The largest Portal API body this adapter keeps — the ceiling it always had,
#: now applied while the body arrives instead of after all of it has.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
#: An error body is read only for its short ``error`` diagnostic.
_MAX_ERROR_BYTES = 16 * 1024
#: One request from connect to its last body byte. httpx's own timeout bounds
#: each read, so an endpoint that drips a byte at a time is bounded only here.
_REQUEST_DEADLINE_S = 180.0
_SAFE_DIAGNOSTIC_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SENSITIVE_DIAGNOSTIC_VALUE = re.compile(
    r"token|secret|password|credential|bearer|api[_-]?key|jwt", re.IGNORECASE
)


# Parameters of deploy/cloudlab/cloud-agent-node.py the boot script reads.
REGISTRATION_URL_BINDING = "registrationUrl"
REGISTRATION_TOKEN_BINDING = "registrationToken"


def _error_text(response: httpx.Response) -> str:
    """Return a short portal error, never a body that could echo a credential."""
    try:
        value = response.json()
    except (ValueError, UnicodeError):
        return ""
    if not isinstance(value, dict):
        return ""
    error = value.get("error")
    if not isinstance(error, str):
        return ""
    text = " ".join(error.split())
    if not text or text == "Internal server error" or len(text) > 300:
        return ""
    return text


def _wire_bindings(bindings: dict[str, Any]) -> dict[str, Any]:
    """Send parameter values the way the Portal stores them.

    Since 2026-10-09 the Portal answers a JSON boolean binding with HTTP 400
    ("Not a HASH reference"). It keeps every binding as a string and parses
    booleans from "True"/"False" itself, as experiments created earlier show.
    """
    return {
        key: ("True" if value else "False") if isinstance(value, bool) else value
        for key, value in bindings.items()
    }


def _capacity_rejection(diagnostic: str) -> bool:
    """Recognize only explicit node/resource shortages in a completed rejection."""
    return bool(
        re.search(
            r"\b(?:insufficient|not enough)\s+"
            r"(?:(?:available|free|physical|matching)\s+)*(?:nodes?|resources?)\b"
            r"|\b(?:node|resource) capacity (?:is )?(?:unavailable|exhausted|insufficient)\b",
            diagnostic,
            re.IGNORECASE,
        )
    )


def _safe_diagnostic_value(value: str) -> bool:
    """Accept short labels while rejecting obvious and opaque credential values."""
    return bool(
        _SAFE_DIAGNOSTIC_VALUE.fullmatch(value)
        and not _SENSITIVE_DIAGNOSTIC_VALUE.search(value)
        and not (len(value) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", value))
    )


def _operation_name(method: str, path: str) -> str:
    """Name the Portal operation without retaining its URL or query values."""
    parts = [part for part in path.split("?", 1)[0].strip("/").split("/") if part]
    method = method.upper()
    if parts and parts[0] == "experiments":
        if len(parts) == 1:
            return "experiments.create" if method == "POST" else "experiments.list"
        if len(parts) >= 3 and parts[2] == "manifests":
            return "experiments.manifests.read"
        return {
            "GET": "experiments.read",
            "PUT": "experiments.update",
            "DELETE": "experiments.delete",
        }.get(method, "experiments.request")
    if parts and parts[0] == "profiles":
        return "profiles.read" if method == "GET" else "profiles.request"
    return "provider.request"


def _error_reason_code(response: httpx.Response) -> str:
    """Map bounded provider text to a stable reason code; never return that text."""
    status = response.status_code
    if status in {401, 403}:
        return "authorization_denied"
    if status == 404:
        return "resource_not_found"
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        body = None
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, str) and code in {
            "capacity_unavailable",
            "invalid_parameter",
            "invalid_profile_binding",
            "permission_denied",
            "profile_not_found",
            "rate_limited",
            "reservation_conflict",
        }:
            return code
        error_parts: list[str] = []
        for key in ("error", "message", "detail"):
            value = body.get(key)
            if isinstance(value, str):
                error_parts.append(value)
            elif isinstance(value, list):
                # FastAPI validation responses put field messages in a list of
                # objects. Read only their public message field; never inspect
                # or retain ``input``, which can echo submitted credentials.
                error_parts.extend(
                    item["msg"]
                    for item in value
                    if isinstance(item, dict) and isinstance(item.get("msg"), str)
                )
    else:
        error_parts = []
    text = " ".join(" ".join(part.split()) for part in error_parts).lower()
    if status == 429:
        if "rate limit" in text or "too many requests" in text:
            return "rate_limited"
        if any(word in text for word in ("capacity", "free node", "physical node", "reservation")):
            return "capacity_unavailable"
        return "rate_limited_or_capacity"
    if status in {400, 422}:
        if "not a hash reference" in text or (
            any(word in text for word in ("binding", "parameter"))
            and any(word in text for word in ("invalid", "unknown", "required", "not a"))
        ):
            return "invalid_profile_binding"
        if "profile" in text:
            return "profile_rejected"
        if "project" in text and any(word in text for word in ("permission", "member", "denied")):
            return "project_access_denied"
        if status == 422:
            return "invalid_parameter"
        return "invalid_request"
    if status >= 500:
        return "provider_server_error"
    return "http_rejected"


def _safe_diagnostic_context(context: dict[str, Any] | None) -> dict[str, str]:
    """Retain only explicitly allowlisted, short public configuration labels."""
    if not isinstance(context, dict):
        return {}
    safe: dict[str, str] = {}
    for key in (
        "external_id",
        "requested_project",
        "requested_profile",
        "requested_profile_project",
        "requested_site",
        "requested_hardware",
    ):
        value = context.get(key)
        if not isinstance(value, str):
            continue
        if key == "external_id":
            try:
                safe[key] = str(UUID(value))
            except ValueError:
                continue
        elif _safe_diagnostic_value(value):
            safe[key] = value
    return safe


def _connection_failure_reason(error: BaseException) -> str:
    """Distinguish certificate failures without retaining provider exception text."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return "tls_verification_failed"
        # HTTPX can wrap OpenSSL failures in ConnectError. Inspect only to map
        # to a fixed label; never persist or log this text.
        message = str(current).lower()
        if "certificate" in message and any(
            phrase in message for phrase in ("verify", "expired", "self-signed", "unknown ca")
        ):
            return "tls_verification_failed"
        for cause in (current.__cause__, current.__context__):
            if isinstance(cause, BaseException):
                pending.append(cause)
    return "connect_failed"


def _diagnostic_context(
    config: CloudLabConfig, *, external_id: str | None = None
) -> dict[str, str]:
    """Extract only known-safe labels from a validated CloudLab configuration."""
    context: dict[str, str] = {"requested_project": config.project}
    if config.profile_name:
        context["requested_profile"] = config.profile_name
    if config.profile_project:
        context["requested_profile_project"] = config.profile_project
    if external_id:
        context["external_id"] = external_id
    for key in ("site", "cluster"):
        value = config.bindings.get(key)
        if isinstance(value, str):
            context["requested_site"] = value
            break
    for key in ("phystype", "hardware", "node_type"):
        value = config.bindings.get(key)
        if isinstance(value, str):
            context["requested_hardware"] = value
            break
    return context


class CloudLabProvider(ComputeProvider):
    """Adapt CloudLab experiments to the common resource lifecycle contract."""

    def __init__(
        self,
        *,
        api_url: str | Callable[[], str],
        resolver: SecretResolver,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if isinstance(api_url, str):
            api_url = self._validated_api_url(api_url)
        self._api_url = api_url
        self._resolver = resolver
        self._client = client

    @staticmethod
    def _validated_api_url(api_url: str) -> str:
        """Keep credentials on the configured HTTPS endpoint without redirects."""
        try:
            url = urlsplit(api_url)
            valid = (
                url.scheme == "https"
                and bool(url.hostname)
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
            _ = url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("CloudLab requires an operator-verified HTTPS API endpoint")
        return api_url.rstrip("/")

    def capabilities(self) -> ProviderCapabilities:
        """Return the actions this adapter implements."""
        return ProviderCapabilities(
            acquisition_modes=("attach", "provision"), can_extend=True, can_release=True
        )

    def validate_config(self, mode: str, config: dict[str, Any]) -> dict[str, Any]:
        """Validate a specific acquisition mode and return persistable parameters."""
        if mode not in self.capabilities().acquisition_modes:
            raise UnsupportedCapability(mode)
        parsed = CloudLabConfig.model_validate(config)
        if mode == "attach" and parsed.external_id is None:
            raise ProviderError(
                "invalid_configuration", "An experiment UUID is required for attach"
            )
        if mode == "provision" and (
            not parsed.profile_name or not parsed.profile_project or parsed.external_id
        ):
            raise ProviderError(
                "invalid_configuration",
                "Provision requires a profile and its project, without an existing experiment ID",
            )
        ssh_fields = (parsed.ssh_username, parsed.ssh_credential_ref)
        trusts_list = parsed.host_key_source == "known_hosts"
        if (any(ssh_fields) or parsed.ssh_known_hosts_ref) and not (
            all(ssh_fields) and (parsed.ssh_known_hosts_ref or not trusts_list)
        ):
            raise ProviderError(
                "invalid_configuration",
                "SSH username, credential and host-key policy must be supplied together",
            )
        if not trusts_list and mode != "provision":
            raise ProviderError(
                "invalid_configuration",
                "Only a node created from the Cloud Agent profile can register its host keys",
            )
        return parsed.model_dump(mode="json", exclude_none=True)

    async def validate_connection(self, credential_ref: str) -> None:
        """Validate token access without creating or changing any experiment."""
        # The deployed Portal API uses a trailing slash for the collection.
        # Call it directly: redirects must never carry account credentials.
        data = self._json(
            await self._request("GET", "/experiments/", credential_ref),
            method="GET",
            path="/experiments/",
        )
        if not isinstance(data.get("experiments"), list):
            raise ProviderError(
                "invalid_response", "CloudLab API endpoint did not return an experiment list"
            )

    async def _request(
        self,
        method: str,
        path: str,
        credential_ref: str,
        *,
        payload: dict[str, Any] | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> httpx.Response:
        # Snapshot the endpoint before awaiting credentials, so an admin edit
        # cannot change the destination midway through this request.
        try:
            api_url = self._validated_api_url(
                self._api_url() if callable(self._api_url) else self._api_url
            )
        except ValueError:
            raise ProviderError(
                "provider_unavailable", "CloudLab API endpoint is not configured correctly"
            ) from None
        token = (await self._resolver.resolve(credential_ref)).strip()
        if not token or "\n" in token or "\r" in token:
            raise ProviderError("credential_invalid", "CloudLab credential is missing or malformed")
        headers = {"X-Api-Token": token, "X-Api-Elaborate": "true", **IDENTITY_ENCODING_HEADERS}
        try:
            async with asyncio.timeout(_REQUEST_DEADLINE_S):
                if self._client is not None:
                    response = await self._exchange(
                        self._client, method, api_url + path, headers, payload
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await self._exchange(
                            client, method, api_url + path, headers, payload
                        )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # No byte of the request left this process: the TCP or TLS
            # handshake failed (the API host has intermittently served an
            # expired certificate). Even a mutation is safe to send again.
            raise ProviderError(
                "temporarily_unavailable",
                "CloudLab could not be reached",
                retryable=True,
                diagnostics=self._network_diagnostics(
                    method,
                    path,
                    diagnostic_context,
                    _connection_failure_reason(exc),
                ),
            ) from None
        except (httpx.RequestError, TimeoutError):
            mutation = method != "GET"
            raise ProviderError(
                "result_unknown" if mutation else "temporarily_unavailable",
                "CloudLab request outcome is unknown"
                if mutation
                else "CloudLab could not be reached",
                retryable=not mutation,
                diagnostics=self._network_diagnostics(
                    method,
                    path,
                    diagnostic_context,
                    "request_outcome_unknown" if mutation else "request_failed",
                ),
            ) from None
        status = response.status_code
        diagnostics = self._response_diagnostics(response, method, path, diagnostic_context)
        if status in {401, 403}:
            raise ProviderError(
                "credential_invalid" if status == 401 else "permission_denied",
                "CloudLab denied access",
                diagnostics=diagnostics,
            )
        if status == 404:
            raise ProviderError(
                "not_found", "CloudLab experiment was not found", diagnostics=diagnostics
            )
        if status == 429:
            capacity = _capacity_rejection(_error_text(response))
            raise ProviderError(
                "capacity_unavailable" if capacity else "rate_limited",
                "CloudLab has no matching node capacity"
                if capacity
                else "CloudLab request limit reached",
                retryable=True,
                diagnostics={
                    **diagnostics,
                    "provider_reason_code": (
                        "capacity_unavailable" if capacity else _error_reason_code(response)
                    ),
                },
            )
        if status >= 500:
            # A finished HTTP 500 on create is not a lost request: the portal
            # answered. Acquire looks up the experiment name and, when it is
            # absent, reports a rejection that can be corrected and retried.
            # Timeouts stay result_unknown and are never posted again.
            code = (
                "temporarily_unavailable"
                if method == "GET"
                else "server_error"
                if method == "POST" and path.split("?", 1)[0].rstrip("/") == "/experiments"
                else "result_unknown"
            )
            detail = _error_text(response)
            raise ProviderError(
                code,
                detail or "CloudLab could not confirm the request",
                retryable=method == "GET",
                diagnostics=diagnostics,
            )
        if status >= 300:
            raise ProviderError(
                "request_rejected", "CloudLab rejected the request", diagnostics=diagnostics
            )
        return response

    @staticmethod
    async def _exchange(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
    ) -> httpx.Response:
        """Stream bounded response bodies and retain only safe request metadata."""
        async with client.stream(
            method,
            url,
            headers=headers,
            json=payload,
            follow_redirects=False,
            timeout=60,
        ) as streamed:
            status = streamed.status_code
            limit = (
                _MAX_RESPONSE_BYTES
                if status < 300
                else _MAX_ERROR_BYTES
                if status in {400, 422, 429} or status >= 500
                else 0
            )
            body = bytearray()
            too_large = False
            if limit:
                async for chunk in iter_response_bytes(streamed):
                    if len(body) + len(chunk) > limit:
                        too_large = status < 300
                        body.clear()
                        break
                    body.extend(chunk)
            safe_headers: dict[str, str] = {}
            content_type = streamed.headers.get("content-type")
            if content_type:
                safe_headers["content-type"] = content_type[:128]
            request_id = streamed.headers.get("x-request-id")
            if request_id:
                with suppress(ValueError):
                    safe_headers["x-request-id"] = str(UUID(request_id))
            response = httpx.Response(
                status,
                headers=safe_headers,
                content=bytes(body),
                request=streamed.request,
            )
            if too_large:
                response.extensions["cloudlab_response_too_large"] = True
            return response

    @staticmethod
    def _response_diagnostics(
        response: httpx.Response,
        method: str,
        path: str,
        context: dict[str, Any] | None,
        *,
        reason_code: str | None = None,
    ) -> dict[str, str | int | bool]:
        diagnostics: dict[str, str | int | bool] = {
            "provider_operation": _operation_name(method, path),
            "http_status": response.status_code,
            "provider_reason_code": reason_code or _error_reason_code(response),
        }
        request_id = response.headers.get("x-request-id")
        if request_id:
            with suppress(ValueError):
                diagnostics["provider_request_id"] = str(UUID(request_id))
        diagnostics.update(_safe_diagnostic_context(context))
        return diagnostics

    @staticmethod
    def _network_diagnostics(
        method: str,
        path: str,
        context: dict[str, Any] | None,
        reason_code: str,
    ) -> dict[str, str | int | bool]:
        diagnostics: dict[str, str | int | bool] = {
            "provider_operation": _operation_name(method, path),
            "provider_reason_code": reason_code,
        }
        diagnostics.update(_safe_diagnostic_context(context))
        return diagnostics

    @classmethod
    def _json(
        cls,
        response: httpx.Response,
        *,
        method: str = "GET",
        path: str = "/",
        diagnostic_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (
            response.extensions.get("cloudlab_response_too_large") is True
            or len(response.content) > _MAX_RESPONSE_BYTES
        ):
            reason_code = "response_too_large"
            message = "CloudLab response exceeds the size limit"
        else:
            try:
                value = response.json()
            except (ValueError, UnicodeError):
                reason_code = "invalid_json_response"
                message = "CloudLab returned invalid JSON"
            else:
                if isinstance(value, dict):
                    return value
                reason_code = "invalid_response_shape"
                message = "CloudLab returned an invalid object"
        diagnostics = cls._response_diagnostics(
            response, method, path, diagnostic_context, reason_code=reason_code
        )
        raise ProviderError("invalid_response", message, diagnostics=diagnostics)

    async def _require_profile(self, config: CloudLabConfig) -> None:
        """Reject a missing profile before create. A 500 from that create is not recoverable."""
        query = urlencode(
            {"project": config.profile_project or "", "profile_name": config.profile_name or ""}
        )
        path = "/profiles/?" + query
        context = _diagnostic_context(config)
        response = await self._request(
            "GET", path, config.credential_ref, diagnostic_context=context
        )
        data = self._json(response, method="GET", path=path, diagnostic_context=context)
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            raise ProviderError(
                "invalid_response",
                "CloudLab API endpoint did not return a profile list",
                diagnostics=self._response_diagnostics(
                    response,
                    "GET",
                    path,
                    context,
                    reason_code="invalid_response_shape",
                ),
            )
        if any(
            isinstance(item, dict)
            and item.get("name") == config.profile_name
            and item.get("project") == config.profile_project
            for item in profiles
        ):
            return
        raise ProviderError(
            "request_rejected",
            f"CloudLab project {config.profile_project} has no profile {config.profile_name}.",
            diagnostics=self._response_diagnostics(
                response, "GET", path, context, reason_code="profile_not_found"
            ),
        )

    async def _created_experiment(self, name: str, config: CloudLabConfig) -> str | None:
        """Return the id of the experiment this create would have named, if it exists."""
        query = urlencode({"project": config.project})
        path = "/experiments/?" + query
        context = _diagnostic_context(config)
        response = await self._request(
            "GET", path, config.credential_ref, diagnostic_context=context
        )
        data = self._json(response, method="GET", path=path, diagnostic_context=context)
        experiments = data.get("experiments")
        if not isinstance(experiments, list):
            raise ProviderError(
                "invalid_response",
                "CloudLab API endpoint did not return an experiment list",
                diagnostics=self._response_diagnostics(
                    response,
                    "GET",
                    path,
                    context,
                    reason_code="invalid_response_shape",
                ),
            )
        matches = [
            item.get("id")
            for item in experiments
            if isinstance(item, dict)
            and item.get("name") == name
            and item.get("project") == config.project
            and item.get("id")
        ]
        if len(matches) > 1:
            raise ProviderError(
                "result_unknown",
                "CloudLab returned more than one experiment with this name",
                diagnostics=self._response_diagnostics(
                    response,
                    "GET",
                    path,
                    context,
                    reason_code="resource_identity_mismatch",
                ),
            )
        if not matches:
            return None
        return str(UUID(str(matches[0])))

    async def acquire(self, request: AcquireRequest, intent: OperationIntent) -> ProviderReceipt:
        """Execute the persisted acquisition intent and return its acknowledgement."""
        validated = self.validate_config(request.mode, request.config)
        config = CloudLabConfig.model_validate(validated)
        if request.mode == "attach":
            observation = await self.inspect(
                ResourceRef(external_id=str(config.external_id), config=validated)
            )
            if observation.state == "terminated":
                raise ProviderError("not_found", "The experiment no longer exists")
            return self._receipt(observation)
        # No vendor idempotency key exists. The orchestrator must call this
        # only on an unsent persisted intent; recovery must never re-POST it.
        name = config.name or "ca-" + intent.allocation_id.replace("-", "")[:13]
        bindings = dict(config.bindings)
        if config.host_key_source == "registration":
            if request.registration is None:
                raise ProviderError(
                    "registration_unavailable", "Node registration is not configured"
                )
            # Only the outgoing request carries the token; the persisted
            # configuration below never does.
            bindings[REGISTRATION_URL_BINDING] = request.registration.url
            bindings[REGISTRATION_TOKEN_BINDING] = request.registration.token.get_secret_value()
        payload = {
            "name": name,
            "project": config.project,
            "profile_name": config.profile_name,
            "profile_project": config.profile_project,
            "duration": config.duration,
            "bindings": _wire_bindings(bindings),
        }
        if config.group:
            payload["group"] = config.group
        if config.ssh_public_key:
            payload["sshpubkey"] = config.ssh_public_key
        await self._require_profile(config)
        explicit_type = config.bindings.get(config.node_type_binding)
        pinned = explicit_type is not None and (
            not isinstance(explicit_type, str) or bool(explicit_type.strip())
        )
        candidates: list[str | None] = list(config.node_types) if not pinned else []
        if not candidates:
            candidates = [None]
        for index, node_type in enumerate(candidates):
            selected_bindings = dict(config.bindings)
            response: httpx.Response | None = None
            receipt_diagnostics: dict[str, str | int | bool] = {}
            if node_type is not None:
                selected_bindings[config.node_type_binding] = node_type
                payload["bindings"] = _wire_bindings(
                    {**bindings, config.node_type_binding: node_type}
                )
            try:
                context = _diagnostic_context(config)
                response = await self._request(
                    "POST",
                    "/experiments/",
                    config.credential_ref,
                    payload=payload,
                    diagnostic_context=context,
                )
                data = self._json(
                    response,
                    method="POST",
                    path="/experiments/",
                    diagnostic_context=context,
                )
                external_id = str(UUID(str(data["id"])))
                receipt_diagnostics = self._response_diagnostics(
                    response,
                    "POST",
                    "/experiments/",
                    _diagnostic_context(config, external_id=external_id),
                    reason_code="request_accepted",
                )
            except ProviderError as exc:
                if exc.code == "server_error":
                    try:
                        created = await self._created_experiment(name, config)
                    except ProviderError as lookup_error:
                        return ProviderReceipt(
                            outcome="unknown",
                            diagnostics=lookup_error.diagnostics or exc.diagnostics,
                        )
                    if created is None:
                        if not _capacity_rejection(str(exc)):
                            raise ProviderError(
                                "request_rejected",
                                "CloudLab did not create the experiment. "
                                "Check the profile name and its parameters.",
                                diagnostics=exc.diagnostics,
                            ) from None
                        exc = ProviderError(
                            "capacity_unavailable",
                            "CloudLab has no matching node capacity",
                            retryable=True,
                            diagnostics=exc.diagnostics,
                        )
                    else:
                        external_id = created
                        receipt_diagnostics = exc.diagnostics
                elif exc.code in {"result_unknown", "invalid_response"}:
                    return ProviderReceipt(outcome="unknown", diagnostics=exc.diagnostics)
                if exc.code == "capacity_unavailable":
                    if index + 1 < len(candidates):
                        continue
                    raise exc from None
                if exc.code != "server_error":
                    raise
            except (KeyError, ValueError):
                diagnostics = (
                    self._response_diagnostics(
                        response,
                        "POST",
                        "/experiments/",
                        _diagnostic_context(config),
                        reason_code="missing_or_invalid_experiment_id",
                    )
                    if response is not None
                    else {}
                )
                return ProviderReceipt(outcome="unknown", diagnostics=diagnostics)
            ref = ResourceRef(
                external_id=external_id,
                config={
                    **validated,
                    "bindings": selected_bindings,
                    "name": name,
                    "external_id": external_id,
                },
            )
            # Save the immutable resource ID and selected type immediately,
            # even if its subsequent status request fails.
            return ProviderReceipt(
                outcome="pending",
                resource=ref,
                diagnostics=receipt_diagnostics,
            )
        raise AssertionError("CloudLab acquisition has no hardware candidates")

    async def recover_acquire(self, intent: OperationIntent) -> AcquisitionResolution:
        """Resolve a previous acquisition without blindly creating another resource."""
        if intent.acquisition is None:
            return AcquisitionResolution(outcome="unknown")
        config = self.validate_config(intent.acquisition.mode, intent.acquisition.config)
        external_id = intent.known_external_id or config.get("external_id")
        if not external_id:
            # A name/project/time match is not proof of ownership. In particular,
            # an empty list cannot prove a delayed create will never complete.
            return AcquisitionResolution(outcome="unknown")
        observation = await self.inspect(ResourceRef(external_id=external_id, config=config))
        receipt = self._receipt(observation)
        return AcquisitionResolution(outcome=receipt.outcome, receipt=receipt)

    @staticmethod
    def _receipt(observation: ResourceObservation) -> ProviderReceipt:
        return ProviderReceipt(
            outcome="confirmed"
            if observation.state in {"ready", "terminated", "failed"}
            else "pending",
            resource=observation.ref,
            observation=observation,
        )

    async def inspect(self, ref: ResourceRef) -> ResourceObservation:
        """Observe the exact resource identity and its full known node scope."""
        config = CloudLabConfig.model_validate(ref.config)
        external_id = str(UUID(ref.external_id))
        try:
            diagnostic_context = _diagnostic_context(config, external_id=external_id)
            response = await self._request(
                "GET",
                f"/experiments/{external_id}",
                config.credential_ref,
                diagnostic_context=diagnostic_context,
            )
        except ProviderError as exc:
            if exc.code != "not_found":
                raise
            # A valid token or empty experiment list does not prove access to
            # this resource's project. Credentials and membership can change;
            # the public API does not promise visibility-independent absence.
            raise ProviderError(
                "resource_visibility_unknown",
                "CloudLab cannot confirm this experiment's existence or visibility",
                diagnostics=exc.diagnostics,
            ) from None
        data = self._json(
            response,
            method="GET",
            path=f"/experiments/{external_id}",
            diagnostic_context=diagnostic_context,
        )
        if data.get("id") != external_id or data.get("project") != config.project:
            raise ProviderError(
                "resource_identity_mismatch",
                "CloudLab experiment identity or project differs from the authorized scope",
                diagnostics=self._response_diagnostics(
                    response,
                    "GET",
                    f"/experiments/{external_id}",
                    diagnostic_context,
                    reason_code="resource_identity_mismatch",
                ),
            )
        raw = str(data.get("status", "unknown")).lower()
        state: Literal["pending", "ready", "failed", "terminated", "unknown"] = (
            "ready"
            if raw == "ready"
            else "terminated"
            if raw in {"terminated", "destroyed"}
            else "failed"
            if raw in {"failed", "failure"}
            else "pending"
            if raw
            in {
                "created",
                "creating",
                "provisioning",
                "provisioned",
                "imaging",
                "booting",
                "scheduled",
                "deferred",
                "terminating",
            }
            else "unknown"
        )
        expires = None
        if data.get("expires_at"):
            try:
                if not isinstance(data["expires_at"], str):
                    raise ValueError("Invalid timestamp")
                expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError("Missing timezone")
            except (TypeError, ValueError):
                raise ProviderError(
                    "invalid_response",
                    "CloudLab expiry is not a timezone-aware timestamp",
                    diagnostics=self._response_diagnostics(
                        response,
                        "GET",
                        f"/experiments/{external_id}",
                        diagnostic_context,
                        reason_code="invalid_expiry_timestamp",
                    ),
                ) from None
        nodes, complete = await self._nodes(ref, config, data, state == "ready")
        return ResourceObservation(
            ref=ref,
            state=state,
            raw_state=raw[:128],
            observed_at=datetime.now(timezone.utc),
            expires_at=expires,
            lease_known=expires is not None,
            nodes=tuple(nodes),
            scope_complete=complete,
            provider_http_status=response.status_code,
            provider_request_id=response.headers.get("x-request-id"),
        )

    async def _nodes(
        self, ref: ResourceRef, config: CloudLabConfig, data: dict[str, Any], ready: bool
    ) -> tuple[list[NodeObservation], bool]:
        aggregates = data.get("aggregates")
        if not isinstance(aggregates, dict) or not aggregates:
            return [], False
        logins: dict[tuple[str, str], tuple[str, int]] = {}
        if ready and config.ssh_username:
            path = f"/experiments/{ref.external_id}/manifests"
            diagnostic_context = _diagnostic_context(config, external_id=ref.external_id)
            manifest_response = await self._request(
                "GET",
                path,
                config.credential_ref,
                diagnostic_context=diagnostic_context,
            )
            manifests = self._json(
                manifest_response,
                method="GET",
                path=path,
                diagnostic_context=diagnostic_context,
            )
            for aggregate_id, manifest in manifests.items():
                if (
                    not isinstance(manifest, str)
                    or len(manifest) > 2 * 1024 * 1024
                    or "<!DOCTYPE" in manifest.upper()
                    or "<!ENTITY" in manifest.upper()
                ):
                    raise ProviderError(
                        "invalid_response",
                        "CloudLab returned an invalid manifest",
                        diagnostics=self._response_diagnostics(
                            manifest_response,
                            "GET",
                            path,
                            diagnostic_context,
                            reason_code="invalid_manifest",
                        ),
                    )
                try:
                    root = ElementTree.fromstring(manifest)
                    for node in root.findall("{*}node"):
                        for login in node.findall("{*}services/{*}login"):
                            if (
                                login.get("authentication") == "ssh-keys"
                                and login.get("username") == config.ssh_username
                            ):
                                address, port = login.get("hostname"), int(login.get("port", "22"))
                                if address:
                                    logins[(aggregate_id, str(node.get("client_id")))] = (
                                        address,
                                        port,
                                    )
                except (ElementTree.ParseError, ValueError):
                    raise ProviderError(
                        "invalid_response",
                        "CloudLab returned an invalid SSH manifest",
                        diagnostics=self._response_diagnostics(
                            manifest_response,
                            "GET",
                            path,
                            diagnostic_context,
                            reason_code="invalid_ssh_manifest",
                        ),
                    ) from None
        result: list[NodeObservation] = []
        complete = ready
        identities: set[str] = set()
        for aggregate_id, aggregate in aggregates.items():
            if (
                not isinstance(aggregate, dict)
                or not isinstance(aggregate.get("nodes"), list)
                or not aggregate["nodes"]
            ):
                complete = False
                continue
            for node in aggregate["nodes"]:
                if not isinstance(node, dict) or not node.get("urn") or not node.get("client_id"):
                    complete = False
                    continue
                node_id = f"{aggregate_id}:{node['client_id']}"
                if node_id in identities:
                    raise ProviderError(
                        "invalid_response", "CloudLab returned duplicate node identities"
                    )
                identities.add(node_id)
                connection = None
                if (
                    config.ssh_username
                    and config.ssh_credential_ref
                    and (config.ssh_known_hosts_ref or config.host_key_source == "registration")
                ):
                    # Manifest is authoritative for forwarded VM SSH ports.
                    # Never silently assume port 22 for an unobserved login.
                    destination = logins.get((aggregate_id, str(node["client_id"])))
                    if destination:
                        try:
                            connection = SshConnection(
                                address=destination[0],
                                port=destination[1],
                                username=config.ssh_username,
                                credential_ref=config.ssh_credential_ref,
                                # A registering node is trusted only through
                                # the key it registered, never the list.
                                known_hosts_ref=config.ssh_known_hosts_ref
                                if config.host_key_source == "known_hosts"
                                else None,
                            )
                        except ValidationError:
                            raise ProviderError(
                                "invalid_response", "CloudLab returned an invalid SSH destination"
                            ) from None
                result.append(
                    NodeObservation(
                        provider_node_id=node_id,
                        instance_identity=f"{ref.external_id}:{node_id}:{node['urn']}",
                        display_name=str(node.get("hostname") or node["client_id"]),
                        connection=connection,
                    )
                )
        return result, complete and bool(result)

    async def extend(self, ref: ResourceRef, request: ExtendRequest) -> ProviderReceipt:
        """Request the fixed expiration target and preserve its actual observed result."""
        config = CloudLabConfig.model_validate(ref.config)
        if request.target_expires_at.tzinfo is None:
            raise ProviderError("invalid_configuration", "Extension target must include a timezone")
        external_id = str(UUID(ref.external_id))
        diagnostic_context = _diagnostic_context(config, external_id=external_id)
        try:
            response = await self._request(
                "PUT",
                f"/experiments/{external_id}",
                config.credential_ref,
                diagnostic_context=diagnostic_context,
                payload={
                    "expires_at": request.target_expires_at.astimezone(timezone.utc).isoformat(),
                    "reason": request.reason,
                },
            )
        except ProviderError as exc:
            if exc.code == "result_unknown":
                return ProviderReceipt(outcome="unknown", resource=ref)
            raise
        # The documented success body is an Experiment, not an Error with
        # HTTP 200. An undecodable response leaves the mutation uncertain and
        # never causes blind replay or false acceptance.
        try:
            response_body = self._json(
                response,
                method="PUT",
                path=f"/experiments/{external_id}",
                diagnostic_context=diagnostic_context,
            )
        except ProviderError as exc:
            return ProviderReceipt(outcome="unknown", resource=ref, diagnostics=exc.diagnostics)
        if response.status_code != 200 or response_body.get("id") != external_id:
            return ProviderReceipt(
                outcome="unknown",
                resource=ref,
                diagnostics=self._response_diagnostics(
                    response,
                    "PUT",
                    f"/experiments/{external_id}",
                    diagnostic_context,
                    reason_code="resource_identity_mismatch",
                ),
            )
        # PUT acceptance, including a partial extension, is not confirmation.
        try:
            observation = await self.inspect(ref)
        except ProviderError as exc:
            return ProviderReceipt(outcome="unknown", resource=ref, diagnostics=exc.diagnostics)
        outcome: Literal["confirmed", "pending"] = (
            "confirmed"
            if observation.expires_at and request.granted_by(observation.expires_at)
            else "pending"
        )
        return ProviderReceipt(outcome=outcome, resource=ref, observation=observation)

    async def release(self, ref: ResourceRef, intent: OperationIntent) -> ProviderReceipt:
        """Terminate the immutable experiment UUID; exact-ID DELETE replay is safe.

        A second DELETE cannot affect a later experiment with the same name.
        Only documented 204 acknowledgement proves deletion. A 404 may reflect
        lost visibility and cannot confirm a previously ambiguous operation.
        """
        config = CloudLabConfig.model_validate(ref.config)
        external_id = str(UUID(ref.external_id))
        diagnostic_context = _diagnostic_context(config, external_id=external_id)
        try:
            response = await self._request(
                "DELETE",
                f"/experiments/{external_id}",
                config.credential_ref,
                diagnostic_context=diagnostic_context,
            )
        except ProviderError as exc:
            if exc.code in {"result_unknown", "not_found"}:
                return ProviderReceipt(outcome="unknown", resource=ref, diagnostics=exc.diagnostics)
            raise
        # The official contract defines 204 as "Experiment deleted". Save
        # that evidence immediately rather than lose it to a failing GET.
        if response.status_code != 204:
            return ProviderReceipt(
                outcome="unknown",
                resource=ref,
                diagnostics=self._response_diagnostics(
                    response,
                    "DELETE",
                    f"/experiments/{external_id}",
                    diagnostic_context,
                    reason_code="http_rejected",
                ),
            )
        return ProviderReceipt(
            outcome="confirmed",
            resource=ref,
            diagnostics=self._response_diagnostics(
                response,
                "DELETE",
                f"/experiments/{external_id}",
                diagnostic_context,
                reason_code="request_accepted",
            ),
            observation=ResourceObservation(
                ref=ref,
                state="terminated",
                raw_state="delete_acknowledged",
                observed_at=datetime.now(timezone.utc),
                lease_known=True,
                scope_complete=True,
            ),
        )
