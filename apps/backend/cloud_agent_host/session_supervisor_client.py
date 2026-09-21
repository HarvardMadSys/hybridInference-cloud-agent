"""Synchronous runner client for the host's private Session Supervisor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterator

    from cloud_agent_host.chat_sandbox import ChatSandboxSpec
    from cloud_agent_host.resident_transport import ResidentLaunch


@dataclass
class SharedTurnStream:
    """One broker-owned resident turn streamed as bounded NDJSON frames."""

    _response: httpx.Response

    def frames(self) -> Iterator[dict[str, Any]]:
        """Yield validated object frames until result/error."""
        for line in self._response.iter_lines():
            if not line:
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


class SessionSupervisorClient:
    """Use the broker credential without ever exposing it to a chat container."""

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
            detail = "shared Session Supervisor rejected the lifecycle transition"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"]
            except ValueError:
                pass
            raise SessionSupervisorError(response.status_code, detail)
        body = response.json()
        if not isinstance(body, dict):
            raise SessionSupervisorError(502, "shared Session Supervisor returned invalid data")
        return body

    def apply_profiling_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        """Hand the broker one profiling policy and read back its confirmation.

        The broker is a separate container, so it cannot inherit the runner's
        policy the way a child process inherits one. It is told instead, over
        the same authenticated channel every other lifecycle message uses --
        the broker holds no host credential and never will, and the design
        forbids widening that.

        Returns what the broker says it applied, which is *not* the same as what
        was sent: the control plane records the confirmation, so a broker that
        rejected a newer revision must be visible rather than assumed.
        """
        try:
            response = self._client.post(f"{self._base_url}/profiling-policy", json=policy)
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "workspace broker is unavailable") from exc
        if response.is_error:
            detail = "workspace broker rejected the profiling policy"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"]
            except ValueError:
                pass
            raise SessionSupervisorError(response.status_code, detail)
        body = response.json()
        if not isinstance(body, dict):
            raise SessionSupervisorError(502, "workspace broker returned invalid data")
        return body

    def list(self) -> builtins.list[dict[str, Any]]:
        """Observe every retained container owned by this host's broker."""
        try:
            response = self._client.get(f"{self._base_url}/sandboxes")
        except httpx.HTTPError as exc:
            raise SessionSupervisorError(503, "chat Session Supervisor is unavailable") from exc
        if response.is_error:
            detail = "chat Session Supervisor rejected the lifecycle observation"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"]
            except ValueError:
                pass
            raise SessionSupervisorError(response.status_code, detail)
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
        # The broker emits CLI events, which may be silent throughout model
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
            detail = "shared Session Supervisor rejected the turn"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"]
            except ValueError:
                pass
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
        reason: str = "idle",
    ) -> dict[str, Any]:
        """Apply a group-fenced shared idle stop."""
        return self._shared_request(
            "POST",
            thread_id,
            "stop-idle",
            payload={
                "generation": generation,
                "activation_generation": 0,
                "reason": reason,
            },
        )


__all__ = ["SessionSupervisorClient", "SessionSupervisorError", "SharedTurnStream"]
