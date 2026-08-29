"""Gateway-side client for the private agent workspace broker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class WorkspaceBrokerError(RuntimeError):
    """A bounded, owner-safe failure returned by the internal broker."""

    status_code: int
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class WorkspaceBrokerStream:
    """An already-authorized broker response that the gateway must close."""

    _client: httpx.AsyncClient
    _response: httpx.Response

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._response.aiter_raw()

    async def aclose(self) -> None:
        """Release both the streaming response and its dedicated client."""
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


class WorkspaceBrokerClient:
    """Talk to the broker without exposing its credential to the browser."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Agent-Workspace-Token": token}

    @staticmethod
    def _response_error(response: httpx.Response) -> WorkspaceBrokerError:
        """Read a bounded, owner-safe error from a completed broker response."""
        message = "The live workspace request failed."
        try:
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, str) and detail:
                message = detail
        except ValueError:
            pass
        return WorkspaceBrokerError(response.status_code, message)

    async def _request(
        self,
        method: str,
        workspace_id: str,
        suffix: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}/workspaces/{workspace_id}/{suffix}",
                    headers=self._headers,
                    params=params,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(
                503, "The live workspace is temporarily unavailable."
            ) from exc
        if response.is_error:
            raise self._response_error(response)
        body = response.json()
        if not isinstance(body, dict):
            raise WorkspaceBrokerError(502, "The live workspace returned an invalid response.")
        return body

    async def _sandbox_request(
        self,
        method: str,
        thread_id: str,
        suffix: str = "",
        *,
        json: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> dict[str, Any]:
        """Call the private retained-sandbox supervisor for one thread."""
        encoded = quote(thread_id, safe="")
        path = f"/sandboxes/{encoded}"
        if suffix:
            path += f"/{suffix}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    json=json,
                )
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(
                503, "The chat sandbox supervisor is temporarily unavailable."
            ) from exc
        if response.is_error:
            raise self._response_error(response)
        body = response.json()
        if not isinstance(body, dict):
            raise WorkspaceBrokerError(502, "The chat sandbox supervisor returned invalid data.")
        return body

    async def health(self, *, timeout: float = 3) -> None:
        """Reach the broker and be recognised by it, or raise.

        Not part of a job's path — this is what lets the control plane find out
        at enrolment whether the address a host reported is one it can actually
        use, rather than at the first terminal or file read of the first job.

        `/health` is authenticated, which is why it is the right probe: it
        answers reachability and credential in one call, and those were the two
        ways this was misconfigured on the first remote host. An unauthenticated
        endpoint would have said everything was fine while the token was wrong.

        Deliberately short: a hanging broker must not hold up a heartbeat.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self._base_url}/health", headers=self._headers)
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(503, f"unreachable: {exc}") from exc
        if response.is_error:
            raise self._response_error(response)

    async def files(self, workspace_id: str, path: str) -> dict[str, Any]:
        """Read one file or directory from a live workspace."""
        return await self._request("GET", workspace_id, "files", params={"path": path})

    async def write_file(self, workspace_id: str, path: str, content: str) -> dict[str, Any]:
        """Replace one UTF-8 file in a live workspace."""
        return await self._request(
            "PUT", workspace_id, "files", params={"path": path}, json={"content": content}
        )

    async def terminal(
        self, workspace_id: str, *, command: str, cwd: str, timeout_seconds: float
    ) -> dict[str, Any]:
        """Execute one bounded command in an isolated workspace sandbox."""
        return await self._request(
            "POST",
            workspace_id,
            "terminal",
            json={"command": command, "cwd": cwd, "timeout_seconds": timeout_seconds},
            timeout=timeout_seconds + 10,
        )

    async def create_terminal(self, workspace_id: str, *, rows: int, cols: int) -> dict[str, Any]:
        """Open one interactive terminal in a durable workspace."""
        return await self._request(
            "POST", workspace_id, "terminals", json={"rows": rows, "cols": cols}
        )

    async def list_terminals(self, workspace_id: str) -> dict[str, Any]:
        """List the interactive terminals retained by a workspace."""
        return await self._request("GET", workspace_id, "terminals")

    async def suspend_terminals(
        self, workspace_id: str, *, lease_generation: int
    ) -> dict[str, Any]:
        """Freeze every interactive terminal process tree in a workspace."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/suspend",
            json={"lease_generation": lease_generation},
            timeout=60,
        )

    async def resume_terminals(self, workspace_id: str, *, lease_generation: int) -> dict[str, Any]:
        """Resume retained interactive terminals after a protected phase."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/resume",
            json={"lease_generation": lease_generation},
            timeout=60,
        )

    async def resume_settled_terminals(self, workspace_id: str) -> dict[str, Any]:
        """Authoritatively resume terminals after the job can no longer retry."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/resume-settled",
            timeout=60,
        )

    async def stream_terminal(
        self, workspace_id: str, terminal_id: str, *, after: int
    ) -> WorkspaceBrokerStream:
        """Open and validate a terminal SSE stream before public headers are sent."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        )
        suffix = f"terminals/{quote(terminal_id, safe='')}/stream"
        response: httpx.Response | None = None
        ownership_transferred = False
        try:
            request = client.build_request(
                "GET",
                f"{self._base_url}/workspaces/{workspace_id}/{suffix}",
                headers=self._headers,
                params={"after": str(after)},
            )
            response = await client.send(request, stream=True)
            if response.is_error:
                await response.aread()
                raise self._response_error(response)
            ownership_transferred = True
            return WorkspaceBrokerStream(client, response)
        except WorkspaceBrokerError:
            raise
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(
                503, "The live workspace is temporarily unavailable."
            ) from exc
        finally:
            # ``asyncio.CancelledError`` is a BaseException, so exception-only
            # cleanup leaks this dedicated client while a cancelled public SSE
            # request is still connecting. Ownership moves to the returned
            # stream only after a successful, validated response.
            if not ownership_transferred:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    await client.aclose()

    async def terminal_input(
        self, workspace_id: str, terminal_id: str, *, data: str
    ) -> dict[str, Any]:
        """Write base64-encoded bytes to an interactive terminal."""
        suffix = f"terminals/{quote(terminal_id, safe='')}/input"
        return await self._request("POST", workspace_id, suffix, json={"data": data})

    async def resize_terminal(
        self, workspace_id: str, terminal_id: str, *, rows: int, cols: int
    ) -> dict[str, Any]:
        """Resize an interactive terminal."""
        suffix = f"terminals/{quote(terminal_id, safe='')}/resize"
        return await self._request("POST", workspace_id, suffix, json={"rows": rows, "cols": cols})

    async def delete_terminal(self, workspace_id: str, terminal_id: str) -> dict[str, Any]:
        """Kill an interactive terminal; the private operation is idempotent."""
        suffix = f"terminals/{quote(terminal_id, safe='')}"
        return await self._request("DELETE", workspace_id, suffix)

    async def git(self, workspace_id: str, base_sha: str | None = None) -> dict[str, Any]:
        """Read live status, base-relative diff, and recent commits."""
        params = {"base_sha": base_sha} if base_sha else None
        return await self._request("GET", workspace_id, "git", params=params, timeout=70)

    async def acquire_chat_sandbox(
        self,
        thread_id: str,
        *,
        sandbox_id: str,
        generation: int,
        activation_generation: int,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None = None,
        owner: str,
        has_settled_context: bool,
        live_process_proven: bool = False,
        memory_limit: str = "4g",
        cpu_limit: str = "2",
        stop_timeout_s: int = 10,
    ) -> dict[str, Any]:
        """Create/start and exclusively reserve a thread's retained container."""
        return await self._sandbox_request(
            "POST",
            thread_id,
            "acquire",
            json={
                "sandbox_id": sandbox_id,
                "generation": generation,
                "activation_generation": activation_generation,
                "runtime_id": runtime_id,
                "runtime_version": runtime_version,
                "sandbox_image_digest": sandbox_image_digest,
                "owner": owner,
                "has_settled_context": has_settled_context,
                "live_process_proven": live_process_proven,
                "memory_limit": memory_limit,
                "cpu_limit": cpu_limit,
                "stop_timeout_s": stop_timeout_s,
            },
            timeout=150,
        )

    async def note_chat_process_started(self, thread_id: str, *, token: str) -> dict[str, Any]:
        """Advance the CLI process generation under an activation lease."""
        return await self._sandbox_request(
            "POST", thread_id, "process-started", json={"token": token}
        )

    async def settle_chat_sandbox(
        self,
        thread_id: str,
        *,
        token: str,
        job_id: str,
        context_status: str,
    ) -> dict[str, Any]:
        """Release a successful turn and begin the chat idle timeout."""
        return await self._sandbox_request(
            "POST",
            thread_id,
            "settle",
            json={
                "token": token,
                "job_id": job_id,
                "context_status": context_status,
            },
        )

    async def abandon_chat_sandbox(
        self, thread_id: str, *, token: str, lost: bool
    ) -> dict[str, Any]:
        """Release a failed turn without claiming a settled context."""
        return await self._sandbox_request(
            "POST",
            thread_id,
            "abandon",
            json={"token": token, "lost": lost},
        )

    async def fork_chat_sandbox(
        self,
        source_thread_id: str,
        *,
        operation_id: str,
        source_sandbox_id: str,
        source_generation: int,
        source_activation_generation: int,
        source_container_id: str,
        source_anchor_job_id: str,
        source_native_session_id: str,
        target_thread_id: str,
        target_sandbox_id: str,
        target_generation: int = 1,
    ) -> dict[str, Any]:
        """Commit and clone one latest-settled retained chat idempotently."""
        return await self._sandbox_request(
            "POST",
            source_thread_id,
            "fork",
            json={
                "operation_id": operation_id,
                "source_sandbox_id": source_sandbox_id,
                "source_generation": source_generation,
                "source_activation_generation": source_activation_generation,
                "source_container_id": source_container_id,
                "source_anchor_job_id": source_anchor_job_id,
                "source_native_session_id": source_native_session_id,
                "target_thread_id": target_thread_id,
                "target_sandbox_id": target_sandbox_id,
                "target_generation": target_generation,
            },
            timeout=420,
        )

    async def rollback_chat_sandbox_fork(
        self,
        source_thread_id: str,
        *,
        operation_id: str,
        source_sandbox_id: str,
        target_thread_id: str,
        target_sandbox_id: str,
        target_generation: int = 1,
        expected_container_id: str | None = None,
    ) -> dict[str, Any]:
        """Compensate only the exact incomplete clone owned by a fork saga."""
        return await self._sandbox_request(
            "POST",
            source_thread_id,
            "fork-rollback",
            json={
                "operation_id": operation_id,
                "source_sandbox_id": source_sandbox_id,
                "target_thread_id": target_thread_id,
                "target_sandbox_id": target_sandbox_id,
                "target_generation": target_generation,
                "expected_container_id": expected_container_id,
            },
            timeout=120,
        )

    async def chat_sandbox_status(self, thread_id: str) -> dict[str, Any]:
        """Inspect the exact retained container recorded for a thread."""
        return await self._sandbox_request("GET", thread_id)


def workspace_broker_from_env() -> WorkspaceBrokerClient | None:
    """Return the configured broker, or ``None`` for legacy deployments."""
    url = (os.environ.get("AGENT_WORKSPACE_BROKER_URL") or "").strip()
    token = (os.environ.get("AGENT_WORKSPACE_BROKER_TOKEN") or "").strip()
    if not url or not token:
        return None
    return WorkspaceBrokerClient(url, token)


__all__ = [
    "WorkspaceBrokerClient",
    "WorkspaceBrokerError",
    "WorkspaceBrokerStream",
    "workspace_broker_from_env",
]
