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
    if handled:
        client.report(executor.observed(), _operation_summaries(store))
    return handled


def pending_results(store: AgentStore) -> list[dict[str, Any]]:
    """Journal entries for intents whose final state may not have reached the control plane."""
    return [r for r in store.operations() if r.get("intent_id") and r.get("state") != "running"]


__all__ = ["ControlPlaneClient", "ControlPlaneUnavailable", "pending_results", "sync_once"]
