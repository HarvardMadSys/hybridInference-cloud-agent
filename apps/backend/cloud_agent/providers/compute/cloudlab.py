"""CloudLab Portal API adapter.

Wire contract: https://gitlab.flux.utah.edu/emulab/portal-api/-/blob/master/openapi.json
The public spec's example server is a development HTTP endpoint. Production
must supply its verified HTTPS endpoint; credentials never follow redirects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from xml.etree import ElementTree

import httpx
from pydantic import Field, ValidationError, field_validator

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
    ssh_username: str | None = Field(default=None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")
    ssh_credential_ref: str | None = None
    ssh_known_hosts_ref: str | None = None
    # "known_hosts": every node key must already be in the administrator's
    # verified list. "registration": the profile's boot script registers the
    # node's keys with a one-time token (deploy/cloudlab/cloud-agent-node.py).
    host_key_source: Literal["known_hosts", "registration"] = "known_hosts"

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, value: str | None) -> str | None:
        """Canonicalize an experiment UUID before constructing a request URL."""
        return str(UUID(value)) if value else None


# Parameters of deploy/cloudlab/cloud-agent-node.py the boot script reads.
REGISTRATION_URL_BINDING = "registrationUrl"
REGISTRATION_TOKEN_BINDING = "registrationToken"


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


class CloudLabProvider(ComputeProvider):
    """Adapt CloudLab experiments to the common resource lifecycle contract."""

    def __init__(
        self, *, api_url: str, resolver: SecretResolver, client: httpx.AsyncClient | None = None
    ) -> None:
        url = urlsplit(api_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("CloudLab requires an operator-verified HTTPS API endpoint")
        self._api_url = api_url.rstrip("/")
        self._resolver = resolver
        self._client = client

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
                "SSH username, credential and verified host keys must be supplied together",
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
        data = self._json(await self._request("GET", "/experiments/", credential_ref))
        if not isinstance(data.get("experiments"), list):
            raise ProviderError(
                "invalid_response", "CloudLab API endpoint did not return an experiment list"
            )

    async def _request(
        self, method: str, path: str, credential_ref: str, *, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        token = (await self._resolver.resolve(credential_ref)).strip()
        if not token or "\n" in token or "\r" in token:
            raise ProviderError("credential_invalid", "CloudLab credential is missing or malformed")
        try:
            if self._client is not None:
                response = await self._client.request(
                    method,
                    self._api_url + path,
                    headers={"X-Api-Token": token, "X-Api-Elaborate": "true"},
                    json=payload,
                    follow_redirects=False,
                    timeout=60,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method,
                        self._api_url + path,
                        headers={"X-Api-Token": token, "X-Api-Elaborate": "true"},
                        json=payload,
                        follow_redirects=False,
                        timeout=60,
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # No byte of the request left this process: the TCP or TLS
            # handshake failed (the API host has intermittently served an
            # expired certificate). Even a mutation is safe to send again.
            raise ProviderError(
                "temporarily_unavailable", "CloudLab could not be reached", retryable=True
            ) from None
        except httpx.RequestError:
            mutation = method != "GET"
            raise ProviderError(
                "result_unknown" if mutation else "temporarily_unavailable",
                "CloudLab request outcome is unknown"
                if mutation
                else "CloudLab could not be reached",
                retryable=not mutation,
            ) from None
        status = response.status_code
        if status in {401, 403}:
            raise ProviderError(
                "credential_invalid" if status == 401 else "permission_denied",
                "CloudLab denied access",
            )
        if status == 404:
            raise ProviderError("not_found", "CloudLab experiment was not found")
        if status == 429:
            raise ProviderError(
                "capacity_unavailable", "CloudLab capacity or request limit reached", retryable=True
            )
        if status >= 500:
            raise ProviderError(
                "temporarily_unavailable" if method == "GET" else "result_unknown",
                "CloudLab could not confirm the request",
                retryable=method == "GET",
            )
        if status >= 300:
            raise ProviderError("request_rejected", "CloudLab rejected the request")
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if len(response.content) > 4 * 1024 * 1024:
            raise ProviderError("invalid_response", "CloudLab response exceeds the size limit")
        try:
            value = response.json()
        except (ValueError, UnicodeError):
            raise ProviderError("invalid_response", "CloudLab returned invalid JSON") from None
        if not isinstance(value, dict):
            raise ProviderError("invalid_response", "CloudLab returned an invalid object")
        return value

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
        try:
            response = await self._request(
                "POST", "/experiments/", config.credential_ref, payload=payload
            )
            data = self._json(response)
            external_id = str(UUID(str(data["id"])))
        except ProviderError as exc:
            if exc.code in {"result_unknown", "invalid_response"}:
                return ProviderReceipt(outcome="unknown")
            raise
        except (KeyError, ValueError):
            return ProviderReceipt(outcome="unknown")
        ref = ResourceRef(
            external_id=external_id, config={**validated, "name": name, "external_id": external_id}
        )
        # Save the immutable resource ID immediately, even if its subsequent
        # status request fails. Its presence makes crash recovery unambiguous.
        return ProviderReceipt(outcome="pending", resource=ref)

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
            response = await self._request(
                "GET", f"/experiments/{external_id}", config.credential_ref
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
            ) from None
        data = self._json(response)
        if data.get("id") != external_id or data.get("project") != config.project:
            raise ProviderError(
                "resource_identity_mismatch",
                "CloudLab experiment identity or project differs from the authorized scope",
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
                    "invalid_response", "CloudLab expiry is not a timezone-aware timestamp"
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
        )

    async def _nodes(
        self, ref: ResourceRef, config: CloudLabConfig, data: dict[str, Any], ready: bool
    ) -> tuple[list[NodeObservation], bool]:
        aggregates = data.get("aggregates")
        if not isinstance(aggregates, dict) or not aggregates:
            return [], False
        logins: dict[tuple[str, str], tuple[str, int]] = {}
        if ready and config.ssh_username:
            manifests = self._json(
                await self._request(
                    "GET", f"/experiments/{ref.external_id}/manifests", config.credential_ref
                )
            )
            for aggregate_id, manifest in manifests.items():
                if (
                    not isinstance(manifest, str)
                    or len(manifest) > 2 * 1024 * 1024
                    or "<!DOCTYPE" in manifest.upper()
                    or "<!ENTITY" in manifest.upper()
                ):
                    raise ProviderError("invalid_response", "CloudLab returned an invalid manifest")
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
                        "invalid_response", "CloudLab returned an invalid SSH manifest"
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
        try:
            response = await self._request(
                "PUT",
                f"/experiments/{external_id}",
                config.credential_ref,
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
            response_body = self._json(response)
        except ProviderError:
            return ProviderReceipt(outcome="unknown", resource=ref)
        if response.status_code != 200 or response_body.get("id") != external_id:
            return ProviderReceipt(outcome="unknown", resource=ref)
        # PUT acceptance, including a partial extension, is not confirmation.
        try:
            observation = await self.inspect(ref)
        except ProviderError:
            return ProviderReceipt(outcome="unknown", resource=ref)
        outcome: Literal["confirmed", "pending"] = (
            "confirmed"
            if observation.expires_at and observation.expires_at >= request.target_expires_at
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
        try:
            response = await self._request(
                "DELETE", f"/experiments/{external_id}", config.credential_ref
            )
        except ProviderError as exc:
            if exc.code in {"result_unknown", "not_found"}:
                return ProviderReceipt(outcome="unknown", resource=ref)
            raise
        # The official contract defines 204 as "Experiment deleted". Save
        # that evidence immediately rather than lose it to a failing GET.
        if response.status_code != 204:
            return ProviderReceipt(outcome="unknown", resource=ref)
        return ProviderReceipt(
            outcome="confirmed",
            resource=ref,
            observation=ResourceObservation(
                ref=ref,
                state="terminated",
                raw_state="delete_acknowledged",
                observed_at=datetime.now(timezone.utc),
                lease_known=True,
                scope_complete=True,
            ),
        )
