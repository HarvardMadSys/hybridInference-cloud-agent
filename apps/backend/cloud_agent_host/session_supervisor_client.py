"""Synchronous runner client for the host's private Session Supervisor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from cloud_agent_host.bounded_output import (
    MAX_PROTOCOL_LINE_BYTES,
    protocol_lines,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterator

    from cloud_agent_host.chat_sandbox import ChatSandboxSpec
    from cloud_agent_host.resident_transport import ResidentLaunch


@dataclass
class SharedTurnStream:
    """One sandbox-manager-owned resident turn streamed as bounded NDJSON frames."""

    _response: httpx.Response

    def frames(self) -> Iterator[dict[str, Any]]:
        """Yield validated object frames until result/error."""
        # Frames include both normalized content and the raw CLI record, plus
        # JSON escaping. Keep HTTP framing bounded before JSON parsing too.
        for line in protocol_lines(
            self._response.iter_text(),
            max_bytes=MAX_PROTOCOL_LINE_BYTES * 4,
        ):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionSupervisorError(502, "shared turn returned invalid JSON") from exc
            if not isinstance(value, dict):
                raise SessionSupervisorError(502, "shared turn returned an invalid frame")
            yield value

    def close(self) -> None:
        """Release the streaming HTTP response."""
        self._response.close()


@dataclass
class SessionSupervisorError(RuntimeError):
    """Bounded failure returned by the private host lifecycle service."""

    status_code: int
    message: str

    def __str__(self) -> str:
        return self.message


#: Where a FastAPI validation error says a field came from. Dropped from the
#: field path: every lifecycle field is in the body, so it only adds noise.
_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})


def _validation_problems(detail: Any) -> str:
    """Name the fields a FastAPI validation error rejected, never their values.

    The values stay out: the field most likely to exceed its bound is a
    credential, and this text becomes an event the chat's owner reads.
    """
    if not isinstance(detail, list):
        return ""
    problems: list[str] = []
    for item in detail[:3]:
        if not isinstance(item, dict):
            continue
        location = item.get("loc")
        parts = location if isinstance(location, list) else []
        if parts and parts[0] in _REQUEST_PARTS:
            parts = parts[1:]
        field_path = ".".join(str(part) for part in parts)
        message = str(item.get("msg") or "is invalid")[:200]
        problems.append(f"{field_path}: {message}" if field_path else message)
    return "; ".join(problems)


def _error_detail(response: httpx.Response, refused: str) -> str:
    """The sandbox manager's own reason for an error response, or the most a caller can say.

    A refusal the sandbox manager anticipated carries its reason as a string
    ``detail``. Anything else used to collapse into ``refused`` alone, which
    named neither the call nor the cause: a request the sandbox manager's models
    rejected, whose ``detail`` is a list, or a crash an older sandbox manager answered
    as plain text. The status, and the fields a validation error names, are
    what make that line findable in the sandbox manager's log.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    reason = f"{refused}: HTTP {response.status_code} {response.reason_phrase}".rstrip()
    problems = _validation_problems(detail)
    return f"{reason} ({problems})" if problems else reason


