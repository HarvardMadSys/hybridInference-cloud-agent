"""Key rotation steps that must happen on this machine, without the key leaving it.

Each step edits this machine's ``.env`` through a new configuration version,
restarts the services that read the key, and keeps the version only if they
come back healthy. The control plane learns the result from one-way key
identifiers in this agent's report (``secret_ids``) and in its own processes'
reports; it never receives a key from here.

* ``root_key/separate``: a dedicated settings root key equal to the current
  ``AGENT_SESSION_SECRET`` fallback, copied locally (same bytes, so nothing
  needs re-encrypting).
* ``root_key/stage``: the new root key (sealed by the browser) becomes the
  key, and the current one ``_PREVIOUS`` for decrypting until re-encryption.
* ``root_key/retire``: drop ``_PREVIOUS`` once nothing is encrypted with it.
* ``sandbox_manager_token/stage``: the next credential, accepted beside the
  current one; ``promote`` makes it the credential and drops the old one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deploy.agent.deployment_contract import key_identifier
from deploy.agent.operations import OperationFailed, Outcome
from deploy.agent.store import parse_env, render_env

if TYPE_CHECKING:
    from deploy.agent.operations import Executor

_ROOT = "AGENT_SETTINGS_ENCRYPTION_KEY"
_ROOT_PREVIOUS = "AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS"
_TOKEN = "AGENT_SANDBOX_MANAGER_TOKEN"
_TOKEN_NEXT = "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"
_CP_SERVICES = ["control-plane", "mcp-relay"]
_HOST_SERVICES = ["sandbox-manager", "runner"]


def _switch(
    executor: Executor, changes: dict[str, str], remove: frozenset[str], services: list[str]
) -> Outcome:
    previous_rev = executor.store.adopt_existing()
    text = render_env(executor.store.current_env(), changes, remove)
    rev = executor.store.stage_version(text)
    executor.store.activate_version(rev)
    healthy, states = executor._restart_and_check(services)
    if healthy:
        return Outcome(
            "succeeded",
            f"configuration version {rev} applied",
            {"config_rev": rev, "services": states},
        )
    executor.store.activate_version(previous_rev)
    restored, restored_states = executor._restart_and_check(services)
    return Outcome(
        "failed",
        f"the services did not come back on version {rev}; restored version {previous_rev}"
        + ("" if restored else " but they are not healthy on it either"),
        {"config_rev": previous_rev, "failed_rev": rev, "services": restored_states},
    )


def _root_step(executor: Executor, step: str, sealed: dict[str, Any]) -> Outcome:
    if executor.role != "control-plane":
        raise OperationFailed("the settings root key lives on control-plane machines")
    env = parse_env(executor.store.current_env())
    dedicated, fallback = env.get(_ROOT, ""), env.get("AGENT_SESSION_SECRET", "")
    if step == "separate":
        if dedicated:
            return Outcome(
                "succeeded",
                "a dedicated root key is already set",
                {"root_key_id": key_identifier(dedicated)},
            )
        if len(fallback.encode()) < 32:
            raise OperationFailed("there is no fallback root key of at least 32 bytes to separate")
        return _switch(executor, {_ROOT: fallback}, frozenset(), _CP_SERVICES)
    if step == "stage":
        new = executor.open_values({_ROOT: sealed[_ROOT]})[_ROOT] if _ROOT in sealed else ""
        if len(new.encode()) < 32:
            raise OperationFailed("the new root key must be at least 32 bytes")
        current = dedicated or fallback
        if not current:
            raise OperationFailed("there is no current root key to keep for decrypting")
        if new == current:
            raise OperationFailed("the new root key equals the current one")
        return _switch(executor, {_ROOT: new, _ROOT_PREVIOUS: current}, frozenset(), _CP_SERVICES)
    if step == "retire":
        if _ROOT_PREVIOUS not in env:
            return Outcome("succeeded", "no previous root key is set", {})
        return _switch(executor, {}, frozenset({_ROOT_PREVIOUS}), _CP_SERVICES)
    raise OperationFailed("unknown root key step")


def _token_step(executor: Executor, step: str, sealed: dict[str, Any]) -> Outcome:
    if executor.role != "host":
        raise OperationFailed("the sandbox manager credential rotation runs on hosts")
    env = parse_env(executor.store.current_env())
    if step == "stage":
        new = (
            executor.open_values({_TOKEN_NEXT: sealed[_TOKEN_NEXT]})[_TOKEN_NEXT]
            if _TOKEN_NEXT in sealed
            else ""
        )
        if len(new.encode()) < 32:
            raise OperationFailed("the next credential must be at least 32 bytes")
        return _switch(executor, {_TOKEN_NEXT: new}, frozenset(), _HOST_SERVICES)
    if step == "promote":
        nxt = env.get(_TOKEN_NEXT, "")
        if not nxt:
            if env.get(_TOKEN):
                return Outcome("succeeded", "already promoted", {})
            raise OperationFailed("there is no next credential to promote")
        return _switch(executor, {_TOKEN: nxt}, frozenset({_TOKEN_NEXT}), _HOST_SERVICES)
    raise OperationFailed("unknown sandbox manager credential step")


def rotation_step(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], _record_id: str
) -> Outcome:
    """Run one rotation step named by ``params['workflow']`` and ``params['step']``."""
    workflow, step = params["workflow"], params["step"]
    if workflow == "root_key":
        return _root_step(executor, step, sealed)
    if workflow == "sandbox_manager_token":
        return _token_step(executor, step, sealed)
    raise OperationFailed(f"{workflow} has no step on a deployment agent")


def secret_ids(env_text: str) -> dict[str, str]:
    """One-way identifiers of the keys this machine's ``.env`` holds, for the report."""
    env = parse_env(env_text)
    names = (_ROOT, _ROOT_PREVIOUS, "AGENT_SESSION_SECRET", _TOKEN, _TOKEN_NEXT, "ADMIN_TOKEN")
    return {name: key_identifier(env[name]) for name in names if env.get(name)}


def register(executor: Executor) -> None:
    """Add the rotation handlers to ``executor``."""
    executor.handlers["rotation_step"] = rotation_step


__all__ = ["register", "rotation_step", "secret_ids"]
