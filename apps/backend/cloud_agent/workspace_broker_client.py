"""Gateway-side client for the private agent workspace broker."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from cloud_agent_shared.operation_profiling import (
    MAX_CONTEXT_CHARS,
    PROFILING_CONTEXT_HEADER,
    answer_from_headers,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping


SHARED_SESSION_FORK_TIMEOUT_S = 180.0
# A new member can install its selected plugins before opening the native CLI.
SHARED_PLUGIN_SESSION_FORK_TIMEOUT_S = 960.0


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
    #: The broker's measurement answer for a streaming route, read from the
    #: response headers because the body is the terminal's own bytes. ``None``
    #: when the broker was not collecting, which leaves the caller's fragment
    #: unanswered rather than inventing an answer it never received.
    profiling_answer: dict[str, Any] | None = None

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Stream the broker's raw body."""
        return self._response.aiter_raw()

    async def aclose(self) -> None:
        """Release both the streaming response and its dedicated client."""
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


class WorkspaceBrokerClient:
    """Talk to the broker without exposing its credential to the browser."""

    def __init__(
        self, base_url: str, token: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        """Address one broker.

        Args:
            base_url: The broker's origin, as its host reported it.
            token: The pool's shared broker credential.
            client: A client to borrow for request-shaped calls. These objects
                are built per request — one per file read, per terminal
                keystroke — so the pool cannot live on the instance; the
                caller keeps it, keyed by ``base_url``, and closes it. Without
                one, each call opens and closes its own connection as before.
        """
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Agent-Workspace-Token": token}
        self._client = client

    @asynccontextmanager
    async def _http(self, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        """Yield a client for one request-shaped call.

        A borrowed client is left open — it outlives this object by design.
        The timeout travels with the request rather than the client for the
        same reason: the deadlines here differ per call (3s for a health
        probe, 60s for the shared supervisor) and a shared client cannot carry
        all of them.

        The terminal *stream* deliberately does not come through here. It
        needs ``read=None`` and it hands its client to the caller to close,
        neither of which a shared pool can offer.
        """
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=timeout) as client:
            yield client

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
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with self._http(timeout) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}/workspaces/{workspace_id}/{suffix}",
                    headers=headers or self._headers,
                    params=params,
                    json=json,
                    timeout=timeout,
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

    async def _shared_sandbox_request(
        self,
        method: str,
        thread_id: str,
        suffix: str = "",
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> dict[str, Any]:
        """Call the private shared-live supervisor for one membership."""
        encoded = quote(thread_id, safe="")
        path = f"/shared-sandboxes/{encoded}"
        if suffix:
            path += f"/{suffix}"
        try:
            async with self._http(timeout) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    params=params,
                    json=json,
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(
                503, "The shared chat supervisor is temporarily unavailable."
            ) from exc
        if response.is_error:
            raise self._response_error(response)
        body = response.json()
        if not isinstance(body, dict):
            raise WorkspaceBrokerError(502, "The shared chat supervisor returned invalid data.")
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
            async with self._http(timeout) as client:
                response = await client.get(
                    f"{self._base_url}/health", headers=self._headers, timeout=timeout
                )
        except httpx.HTTPError as exc:
            raise WorkspaceBrokerError(503, f"unreachable: {exc}") from exc
        if response.is_error:
            raise self._response_error(response)

    async def list_sandboxes(
        self, *, timeout: float = 10, thread_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Read retained-container inventory, optionally scoped to one shared group."""
        selection: dict[str, Any] = (
            {"params": {"thread_id": thread_id}} if thread_id is not None else {}
        )
        try:
            async with self._http(timeout) as client:
                response = await client.get(
                    f"{self._base_url}/sandboxes",
                    headers=self._headers,
                    timeout=timeout,
                    **selection,
                )
        except httpx.HTTPError:
            raise WorkspaceBrokerError(
                503, "The workspace broker inventory is unavailable."
            ) from None
        if response.is_error:
            raise WorkspaceBrokerError(503, "The workspace broker refused the inventory request.")
        try:
            body = response.json()
        except ValueError:
            raise WorkspaceBrokerError(502, "The workspace broker inventory is invalid.") from None
        if not isinstance(body, dict) or not isinstance(body.get("sandboxes"), list):
            raise WorkspaceBrokerError(502, "The workspace broker inventory is incomplete.")
        rows = body["sandboxes"]
        if not all(isinstance(row, dict) and row.get("sandbox_id") for row in rows):
            raise WorkspaceBrokerError(502, "The workspace broker inventory is invalid.")
        return rows

    def _profiling_headers(self, context: Mapping[str, Any] | None) -> dict[str, str]:
        """Return this request's headers, carrying the inherited selection.

        The selection is measurement metadata, never an authorization token: the
        broker validates it against its own switch, run and deadline and treats
        it as absent when it cannot. It travels as one header rather than as a
        query parameter so it cannot end up in an access log or a cache key.
        """
        if not context:
            return self._headers
        try:
            encoded = json.dumps(dict(context), separators=(",", ":"), default=str)
        except (TypeError, ValueError):  # pragma: no cover - context is plain data
            return self._headers
        if len(encoded) > MAX_CONTEXT_CHARS:
            return self._headers
        return {**self._headers, PROFILING_CONTEXT_HEADER: encoded}

    async def files(
        self, workspace_id: str, path: str, *, profiling: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read one file or directory from a live workspace."""
        return await self._request(
            "GET",
            workspace_id,
            "files",
            params={"path": path},
            headers=self._profiling_headers(profiling),
        )

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

    async def create_terminal(
        self, workspace_id: str, *, rows: int, cols: int, profiling: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Open one interactive terminal in a durable workspace."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals",
            json={"rows": rows, "cols": cols},
            headers=self._profiling_headers(profiling),
        )

    async def list_terminals(
        self, workspace_id: str, *, profiling: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """List the interactive terminals retained by a workspace."""
        return await self._request(
            "GET", workspace_id, "terminals", headers=self._profiling_headers(profiling)
        )

    async def suspend_terminals(
        self, workspace_id: str, *, lease_generation: int, job_id: str | None = None
    ) -> dict[str, Any]:
        """Freeze every interactive terminal process tree in a workspace."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/suspend",
            json={"lease_generation": lease_generation, "job_id": job_id},
            timeout=60,
        )

    async def resume_terminals(
        self, workspace_id: str, *, lease_generation: int, job_id: str | None = None
    ) -> dict[str, Any]:
        """Resume retained interactive terminals after a protected phase."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/resume",
            json={"lease_generation": lease_generation, "job_id": job_id},
            timeout=60,
        )

    async def resume_settled_terminals(
        self, workspace_id: str, *, job_id: str | None = None
    ) -> dict[str, Any]:
        """Authoritatively resume terminals after the job can no longer retry."""
        return await self._request(
            "POST",
            workspace_id,
            "terminals/resume-settled",
            json={"job_id": job_id} if job_id is not None else None,
            timeout=60,
        )

    async def stream_terminal(
        self,
        workspace_id: str,
        terminal_id: str,
        *,
        after: int,
        profiling: Mapping[str, Any] | None = None,
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
                headers=self._profiling_headers(profiling),
                params={"after": str(after)},
            )
            response = await client.send(request, stream=True)
            if response.is_error:
                await response.aread()
                raise self._response_error(response)
            ownership_transferred = True
            return WorkspaceBrokerStream(
                client, response, profiling_answer=answer_from_headers(response.headers)
            )
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

    async def shared_chat_sandbox_status(
        self, thread_id: str, *, timeout: float = 60
    ) -> dict[str, Any]:
        """Inspect one membership in its shared retained container."""
        return await self._shared_sandbox_request("GET", thread_id, timeout=timeout)

    async def prepare_sandbox_deletion(
        self, thread_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Export the exact stopped container behind a durable host barrier."""
        return await self._shared_sandbox_request(
            "POST", thread_id, "admin-delete/prepare", json=payload, timeout=1800
        )

    async def upload_sandbox_deletion_archive(
        self, thread_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a prepared archive using only a checksum-bound S3 upload URL."""
        return await self._shared_sandbox_request(
            "POST", thread_id, "admin-delete/upload", json=payload, timeout=1800
        )

    async def commit_sandbox_deletion(
        self, thread_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove a stopped container after its verified receipt is durable."""
        return await self._shared_sandbox_request(
            "POST", thread_id, "admin-delete/commit", json=payload, timeout=120
        )

    async def abort_sandbox_deletion(
        self, thread_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Release an uncommitted host archive barrier after a failed upload."""
        return await self._shared_sandbox_request(
            "POST", thread_id, "admin-delete/abort", json=payload, timeout=120
        )

    async def discard_sandbox_deletion(
        self, thread_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove a stopped container without exporting or uploading an archive."""
        return await self._shared_sandbox_request(
            "POST", thread_id, "admin-delete/discard", json=payload, timeout=120
        )

    async def register_shared_sandbox(self, sandbox: dict[str, Any]) -> dict[str, Any]:
        """Rehydrate durable member aliases after a broker process restart.

        Docker labels identify the group and its origin chat, but forked chat
        aliases live in the control-plane ledger.  Workspace operations can
        arrive before another Agent turn, so they carry that bounded manifest
        back to the owning supervisor without starting or cloning a container.
        """
        origin_thread_id = str(sandbox.get("origin_thread_id") or "")
        members = []
        allowed_member_fields = {
            "thread_id",
            "native_session_id",
            "context_status",
            "activation_generation",
            "process_generation",
            "residency_generation",
            "resident_state",
            "resident_slot_id",
            "last_settled_job_id",
        }
        for raw in sandbox.get("group_members") or []:
            if isinstance(raw, dict):
                members.append(
                    {key: value for key, value in raw.items() if key in allowed_member_fields}
                )
        if not origin_thread_id or not members:
            raise WorkspaceBrokerError(502, "The shared sandbox manifest is incomplete.")
        return await self._shared_sandbox_request(
            "POST",
            origin_thread_id,
            "register",
            json={
                "sandbox_id": sandbox["sandbox_id"],
                "generation": sandbox["generation"],
                "origin_thread_id": origin_thread_id,
                "runtime_id": sandbox["runtime_id"],
                "runtime_version": sandbox.get("runtime_version"),
                "sandbox_image_digest": sandbox.get("sandbox_image_digest"),
                "expected_container_id": sandbox.get("container_id"),
                "resident_slot_capacity": sandbox["resident_slot_capacity"],
                "memory_limit": sandbox["memory_limit"],
                "cpu_limit": sandbox["cpu_limit"],
                "disk_limit_mib": sandbox.get("disk_limit_mib", 0),
                "stop_timeout_s": sandbox["stop_timeout_seconds"],
                "members": members,
            },
        )

    async def inject_shared_input(
        self, thread_id: str, *, owner: str, message_id: str, text: str
    ) -> dict[str, Any]:
        """Give one member's turn the owner's message for the model's next step.

        Answers ``{"accepted": bool, "reason": str | None}``: a turn that cannot
        have it is an answer, not an error. Accepted, the turn's own stream
        reports where the model got it, naming ``message_id``. ``owner`` names
        the activation the caller saw, ``attempt:<id>``, so a later one never
        receives it; one that has not started on the host yet gets it when it
        does.
        """
        return await self._shared_sandbox_request(
            "POST",
            thread_id,
            "input",
            json={"owner": owner, "message_id": message_id, "text": text},
            timeout=30,
        )

    async def pending_shared_questions(self, thread_id: str, *, owner: str) -> dict[str, Any]:
        """Read native requests waiting on one exact activation owner."""
        return await self._shared_sandbox_request(
            "GET", thread_id, "questions", params={"owner": owner}, timeout=30
        )

    async def answer_shared_question(
        self,
        thread_id: str,
        *,
        owner: str,
        request_id: str,
        answers: dict[str, list[str]],
        reject: bool = False,
    ) -> dict[str, Any]:
        """Answer a native request without retaining a reply for later turns."""
        return await self._shared_sandbox_request(
            "POST",
            thread_id,
            "questions",
            json={
                "owner": owner,
                "request_id": request_id,
                "answers": answers,
                "reject": reject,
            },
            timeout=30,
        )

    async def shared_residency_lost(self, thread_id: str, *, generation: int) -> dict[str, Any]:
        """Close one exact shared member process generation."""
        return await self._shared_sandbox_request(
            "POST",
            thread_id,
            "residency-lost",
            json={"generation": generation},
        )

    async def fork_shared_session(
        self,
        source_thread_id: str,
        *,
        sandbox_id: str,
        generation: int,
        origin_thread_id: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        expected_container_id: str,
        resident_slot_capacity: int,
        memory_limit: str,
        cpu_limit: str,
        disk_limit_mib: int,
        stop_timeout_s: int,
        members: list[dict[str, Any]],
        operation_id: str,
        source_native_session_id: str,
        source_native_turn_no: int,
        target_thread_id: str,
        target_slot_id: int,
        source_launch: dict[str, Any],
        target_launch: dict[str, Any],
    ) -> dict[str, Any]:
        """Materialize a prompt-free native child in the same sandbox."""
        return await self._shared_sandbox_request(
            "POST",
            source_thread_id,
            "fork",
            json={
                "sandbox_id": sandbox_id,
                "generation": generation,
                "origin_thread_id": origin_thread_id,
                "runtime_id": runtime_id,
                "runtime_version": runtime_version,
                "sandbox_image_digest": sandbox_image_digest,
                "expected_container_id": expected_container_id,
                "resident_slot_capacity": resident_slot_capacity,
                "memory_limit": memory_limit,
                "cpu_limit": cpu_limit,
                "disk_limit_mib": disk_limit_mib,
                "stop_timeout_s": stop_timeout_s,
                "members": members,
                "operation_id": operation_id,
                "source_native_session_id": source_native_session_id,
                "source_native_turn_no": source_native_turn_no,
                "target_thread_id": target_thread_id,
                "target_slot_id": target_slot_id,
                "source_launch": source_launch,
                "target_launch": target_launch,
            },
            timeout=(
                SHARED_PLUGIN_SESSION_FORK_TIMEOUT_S
                if source_launch.get("plugins") or target_launch.get("plugins")
                else SHARED_SESSION_FORK_TIMEOUT_S
            ),
        )

    @staticmethod
    def _shared_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Copy only the reviewed immutable group/member manifest fields."""
        return {
            "sandbox_id": snapshot["sandbox_id"],
            "generation": snapshot["generation"],
            "origin_thread_id": snapshot["origin_thread_id"],
            "runtime_id": snapshot["runtime_id"],
            "runtime_version": snapshot.get("runtime_version"),
            "sandbox_image_digest": snapshot.get("sandbox_image_digest"),
            "expected_container_id": snapshot.get("container_id"),
            "resident_slot_capacity": snapshot["resident_slot_capacity"],
            "memory_limit": snapshot["memory_limit"],
            "cpu_limit": snapshot["cpu_limit"],
            "disk_limit_mib": snapshot.get("disk_limit_mib", 0),
            "stop_timeout_s": snapshot["stop_timeout_seconds"],
            "members": snapshot["members"],
        }

    async def capture_shared_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Ask the owning host for one bounded, generation-fenced Git diff."""
        payload = {
            **self._shared_identity(snapshot),
            "snapshot_id": snapshot["snapshot_id"],
            "workspace_epoch": snapshot["workspace_epoch"],
            "base_sha": snapshot["base_sha"],
        }
        return await self._shared_sandbox_request(
            "POST",
            str(snapshot["origin_thread_id"]),
            "snapshot",
            json=payload,
            timeout=120,
        )

    async def import_shared_base(
        self,
        snapshot: dict[str, Any],
        *,
        commit_sha: str,
        bundle: bytes,
    ) -> dict[str, Any]:
        """Import trusted commit objects without changing workspace state."""
        payload = {
            **self._shared_identity(snapshot),
            "snapshot_id": snapshot["snapshot_id"],
            "commit_sha": commit_sha,
            "bundle_b64": base64.b64encode(bundle).decode("ascii"),
        }
        return await self._shared_sandbox_request(
            "POST",
            str(snapshot["origin_thread_id"]),
            "import-base",
            json=payload,
            timeout=360,
        )


__all__ = [
    "WorkspaceBrokerClient",
    "WorkspaceBrokerError",
    "WorkspaceBrokerStream",
]