class SessionSupervisorClient:
    """Use the sandbox manager credential without ever exposing it to a chat container."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 60) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Agent-Workspace-Token": token}
        self._client = httpx.Client(timeout=timeout, headers=self._headers)

    def close(self) -> None:
        """Close the reusable private HTTP connection pool."""
        self._client.close()

    def _shared_request(
        self,
        method: str,
        thread_id: str,
        suffix: str = "",
        *,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        path = f"/shared-sandboxes/{quote(thread_id, safe='')}"
        if suffix:
            path += f"/{suffix}"
        try:
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                json=payload,
                timeout=self._client.timeout if timeout is None else timeout,
            )
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "shared Session Supervisor is unavailable") from exc
        if response.is_error:
            operation = suffix or "status"
            raise SessionSupervisorError(
                response.status_code,
                _error_detail(
                    response,
                    f"shared Session Supervisor rejected the lifecycle transition ({operation})",
                ),
            )
        body = response.json()
        if not isinstance(body, dict):
            raise SessionSupervisorError(502, "shared Session Supervisor returned invalid data")
        return body

    def apply_profiling_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        """Hand the sandbox manager one profiling policy and read back its confirmation.

        The sandbox manager is a separate container, so it cannot inherit the runner's
        policy the way a child process inherits one. It is told instead, over
        the same authenticated channel every other lifecycle message uses --
        the sandbox manager holds no host credential and never will, and the design
        forbids widening that.

        Returns what the sandbox manager says it applied, which is *not* the same as what
        was sent: the control plane records the confirmation, so a sandbox manager that
        rejected a newer revision must be visible rather than assumed.
        """
        try:
            response = self._client.post(f"{self._base_url}/profiling-policy", json=policy)
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "sandbox manager is unavailable") from exc
        if response.is_error:
            raise SessionSupervisorError(
                response.status_code,
                _error_detail(response, "sandbox manager rejected the profiling policy"),
            )
        body = response.json()
        if not isinstance(body, dict):
            raise SessionSupervisorError(502, "sandbox manager returned invalid data")
        return body

    def apply_host_settings(
        self, settings: dict[str, int], *, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Hand the sandbox manager the runtime settings an admin set and read its answer.

        ``settings`` is the sandbox manager's share of what the last heartbeat carried,
        the whole set: a setting absent from it returns the sandbox manager to its own
        value. The answer says what the sandbox manager now applies, which is what this
        host reports — not what it sent. Bounded well under the heartbeat
        interval, because it runs on the heartbeat thread.
        """
        try:
            response = self._client.post(
                f"{self._base_url}/host-settings", json={"settings": settings}, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "sandbox manager is unavailable") from exc
        if response.is_error:
            raise SessionSupervisorError(
                response.status_code, "sandbox manager rejected the runtime settings"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise SessionSupervisorError(502, "sandbox manager returned invalid data")
        return body

    def apply_sandbox_archive_settings(
        self, settings: dict[str, Any], *, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Relay one complete DB archive policy and read its applied revision.

        AWS credentials are intentionally absent: only the non-secret storage
        policy crosses this channel, and the sandbox manager uses its own workload
        identity. Re-sending an equal revision is idempotent and repairs a
        sandbox manager that restarted since the previous heartbeat.
        """
        try:
            response = self._client.post(
                f"{self._base_url}/sandbox-archive-settings",
                json={"settings": settings},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "sandbox manager is unavailable") from exc
        if response.is_error:
            raise SessionSupervisorError(
                response.status_code,
                _error_detail(response, "sandbox manager rejected sandbox archive settings"),
            )
        body = response.json()
        if not isinstance(body, dict):
            raise SessionSupervisorError(502, "sandbox manager returned invalid data")
        return body

    def list(self) -> builtins.list[dict[str, Any]]:
        """Observe every retained container owned by this host's sandbox manager."""
        try:
            response = self._client.get(f"{self._base_url}/sandboxes")
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "chat Session Supervisor is unavailable") from exc
        if response.is_error:
            raise SessionSupervisorError(
                response.status_code,
                _error_detail(
                    response, "chat Session Supervisor rejected the lifecycle observation"
                ),
            )
        body = response.json()
        rows = body.get("sandboxes") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SessionSupervisorError(502, "chat Session Supervisor returned invalid data")
        return [dict(row) for row in rows]

    def acquire_shared(
        self,
        spec: ChatSandboxSpec,
        *,
        thread_id: str,
        activation_generation: int,
        owner: str,
        has_settled_context: bool,
        members: builtins.list[dict[str, Any]],
        expected_container_id: str | None = None,
        profiling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Lease one member without excluding turns on sibling members."""
        return self._shared_request(
            "POST",
            thread_id,
            "acquire",
            payload={
                "sandbox_id": spec.sandbox_id,
                "generation": spec.generation,
                "origin_thread_id": spec.thread_id,
                "runtime_id": spec.runtime_id,
                "runtime_version": spec.runtime_version,
                "sandbox_image_digest": spec.sandbox_image_digest,
                "expected_container_id": expected_container_id,
                "resident_slot_capacity": spec.resident_slot_capacity,
                "memory_limit": spec.memory_limit,
                "cpu_limit": spec.cpu_limit,
                "disk_limit_mib": spec.disk_limit_mib,
                "stop_timeout_s": spec.stop_timeout_s,
                "members": members,
                "owner": owner,
                "activation_generation": activation_generation,
                "has_settled_context": has_settled_context,
                "profiling": profiling,
            },
        )

    def ensure_shared_resident(
        self,
        thread_id: str,
        *,
        token: str,
        launch: ResidentLaunch,
        profiling: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open or reuse one member's supervisor-owned live process."""
        return self._shared_request(
            "POST",
            thread_id,
            "resident",
            payload={"token": token, "launch": asdict(launch), "profiling": profiling},
            # Native package installation runs before the resident is ready.
            timeout=960.0 if launch.plugins else None,
        )

    def capture_initial_archive(
        self,
        spec: ChatSandboxSpec,
        *,
        thread_id: str,
        token: str,
        members: builtins.list[dict[str, Any]],
        expected_container_id: str,
        preparation_id: str,
        base_sha: str,
    ) -> dict[str, Any]:
        """Persist the prepared pre-prompt workspace exactly once."""
        return self._shared_request(
            "POST",
            thread_id,
            "archive-initial",
            timeout=900.0,
            payload={
                "sandbox_id": spec.sandbox_id,
                "generation": spec.generation,
                "origin_thread_id": spec.thread_id,
                "runtime_id": spec.runtime_id,
                "runtime_version": spec.runtime_version,
                "sandbox_image_digest": spec.sandbox_image_digest,
                "expected_container_id": expected_container_id,
                "resident_slot_capacity": spec.resident_slot_capacity,
                "memory_limit": spec.memory_limit,
                "cpu_limit": spec.cpu_limit,
                "disk_limit_mib": spec.disk_limit_mib,
                "stop_timeout_s": spec.stop_timeout_s,
                "members": members,
                "token": token,
                "preparation_id": preparation_id,
                "base_sha": base_sha,
            },
        )

    def initial_archive_status(
        self,
        spec: ChatSandboxSpec,
        *,
        thread_id: str,
        token: str,
        members: builtins.list[dict[str, Any]],
        expected_container_id: str,
        preparation_id: str,
    ) -> dict[str, Any]:
        """Resolve a crash-committed baseline before any workspace reset."""
        return self._shared_request(
            "POST",
            thread_id,
            "archive-initial/status",
            timeout=900.0,
            payload={
                "sandbox_id": spec.sandbox_id,
                "generation": spec.generation,
                "origin_thread_id": spec.thread_id,
                "runtime_id": spec.runtime_id,
                "runtime_version": spec.runtime_version,
                "sandbox_image_digest": spec.sandbox_image_digest,
                "expected_container_id": expected_container_id,
                "resident_slot_capacity": spec.resident_slot_capacity,
                "memory_limit": spec.memory_limit,
                "cpu_limit": spec.cpu_limit,
                "disk_limit_mib": spec.disk_limit_mib,
                "stop_timeout_s": spec.stop_timeout_s,
                "members": members,
                "token": token,
                "preparation_id": preparation_id,
            },
        )

    def start_shared_turn(
        self,
        thread_id: str,
        *,
        token: str,
        prompt: str,
        model: str,
        reasoning_effort: str | None,
        read_timeout_s: float,
    ) -> SharedTurnStream:
        """Start one resident turn and return its framed response stream."""
        path = f"/shared-sandboxes/{quote(thread_id, safe='')}/turn"
        # The sandbox manager emits CLI events, which may be silent throughout model
        # thinking or a tool call. Let the runner's execution/interrupt budget
        # bound that wait without extending ordinary lifecycle requests.
        stream_timeout = httpx.Timeout(self._client.timeout)
        stream_timeout.read = read_timeout_s
        try:
            request = self._client.build_request(
                "POST",
                f"{self._base_url}{path}",
                json={
                    "token": token,
                    "prompt": prompt,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
                timeout=stream_timeout,
            )
            response = self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "shared Session Supervisor is unavailable") from exc
        if response.is_error:
            response.read()
            detail = _error_detail(response, "shared Session Supervisor rejected the turn")
            status_code = response.status_code
            response.close()
            raise SessionSupervisorError(status_code, detail)
        return SharedTurnStream(response)

    def interrupt_shared(self, thread_id: str, *, token: str) -> None:
        """Interrupt one exact member turn."""
        self._shared_request("POST", thread_id, "interrupt", payload={"token": token})

    def shared_residency_lost(self, thread_id: str, *, generation: int) -> dict[str, Any]:
        """Close the exact member process whose renewable grant was rejected."""
        return self._shared_request(
            "POST",
            thread_id,
            "residency-lost",
            payload={"generation": generation},
        )

    def settle_shared(
        self,
        thread_id: str,
        *,
        token: str,
        job_id: str,
        context_status: str,
    ) -> dict[str, Any]:
        """Release a successful member activation while keeping its process."""
        return self._shared_request(
            "POST",
            thread_id,
            "settle",
            timeout=10.0,
            payload={
                "token": token,
                "job_id": job_id,
                "context_status": context_status,
            },
        )

    def abandon_shared(self, thread_id: str, *, token: str, lost: bool) -> dict[str, Any]:
        """Release one failed member without stopping healthy siblings."""
        return self._shared_request(
            "POST",
            thread_id,
            "abandon",
            payload={"token": token, "lost": lost},
        )

    def shared_status(self, thread_id: str) -> dict[str, Any]:
        """Read one member plus its shared Docker group state."""
        return self._shared_request("GET", thread_id)

    def stop_shared_idle(
        self,
        thread_id: str,
        *,
        generation: int,
        activation_generation: int,
        reason: str = "idle",
        member_activation_generations: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Apply a group- and activation-fenced shared idle stop."""
        return self._shared_request(
            "POST",
            thread_id,
            "stop-idle",
            # A configured archive makes the clean stop synchronous: Docker
            # exports bounded final roots and completes bounded S3 retries
            # before this call can report success.
            timeout=1800.0,
            payload={
                "generation": generation,
                "activation_generation": activation_generation,
                "reason": reason,
                **(
                    {}
                    if member_activation_generations is None
                    else {
                        "member_activation_generations": member_activation_generations,
                    }
                ),
            },
        )


__all__ = ["SessionSupervisorClient", "SessionSupervisorError", "SharedTurnStream"]
