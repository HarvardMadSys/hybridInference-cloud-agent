"""The deployment agent's side of the control-plane protocol: pull intents, push reports.

The agent always initiates: the control plane has no way to reach it, so it
cannot run anything here that the agent did not fetch and validate itself.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deploy.agent.operations import Executor
    from deploy.agent.store import AgentStore


class ControlPlaneUnavailable(RuntimeError):
    """The control plane could not be reached or refused the agent."""


@dataclass
class ControlPlaneClient:
    """JSON over HTTPS (or loopback HTTP) with the agent's bearer credential."""

    base_url: str
    credential: str | None = None
    timeout: float = 15.0

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url.rstrip("/") + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.credential:
            request.add_header("Authorization", f"Bearer {self.credential}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise ControlPlaneUnavailable(f"the control plane answered {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ControlPlaneUnavailable("the control plane is unreachable") from exc
        return json.loads(text) if text else None

    def register(self, token: str, identity: dict[str, Any]) -> dict[str, Any]:
        """Exchange a one-time enrollment token for this agent's credential."""
        return self._request(
            "POST",
            "/v1/agent/deploy/register",
            {
                "token": token,
                "name": identity["name"],
                "role": identity["role"],
                "machine": identity["machine"],
                "public_key": identity["public_key"],
            },
        )

    def report(self, observed: dict[str, Any], operations: list[dict[str, Any]]) -> None:
        """Send this machine's facts and the state of its recent operations."""
        self._request(
            "POST", "/v1/agent/deploy/report", {"observed": observed, "operations": operations}
        )

    def intents(self, after_seq: int) -> list[dict[str, Any]]:
        """Intents for this agent with a sequence number above ``after_seq``."""
        answer = self._request("GET", f"/v1/agent/deploy/intents?after_seq={int(after_seq)}")
        return list((answer or {}).get("intents") or [])

    def result(self, intent_id: str, state: str, detail: str, evidence: dict[str, Any]) -> None:
        """Report one intent's state."""
        self._request(
            "POST",
            f"/v1/agent/deploy/intents/{intent_id}/result",
            {"state": state, "detail": detail[:2000], "evidence": evidence},
        )


def _operation_summaries(store: AgentStore, limit: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "id": record["id"],
            "intent_id": record.get("intent_id"),
            "operation": record.get("operation"),
            "state": record.get("state"),
            "detail": str(record.get("detail") or "")[:2000],
            "origin": record.get("origin"),
            # A migration's last finished step; its journal stays on this machine.
            "phase": ((record.get("migration") or {}).get("done") or [None])[-1],
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
        }
        for record in store.operations()[-limit:]
    ]


def sync_once(executor: Executor, client: ControlPlaneClient, *, now: float | None = None) -> int:
    """Report, run every new intent in order, report again; returns intents handled.

    An intent whose sequence number is not above the last one started here is
    a replay and is refused; an expired one is refused; one for another agent
    is refused. Each is reported, and nothing runs for it.
    """
    store = executor.store
    client.report(executor.observed(), _operation_summaries(store))
    deliver_results(client, store)
    handled = 0
    for intent in client.intents(int(store.state().get("last_seq", 0))):
        handled += 1
        now_s = time.time() if now is None else now
        last = int(store.state().get("last_seq", 0))
        intent_id = str(intent.get("intent_id") or "")
        if intent.get("agent_id") != executor.agent_id:
            client.result(intent_id, "rejected", "this intent is for another agent", {})
            continue
        if int(intent.get("seq") or 0) <= last:
            client.result(
                intent_id, "rejected", "replayed intent: its sequence number was already used", {}
            )
            continue
        if float(intent.get("expires_at") or 0) < now_s:
            state = store.state()
            state["last_seq"] = int(intent["seq"])
            store.save_state(state)
            client.result(intent_id, "rejected", "the intent expired before it was fetched", {})
            continue
        client.result(intent_id, "running", "started", {})
        outcome = executor.execute(
            str(intent.get("operation")),
            dict(intent.get("params") or {}),
            dict(intent.get("sealed") or {}),
            intent_id=intent_id,
            seq=int(intent["seq"]),
            origin="intent",
        )
        client.result(intent_id, outcome.state, outcome.detail, outcome.evidence)
        _mark_delivered(store, intent_id)
    if handled:
        client.report(executor.observed(), _operation_summaries(store))
    return handled


def pending_results(store: AgentStore) -> list[dict[str, Any]]:
    """Journal entries for intents whose final state has not reached the control plane."""
    return [
        r
        for r in store.operations()
        if r.get("intent_id") and r.get("state") != "running" and not r.get("delivered")
    ]


def _mark_delivered(store: AgentStore, record_id: str) -> None:
    record = store.operation(record_id)
    if record is not None:
        record["delivered"] = True
        store.save_operation(record)


def deliver_results(client: ControlPlaneClient, store: AgentStore) -> None:
    """Send every final state the control plane has not acknowledged.

    A result can miss the control plane: a migration finishes while the service
    restarts on the target. It stays in the journal until a call succeeds; one
    the control plane already holds is simply not accepted again.
    """
    for record in pending_results(store):
        client.result(
            str(record["intent_id"]),
            str(record["state"]),
            str(record.get("detail") or ""),
            dict(record.get("evidence") or {}),
        )
        _mark_delivered(store, str(record["id"]))


def recover_interrupted(executor: Executor) -> list[str]:
    """Settle what the last run left ``running``: resume a migration, fail anything else.

    Run once before the agent's first sync. Nothing but a migration resumes by
    itself: the rest may have stopped anywhere, so it is reported as interrupted
    with the configuration version in force, and the workflow decides what next.
    """
    from deploy.agent.database import recover

    settled = []
    for record in executor.store.operations():
        if record.get("state") != "running":
            continue
        outcome = recover(executor, record)
        if outcome is None:
            record.update(
                state="failed",
                detail="interrupted when the agent restarted; configuration version "
                f"{executor.store.state().get('config_rev')} is in force",
            )
            executor.store.save_operation(record)
        settled.append(str(record["id"]))
    return settled


__all__ = [
    "ControlPlaneClient",
    "ControlPlaneUnavailable",
    "deliver_results",
    "pending_results",
    "recover_interrupted",
    "sync_once",
]
