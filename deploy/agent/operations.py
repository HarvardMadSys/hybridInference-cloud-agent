"""Execute one operation of the closed set, journaled, with evidence, on this machine.

An operation comes either from a control-plane intent or from a local
command; both go through :meth:`Executor.execute`, so both are validated
against the shared contract and this machine's policy, written to the journal
before anything changes, and judged by evidence rather than by an exit status
alone: a configuration change is kept only if its services come back healthy,
a release activation only if ``.deployed-sha`` names the manifest's commit, a
Kata install only if the setup script's own ``--check`` accepts it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from deploy.agent import releases
from deploy.agent.deployment_contract import (
    CONFIG_KEYS_BY_ROLE,
    ContractError,
    check_operation,
    open_sealed,
    sealed_purpose,
    services_for,
)
from deploy.agent.store import AgentStore, StoreError, parse_env, render_env
from deploy.agent.system import Compose, http_ok, wait_until

if TYPE_CHECKING:
    from collections.abc import Callable

    from deploy.agent.system import CommandRunner

#: Names retired from the inventory that an agent reports when still present.
LEGACY_NAMES = frozenset(
    {
        "AGENT_WORKSPACE_BROKER_TOKEN",
        "AGENT_WORKSPACE_BROKER_URL",
        "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
        "AGENT_BROKER_URL_ALLOWED_HOSTS",
        "AGENT_BROKER_REVERSE_BIND",
        "AGENT_BROKER_REVERSE_PORT",
        "AGENT_BROKER_REVERSE_TARGET_HOST",
        "AGENT_BROKER_REVERSE_TARGET_PORT",
        "AGENT_BROKER_STOP_GRACE",
        "AGENT_RUNNER_REPLICAS",
    }
)


class OperationFailed(RuntimeError):
    """The operation ran and did not reach its evidence; the message is safe to report."""


@dataclass
class Outcome:
    """What an operation ended as, for the journal and the control plane."""

    state: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Executor:
    """Runs operations on one machine."""

    store: AgentStore
    runner: CommandRunner
    #: Seconds to wait for services to come back healthy after a change.
    health_timeout: float = 120.0
    health_interval: float = 2.0
    #: Extra handlers (rotation, database) registered by other modules.
    handlers: dict[str, Callable[..., Outcome]] = field(default_factory=dict)
    #: How a loopback health endpoint is asked (replaced in tests).
    http_check: Callable[[str], bool] = http_ok
    #: Called as a long operation passes a step, so the control plane sees it.
    progress: Callable[[], None] = field(default=lambda: None)

    @property
    def role(self) -> str:
        """``control-plane`` or ``host``."""
        return str(self.store.identity()["role"])

    @property
    def agent_id(self) -> str:
        """This agent's id, or ``local`` before registration."""
        return str(self.store.identity().get("agent_id") or "local")

    def compose(self) -> Compose:
        """Compose for this machine's project."""
        return Compose(self.runner, self.store.deploy_root, self.store.policy())

    # -- entry --------------------------------------------------------------

    def execute(
        self,
        operation: str,
        params: dict[str, Any],
        sealed: dict[str, Any] | None = None,
        *,
        intent_id: str | None = None,
        record_id: str | None = None,
        seq: int | None = None,
        origin: str = "local",
    ) -> Outcome:
        """Validate, journal, run and judge one operation."""
        record_id = record_id or intent_id or uuid.uuid4().hex
        record = {
            "id": record_id,
            "intent_id": intent_id,
            "seq": seq,
            "operation": operation,
            "params": params,
            "sealed_names": sorted(sealed or {}),
            "origin": origin,
            "state": "running",
            "created_at": time.time(),
        }
        existing = self.store.operation(record_id)
        if existing and "migration" in existing:
            # A resumed migration keeps its journal of completed steps.
            record["migration"] = existing["migration"]
        try:
            current = parse_env(self.store.current_env())
            check_operation(
                operation, self.role, params, sealed, policy=self.store.policy(), current=current
            )
        except (ContractError, StoreError) as exc:
            record.update(state="rejected", detail=str(exc))
            self.store.save_operation(record)
            return Outcome("rejected", str(exc))
        if seq is not None:
            state = self.store.state()
            state["last_seq"] = seq
            self.store.save_state(state)
        self.store.save_operation(record)
        try:
            outcome = self._dispatch(operation, params, sealed or {}, record_id)
        except (OperationFailed, releases.ReleaseError, StoreError, ContractError) as exc:
            outcome = Outcome("failed", str(exc))
        latest = self.store.operation(record_id)
        if latest and "migration" in latest:
            # The handler journaled its steps on the same record.
            record["migration"] = latest["migration"]
        record.update(state=outcome.state, detail=outcome.detail, evidence=outcome.evidence)
        self.store.save_operation(record)
        return outcome

    def _dispatch(
        self, operation: str, params: dict[str, Any], sealed: dict[str, Any], record_id: str
    ) -> Outcome:
        if operation in self.handlers:
            return self.handlers[operation](self, params, sealed, record_id)
        if operation == "apply_config":
            return self.apply_config(params.get("changes") or {}, sealed)
        if operation == "rollback_config":
            return self.rollback_config(int(params["to_rev"]))
        if operation == "restart_services":
            return self.restart(list(params["services"]))
        if operation.startswith("release_"):
            return self.release(operation, params["manifest_sha256"])
        if operation == "kata_install":
            return self.kata_install(str(params["version"]), str(params["sha256"]))
        raise OperationFailed(f"{operation} is not available on this agent version")

    # -- configuration ------------------------------------------------------

    def open_values(self, sealed: dict[str, Any]) -> dict[str, str]:
        """Open sealed values bound to this agent; the plaintext stays in memory."""
        key = self.store.private_key()
        opened = {}
        for name, box in sealed.items():
            value = open_sealed(key, box, sealed_purpose(self.agent_id, name))
            spec = CONFIG_KEYS_BY_ROLE[self.role].get(name)  # type: ignore[index]
            if spec is not None:
                value = spec.check(value)
            opened[name] = value
        return opened

    def apply_config(self, changes: dict[str, str], sealed: dict[str, Any]) -> Outcome:
        """Write a new version with ``changes``, restart what they affect, keep it if healthy."""
        values = {**changes, **self.open_values(sealed)}
        previous_rev = self.store.adopt_existing()
        text = render_env(self.store.current_env(), values)
        return self._switch(text, sorted(values), previous_rev)

    def rollback_config(self, to_rev: int) -> Outcome:
        """Re-apply a stored version (local recovery and the control plane's rollback)."""
        previous_rev = self.store.adopt_existing()
        target = parse_env(self.store.version_text(to_rev))
        current = parse_env(self.store.current_env())
        names = sorted(
            name
            for name in set(target) | set(current)
            if target.get(name) != current.get(name) and name in CONFIG_KEYS_BY_ROLE[self.role]  # type: ignore[index]
        )
        return self._switch(self.store.version_text(to_rev), names, previous_rev)

    def _switch(self, text: str, names: list[str], previous_rev: int) -> Outcome:
        rev = self.store.stage_version(text)
        self.store.activate_version(rev)
        services = services_for(
            self.role, [n for n in names if n in CONFIG_KEYS_BY_ROLE[self.role]]
        )  # type: ignore[arg-type,index]
        if not services:
            return Outcome(
                "succeeded",
                f"configuration version {rev} stored; nothing to restart",
                {"config_rev": rev},
            )
        healthy, states = self._restart_and_check(services)
        if healthy:
            return Outcome(
                "succeeded",
                f"configuration version {rev} applied",
                {"config_rev": rev, "services": states},
            )
        # Put the last good version back and bring the services up on it.
        self.store.activate_version(previous_rev)
        restored, restored_states = self._restart_and_check(services)
        detail = (
            f"configuration version {rev} did not come up healthy; restored version {previous_rev}"
            + ("" if restored else " but its services are not healthy either")
        )
        return Outcome(
            "failed",
            detail,
            {"config_rev": previous_rev, "failed_rev": rev, "services": restored_states},
        )

    def restart(self, services: list[str]) -> Outcome:
        """Recreate named services and wait for them."""
        healthy, states = self._restart_and_check(services)
        if not healthy:
            raise OperationFailed("the services did not come back healthy")
        return Outcome("succeeded", "services recreated", {"services": states})

    def _restart_and_check(self, services: list[str]) -> tuple[bool, dict[str, bool]]:
        compose = self.compose()
        if self.role == "host":
            drained = compose.drain(services)
            if not drained.ok:
                return False, dict.fromkeys(services, False)
        result = compose.recreate(services)
        if not result.ok:
            return False, dict.fromkeys(services, False)
        states: dict[str, bool] = {}

        def healthy() -> bool:
            states.clear()
            states.update(compose.running(services))
            return all(states.values()) and self._listeners_answer(services)

        ok = wait_until(healthy, timeout=self.health_timeout, interval=self.health_interval)
        return ok, dict(states)

    def _listeners_answer(self, services: list[str]) -> bool:
        """The control plane's own health endpoints, on the address it now binds."""
        if self.role != "control-plane":
            return True
        env = parse_env(self.store.current_env())
        checks = []
        if "control-plane" in services:
            checks.append(("AGENT_CONTROL_PLANE_BIND", "AGENT_CONTROL_PLANE_PORT", "8000"))
        if "mcp-relay" in services:
            checks.append(("AGENT_MCP_RELAY_BIND", "AGENT_MCP_RELAY_PORT", "8001"))
        for bind_name, port_name, default_port in checks:
            bind = env.get(bind_name, "127.0.0.1") or "127.0.0.1"
            host = "127.0.0.1" if bind in {"0.0.0.0", "::"} else bind
            host = f"[{host}]" if ":" in host else host
            if not self.http_check(
                f"http://{host}:{env.get(port_name, default_port) or default_port}/healthz"
            ):
                return False
        return True

    # -- releases -----------------------------------------------------------

    def release(self, operation: str, manifest_sha256: str) -> Outcome:
        """Run one recipe phase for a locked release and check what it should have done."""
        phase = operation.removeprefix("release_").replace("_", "-")
        refusal = releases.rollback_allowed(self.store, manifest_sha256)
        if refusal and phase in {"prepare", "activate"}:
            raise OperationFailed(refusal)
        result, manifest = releases.run_phase(
            self.store, self.runner, role=self.role, manifest_sha256=manifest_sha256, phase=phase
        )
        if not result.ok:
            raise OperationFailed(f"deploy.sh --phase {phase} failed (exit {result.code})")
        commit = manifest["source_commit"]
        if phase == "prepare":
            missing = releases.images_present(self.runner, self.store, manifest, self.role)
            if missing:
                raise OperationFailed(
                    f"{len(missing)} release image(s) are not present after prepare"
                )
            return Outcome("succeeded", "release prepared", {"manifest_sha256": manifest_sha256})
        if phase == "activate":
            deployed = releases.deployed_commit(self.store)
            if deployed != commit:
                raise OperationFailed(
                    "the deployment does not record the release's commit after activate"
                )
            state = self.store.state()
            state["release"] = {"manifest_sha256": manifest_sha256, "commit": commit}
            self.store.save_state(state)
            return Outcome("succeeded", "release active", {"deployed_commit": deployed})
        return Outcome("succeeded", f"{phase} finished", {"manifest_sha256": manifest_sha256})

    # -- Kata ---------------------------------------------------------------

    def kata_install(self, version: str, sha256: str) -> Outcome:
        """Stop the runner, install a reviewed pair through the helper, check, restart."""
        helper = self.store.policy().get("kata_helper")
        if (
            not isinstance(helper, list)
            or not helper
            or not all(isinstance(a, str) for a in helper)
        ):
            raise OperationFailed("this machine's policy names no Kata helper command")
        compose = self.compose()
        stopped = compose.stop(["runner"])
        if not stopped.ok:
            raise OperationFailed("the runner did not stop")
        installed = self.runner.run(
            [*helper, "install", version, sha256], cwd=self.store.deploy_root, timeout=3600
        )
        checked = self.runner.run([*helper, "check", version, sha256], cwd=self.store.deploy_root)
        healthy, states = self._restart_and_check(["runner"])
        if not installed.ok or not checked.ok:
            raise OperationFailed(
                "the Kata helper did not install and verify this version"
                + ("" if healthy else "; the runner is not healthy either")
            )
        state = self.store.state()
        state["kata"] = {"version": version, "sha256": sha256, "checked_at": time.time()}
        self.store.save_state(state)
        return Outcome(
            "succeeded" if healthy else "failed",
            "Kata installed and checked" + ("" if healthy else "; the runner did not come back"),
            {"kata": {"version": version, "sha256": sha256}, "services": states},
        )

    # -- report -------------------------------------------------------------

    def observed(self) -> dict[str, Any]:
        """Facts about this machine, for the control plane; no value of any variable."""
        env = parse_env(self.store.current_env())
        known = CONFIG_KEYS_BY_ROLE[self.role]  # type: ignore[index]
        policy = self.store.policy()
        state = self.store.state()
        lock = self.store.release_lock()
        services = list(policy.get("report_services") or [])
        return {
            "deployed_commit": releases.deployed_commit(self.store),
            "config_rev": state.get("config_rev"),
            "config_versions": self.store.versions(),
            "managed_keys": sorted(name for name in env if name in known),
            "unmanaged_keys": sorted(name for name in env if name not in known),
            "legacy_names_present": sorted(name for name in env if name in LEGACY_NAMES),
            "releases": {
                sha: {k: v for k, v in entry.items() if k != "recipe_sha256"}
                for sha, entry in lock.items()
            },
            "release": state.get("release"),
            "kata": state.get("kata"),
            "runner_host": env.get("AGENT_RUNNER_HOST", "") if self.role == "host" else "",
            "services": self.compose().running(services) if services else {},
            "policy": {
                "allowed": policy.get("allowed") or {},
                "kata": policy.get("kata") or [],
                "has_kata_helper": bool(policy.get("kata_helper")),
                "has_database_tools": bool(policy.get("database_tools")),
                "tunnel_authorized_keys": sorted(policy.get("tunnel_authorized_keys") or {}),
            },
            "last_seq": state.get("last_seq", 0),
            "secret_ids": _secret_ids(self.store.current_env()),
        }


def _secret_ids(env_text: str) -> dict[str, str]:
    from deploy.agent.rotation import secret_ids

    return secret_ids(env_text)


__all__ = ["LEGACY_NAMES", "Executor", "OperationFailed", "Outcome"]
