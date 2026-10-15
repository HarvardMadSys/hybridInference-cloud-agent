"""Attempt-scoped HTTP client for worker control-plane endpoints.

The runner constructs this client at its orchestration boundary. This module
owns request payloads, fencing responses, event replay, and request deadlines;
lease renewal and turn scheduling remain in the runner.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from cloud_agent_host.claimed_job import LeaseLost
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import DELIVERY_TIMEOUT_S
from cloud_agent_host.runtimes import NormalizedEvent  # noqa: TC001 — Public method introspection.
from cloud_agent_shared.secret_redaction import SecretRedactor

# Heartbeats must answer before ordinary lifecycle requests consume the lease.
_HEARTBEAT_TIMEOUT_S = 10.0
# Attachments may need longer than the client's default deadline for transfer.
_ATTACHMENT_TIMEOUT_S = 120.0
#: How long one event upload keeps retrying an unreachable control plane, and
#: the backoff ceiling between tries. Half the default lease: long enough to
#: ride out a control-plane container being recreated, short enough that a
#: turn the platform cannot record is not left running unseen.
_EVENT_RETRY_BUDGET_S = 60.0
_EVENT_RETRY_MAX_DELAY_S = 8.0
_EVENT_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

logger = get_logger(__name__)


def _log_control_failure(operation: str, job_id: str, exc: Exception) -> None:
    """Keep a diagnosable failure without logging tokens or arbitrary response bodies.

    The same ``agent_control_failure`` record the runner writes for its own
    control calls, for the retries this client makes itself.
    """
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    logger.warning(
        "agent_control_failure operation=%s job_id=%s error_type=%s status_code=%s",
        operation,
        job_id,
        type(exc).__name__,
        status,
    )


class ControlPlane:
    """Thin client for the worker endpoints, using the capability token."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})
        # This client serves exactly one attempt (one claim, one token), so a
        # plain counter is the whole numbering scheme. It makes each upload
        # idempotent server-side, which is what licenses the retry below.
        self._event_seq = 0
        self._redactor = SecretRedactor({})
        self._preview_redactors: dict[str, SecretRedactor] = {}
        self._preview_contexts: dict[str, tuple[str, dict[str, Any]]] = {}
        self._secret_values: dict[str, str] = {}

    def close(self) -> None:
        """Release the HTTP connection pool."""
        self._client.close()

    def _post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        already_redacted: bool = False,
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self._base}{path}",
            json=(payload or {}) if already_redacted else self._redactor.payload(payload or {}),
            timeout=self._client.timeout if timeout is None else timeout,
        )
        if response.status_code == 409:
            raise LeaseLost(response.text[:200])
        response.raise_for_status()
        return response.json() if response.content else {}

    def record_measurement(self, job_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Post one measurement and return its status and body.

        Returns the answer rather than raising on a non-2xx, because delivery
        decides what a status means: a 409 here is a *measurement* conflict,
        not a lost lease, and the shared ``_post`` would raise ``LeaseLost``
        for it and end a turn that had already succeeded.
        """
        response = self._client.post(
            f"{self._base}/v1/agent/worker/jobs/{job_id}/measurements",
            json=self._redactor.payload(body),
            timeout=DELIVERY_TIMEOUT_S,
        )
        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {}
        return response.status_code, parsed if isinstance(parsed, dict) else {}

    def append_event(self, event: NormalizedEvent) -> None:
        """Report one normalized event, retrying with backoff since replay is safe.

        The retry exists *because of* ``event_seq``: before it, a response
        lost on the wire left the runner unable to tell "never stored" from
        "stored, answer dropped", and re-sending risked a duplicate row in the
        owner's stream. With the number attached, the server answers a replay
        with the already-stored id.

        One immediate retry was not enough: a control plane being recreated by
        a deploy refuses connections, or its proxy answers 502, for longer
        than that, and the escaping error ended an otherwise healthy turn and
        marked its native context lost. Transport errors and transient statuses
        are retried for a bounded time instead, well inside the lease the
        heartbeat keeps renewing meanwhile; a lost lease (409) still ends the
        turn at once.
        """
        payload = self._redactor.payload(event.payload)
        if isinstance(event.payload, dict) and isinstance(event.payload.get("delta"), str):
            stream_id = str(
                event.payload.get("preview_id") or event.payload.get("id") or event.event_type
            )
            if event.payload.get("reset") or stream_id not in self._preview_redactors:
                self._preview_redactors[stream_id] = SecretRedactor(self._secret_values)
            payload["delta"] = self._preview_redactors[stream_id].feed(event.payload["delta"])
            self._preview_contexts[stream_id] = (
                event.event_type,
                {k: v for k, v in payload.items() if k not in {"delta", "reset"}},
            )
        self._event_seq += 1
        body = {
            "event_type": event.event_type,
            "payload": payload,
            "event_seq": self._event_seq,
        }
        path = f"/v1/agent/worker/jobs/{self.job_id}/events"
        give_up_at = time.monotonic() + _EVENT_RETRY_BUDGET_S
        delay = 1.0
        while True:
            try:
                self._post(path, body, already_redacted=True)
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in _EVENT_RETRY_STATUSES
                ) or time.monotonic() + delay > give_up_at:
                    raise
                _log_control_failure("event_retry", self.job_id, exc)
            time.sleep(delay)
            delay = min(delay * 2, _EVENT_RETRY_MAX_DELAY_S)

    def _get(self, path: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Read from the worker surface, with the same fencing as a write."""
        response = self._client.get(
            f"{self._base}{path}",
            timeout=self._client.timeout if timeout is None else timeout,
        )
        if response.status_code == 409:
            raise LeaseLost(response.text[:200])
        response.raise_for_status()
        return response.json() if response.content else {}

    def list_attachments(self) -> list[dict[str, Any]]:
        """Every upload in this conversation, without the bytes."""
        body = self._get(f"/v1/agent/worker/jobs/{self.job_id}/attachments")
        return list(body.get("attachments") or [])

    def read_attachment(self, attachment_id: int) -> tuple[str, bytes]:
        """One upload's path and bytes."""
        body = self._get(
            f"/v1/agent/worker/jobs/{self.job_id}/attachments/{attachment_id}",
            timeout=_ATTACHMENT_TIMEOUT_S,
        )
        return str(body["path"]), base64.b64decode(body["content_base64"])

    def heartbeat(self, ttl: float) -> dict[str, Any]:
        """Renew the lease and learn whether cancellation is pending."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/heartbeat",
            {"lease_ttl_seconds": ttl},
            timeout=_HEARTBEAT_TIMEOUT_S,
        )

    def report_continuity(self, mode: str, *, context_status: str | None = None) -> None:
        """Record this attempt's continuity rung. Best-effort observability."""
        try:
            body = {"mode": mode}
            if context_status is not None:
                body["context_status"] = context_status
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/continuity", body)
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_environment_snapshot(
        self, *, snapshot_id: str | None, outcome: str, base_sha: str | None
    ) -> None:
        """Say what a new workspace did with its installation snapshot. Best-effort.

        A snapshot only saves time, so neither its accounting nor the build a
        missing one queues may fail the turn.
        """
        try:
            self._post(
                f"/v1/agent/worker/jobs/{self.job_id}/environment-snapshot",
                {"snapshot_id": snapshot_id, "outcome": outcome, "base_sha": base_sha},
            )
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_context_status(self, context_status: str) -> None:
        """Record NEW/LIVE/RESTORED/LOST without inventing a continuity rung."""
        try:
            self._post(
                f"/v1/agent/worker/jobs/{self.job_id}/continuity",
                {"context_status": context_status},
            )
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_resources(self, rollup: dict[str, Any]) -> None:
        """Report one closed attempt's resource and timing telemetry. Best-effort.

        Swallowed like every other observability write: a turn that ran must
        not fail because the platform could not record its telemetry.
        """
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/resources", rollup)
        except LeaseLost:
            raise
        except Exception:
            pass

    def report_turn_resources(self, payload: dict[str, Any]) -> None:
        """Report one cumulative whole-turn member execution snapshot.

        The same method serves a running ``collecting`` checkpoint and the
        closing ``final`` snapshot: the body carries its own state and
        ``report_seq``, and the endpoint treats a resend at the same sequence
        as the same report. Swallowed like every other observability write --
        a turn that ran must not fail because the platform could not record
        its telemetry -- with the loss of the lease still fatal.

        There is deliberately no retry here. The payload is a cumulative
        snapshot with a monotonic ``report_seq``, so a checkpoint lost to a
        transport error is superseded by the next one: retrying would upload
        a stale picture while the current one is already being assembled.
        """
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/turn-resources", payload)
        except LeaseLost:
            raise
        except Exception:
            pass

    def reserve_chat_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reserve immutable thread/container identity under this attempt fence."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/reserve",
            payload,
        )

    def mark_chat_sandbox_running(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        container_id: str,
        context_status: str,
    ) -> dict[str, Any]:
        """Record that Docker started the reserved activation."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/running",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "container_id": container_id,
                "context_status": context_status,
            },
        )

    def record_chat_process(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
        process_generation: int,
    ) -> None:
        """Record a CLI process rotation inside the same chat container."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/process",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "residency_generation": residency_generation,
                "process_generation": process_generation,
            },
        )

    def establish_shared_residency(
        self,
        *,
        sandbox_id: str,
        runtime_id: str,
        runtime_version: str | None,
    ) -> dict[str, Any]:
        """Obtain the member-scoped renewable credential owned by the sandbox manager."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/residency",
            {
                "sandbox_id": sandbox_id,
                "runtime_id": runtime_id,
                "runtime_version": runtime_version,
            },
        )

    def admit_chat_turn(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
    ) -> None:
        """Reserve this job before its prompt may reach Agent CLI stdin."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/admit",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "residency_generation": residency_generation,
            },
            timeout=10.0,
        )

    def settle_chat_sandbox(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        """Persist a native context boundary before the host starts its idle timer."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/settle",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "residency_generation": residency_generation,
                "context_status": context_status,
                "native_session_id": native_session_id,
                "base_sha": base_sha,
                "idle_timeout_seconds": idle_timeout_seconds,
            },
            timeout=10.0,
        )

    def abandon_chat_sandbox(
        self,
        *,
        sandbox_generation: int,
        activation_generation: int,
        lost: bool,
        detail: str | None,
        idle_timeout_seconds: float,
    ) -> dict[str, Any]:
        """Release a failed activation without claiming context continuity."""
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/abandon",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "lost": lost,
                "detail": detail,
                "idle_timeout_seconds": idle_timeout_seconds,
            },
        )

    def release_turn(self, reason: str) -> bool:
        """Hand this never-admitted turn back to the queue without spending an attempt.

        The control plane refuses once the turn was admitted, so ``False`` is
        never a reason to retry: the lease decides, as it always would.
        """
        try:
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/release", {"reason": reason})
        except Exception as exc:
            _log_control_failure("release", self.job_id, exc)
            return False
        return True

    def suspend_terminals(self, phase: str) -> None:
        """Freeze owner PTYs before workspace preparation or group snapshot capture."""
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/terminals/suspend",
            {"phase": phase},
        )

    def resume_terminals(self) -> None:
        """Resume retained PTYs and publish readiness for this attempt."""
        self._post(f"/v1/agent/worker/jobs/{self.job_id}/terminals/resume")

    def finish(self, state: str, detail: str | None = None, base_sha: str | None = None) -> None:
        """Drive the fenced terminal transition.

        ``base_sha`` reports the commit the agent actually worked from. A job
        may be submitted without one, and the publisher cannot apply a patch
        without knowing its base — so the runner, which resolves it when it
        checks the repository out, is the component that knows.
        """
        for stream_id, redactor in tuple(self._preview_redactors.items()):
            final = redactor.flush()
            if final:
                event_type, payload = self._preview_contexts[stream_id]
                self._event_seq += 1
                body = {
                    "event_type": event_type,
                    "payload": {**payload, "delta": final},
                    "event_seq": self._event_seq,
                }
                try:
                    self._post(
                        f"/v1/agent/worker/jobs/{self.job_id}/events", body, already_redacted=True
                    )
                except httpx.TransportError:
                    self._post(
                        f"/v1/agent/worker/jobs/{self.job_id}/events", body, already_redacted=True
                    )
        self._preview_redactors.clear()
        self._preview_contexts.clear()
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/finish",
            {"state": state, "detail": detail, "base_sha": base_sha},
        )

    job_id: str = ""
