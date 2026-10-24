"""Attempt-scoped HTTP client for worker control-plane endpoints.

The runner constructs this client at its orchestration boundary. This module
owns request payloads, fencing responses, event delivery and replay, and
request deadlines; lease renewal and turn scheduling remain in the runner.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx

from cloud_agent_host.claimed_job import LeaseLost
from cloud_agent_host.event_sender import OrderedSender
from cloud_agent_host.logging_utils import get_logger
from cloud_agent_host.measurement_delivery import DELIVERY_TIMEOUT_S
from cloud_agent_host.runtimes import NormalizedEvent  # noqa: TC001 — Public method introspection.
from cloud_agent_shared.secret_redaction import SecretRedactor
from cloud_agent_shared.shared_protocol import ABANDON_DETAIL_MAX_CHARS
from cloud_agent_shared.storable_text import storable_json

if TYPE_CHECKING:
    from collections.abc import Callable

# Heartbeats must answer before ordinary lifecycle requests consume the lease.
_HEARTBEAT_TIMEOUT_S = 10.0
# A question notification is only a wake-up; the 60 s poll is its recovery path.
_QUESTION_HINT_TIMEOUT_S = 2.0
# Attachments may need longer than the client's default deadline for transfer.
_ATTACHMENT_TIMEOUT_S = 120.0
#: How long one event upload keeps retrying an unreachable control plane, and
#: the backoff ceiling between tries. Half the default lease: long enough to
#: ride out a control-plane container being recreated, short enough that a
#: turn the platform cannot record is not left running unseen.
_EVENT_RETRY_BUDGET_S = 60.0
_EVENT_RETRY_MAX_DELAY_S = 8.0
_EVENT_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
#: What a control plane from before the batch endpoint answers it with.
_EVENT_BATCH_UNSUPPORTED = frozenset({404, 405})
_JSON_CONTENT = {"Content-Type": "application/json"}

logger = get_logger(__name__)


def _event_json(event_type: str, payload: Any, event_seq: int) -> bytes:
    """One event as an upload carries it, encoded the way httpx encodes JSON.

    Encoded where it is reported, so an event that cannot be sent fails the
    append that reported it, and the sender only joins what is encoded.
    """
    body = {"event_type": event_type, "payload": payload, "event_seq": event_seq}
    return json.dumps(
        # Tool output can hold half a surrogate pair, which UTF-8 cannot
        # encode, or a NUL, which the control plane cannot store.
        storable_json(body),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _log_control_failure(operation: str, job_id: str, exc: Exception) -> None:
    """Keep a diagnosable failure without logging tokens or arbitrary response bodies.

    One ``agent_control_failure`` record, for the runner's control calls and
    for the retries this client makes itself.
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
        # One appending thread at a time: the numbers, the redactors' state and
        # the queue's order are only the same order while it holds this.
        self._appending = threading.Lock()
        self._events = OrderedSender(self._deliver_events)
        # Cleared by a control plane from before the batch endpoint.
        self._batches = True

    @property
    def lease_lost(self) -> bool:
        """Whether an event upload was refused for a lost lease.

        Uploads are not waited for, so the turn loop reads this beside the
        heartbeat's own flag, and a lost lease ends the turn there.
        """
        return self._events.lease_lost

    def close(self) -> None:
        """Stop the event sender and release the HTTP connection pool.

        Whatever events are still queued are dropped: ``finish`` delivers a
        turn's events, so what remains belongs to an attempt that lost its
        lease or never ended, and cannot be recorded now.
        """
        self._events.close()
        self._client.close()

    def _post(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = self._redactor.payload(payload or {})
        response = self._client.post(
            f"{self._base}{path}",
            # A reason quoting what the sandbox printed can hold half a
            # surrogate pair, which httpx cannot encode as UTF-8, or a NUL,
            # which the control plane cannot store.
            json=storable_json(body),
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
        """Number, redact and queue one normalized event for the ordered sender.

        Everything that depends on order happens here, on the caller's thread:
        the secret redaction, a preview's redactor (which carries state from
        one delta to the next), the number and the encoding. The call returns
        once the event is queued, so a slow or restarting control plane no
        longer holds up the turn's stream; it waits only while the sender holds
        its fill. What made the sender give up is raised here, by the next
        append after it: a lost lease, or a control plane that refused an
        upload or stayed unreachable for the whole retry budget. Unless the
        lease was lost, that append's event is queued first: it is often the
        error the ending turn reports, once, and the finish that follows
        delivers it to a control plane that is back.
        """
        with self._appending:
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
            self._events.put(_event_json(event.event_type, payload, self._event_seq))

    def flush(self) -> None:
        """Wait until every queued event is stored, or raise what stopped one.

        Raises what the next append would: a lost lease, or what made the
        sender give up.
        """
        self._events.flush()

    def _drain_events(self) -> None:
        """Deliver every queued event ahead of a write that ends the attempt.

        What each preview's redactor still holds back is queued first. The
        ending write is attempted even when the drain was given up on, as it
        was when each event was posted on its own: an event the control plane
        could not record must not strand the attempt as running. A lost lease
        is final.
        """
        try:
            with self._appending:
                for stream_id, redactor in tuple(self._preview_redactors.items()):
                    final = redactor.flush()
                    if final:
                        event_type, payload = self._preview_contexts[stream_id]
                        self._event_seq += 1
                        self._events.put(
                            _event_json(event_type, {**payload, "delta": final}, self._event_seq)
                        )
                self._preview_redactors.clear()
                self._preview_contexts.clear()
            self.flush()
        except LeaseLost:
            raise
        except Exception as exc:
            _log_control_failure("event_drain", self.job_id, exc)

    def _deliver_events(self, batch: list[bytes], stopped: Callable[[], bool]) -> None:
        """Store one batch of encoded events in order, or raise why it was not stored."""
        if self._batches:
            try:
                self._upload("events:batch", b'{"events":[' + b",".join(batch) + b"]}", stopped)
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _EVENT_BATCH_UNSUPPORTED:
                    raise
                # A control plane from before batches, still answering while a
                # deploy rolls forward, or again after one rolled back.
                # COMPAT(remove-after=2026-12-31): every control plane serves
                # events:batch; this fallback to one post per event goes then.
                self._batches = False
                logger.info(
                    "agent_event_batches_unsupported job_id=%s status_code=%s",
                    self.job_id,
                    exc.response.status_code,
                )
        for body in batch:
            self._upload("events", body, stopped)

    def _upload(self, endpoint: str, content: bytes, stopped: Callable[[], bool]) -> None:
        """Post encoded events, retrying with backoff since replay is safe.

        The retry exists *because of* ``event_seq``: before it, a response
        lost on the wire left the runner unable to tell "never stored" from
        "stored, answer dropped", and re-sending risked a duplicate row in the
        owner's stream. With the number attached, the server answers a replay
        with the already-stored id, so a batch is sent again whole.

        One immediate retry was not enough: a control plane being recreated by
        a deploy refuses connections, or its proxy answers 502, for longer
        than that, and the escaping error ended an otherwise healthy turn and
        marked its native context lost. Transport errors and transient statuses
        are retried for a bounded time instead, well inside the lease the
        heartbeat keeps renewing meanwhile; a lost lease (409) is not retried,
        and neither is anything once the client is closed.
        """
        url = f"{self._base}/v1/agent/worker/jobs/{self.job_id}/{endpoint}"
        give_up_at = time.monotonic() + _EVENT_RETRY_BUDGET_S
        delay = 1.0
        while True:
            try:
                response = self._client.post(url, content=content, headers=_JSON_CONTENT)
                if response.status_code == 409:
                    raise LeaseLost(response.text[:200])
                response.raise_for_status()
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if (
                    (
                        isinstance(exc, httpx.HTTPStatusError)
                        and exc.response.status_code not in _EVENT_RETRY_STATUSES
                    )
                    or time.monotonic() + delay > give_up_at
                    or stopped()
                ):
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

    def report_question_change(self, job_id: str) -> None:
        """Wake the owner's question reader; polling recovers a dropped hint."""
        try:
            self._post(
                f"/v1/agent/worker/jobs/{job_id}/questions:changed",
                {},
                timeout=_QUESTION_HINT_TIMEOUT_S,
            )
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
        """Release a failed activation without claiming context continuity.

        The reason is kept to what the control plane accepts, since it refuses
        the whole release for a longer one.
        """
        return self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/sandbox/abandon",
            {
                "sandbox_generation": sandbox_generation,
                "activation_generation": activation_generation,
                "lost": lost,
                "detail": None if detail is None else detail[:ABANDON_DETAIL_MAX_CHARS],
                "idle_timeout_seconds": idle_timeout_seconds,
            },
        )

    def release_turn(self, reason: str) -> bool:
        """Hand this never-admitted turn back to the queue without spending an attempt.

        The control plane refuses once the turn was admitted, so ``False`` is
        never a reason to retry: the lease decides, as it always would. The
        release ends the attempt, so the events it queued are delivered first.
        """
        try:
            self._drain_events()
            self._post(f"/v1/agent/worker/jobs/{self.job_id}/release", {"reason": reason})
        except Exception as exc:
            _log_control_failure("release", self.job_id, exc)
            return False
        return True

    def suspend_terminals(self, phase: str) -> None:
        """Freeze owner PTYs before workspace preparation or group snapshot capture.

        The endpoint writes its own lifecycle event, so the events this attempt
        queued before it are stored first, in the order they happened.
        """
        self.flush()
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/terminals/suspend",
            {"phase": phase},
        )

    def resume_terminals(self) -> None:
        """Resume retained PTYs and publish readiness for this attempt.

        After the events queued before it, as :meth:`suspend_terminals` is.
        """
        self.flush()
        self._post(f"/v1/agent/worker/jobs/{self.job_id}/terminals/resume")

    def finish(self, state: str, detail: str | None = None, base_sha: str | None = None) -> None:
        """Drive the fenced terminal transition, after the attempt's events.

        ``base_sha`` reports the commit the agent actually worked from. A job
        may be submitted without one, and the publisher cannot apply a patch
        without knowing its base — so the runner, which resolves it when it
        checks the repository out, is the component that knows.

        Every event the attempt reported is delivered first, so the error a
        failing turn reported is stored ahead of the turn's end.
        """
        self._drain_events()
        self._post(
            f"/v1/agent/worker/jobs/{self.job_id}/finish",
            {"state": state, "detail": detail, "base_sha": base_sha},
        )

    job_id: str = ""
