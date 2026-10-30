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
* ``tunnel_keys``: on a host, ``generate`` a key beside each tunnel key (the
  private half stays here), ``switch`` after proving the far end accepts it,
  restored if the tunnels do not come back, and ``finish``. On a control-plane
  machine, ``authorize`` and ``revoke`` edit only the ``authorized_keys`` file
  and restrictions local policy names for that tunnel.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deploy.agent.deployment_contract import key_identifier
from deploy.agent.operations import OperationFailed, Outcome
from deploy.agent.store import parse_env, render_env, write_private

if TYPE_CHECKING:
    from deploy.agent.operations import Executor

_ROOT = "AGENT_SETTINGS_ENCRYPTION_KEY"
_ROOT_PREVIOUS = "AGENT_SETTINGS_ENCRYPTION_KEY_PREVIOUS"
_TOKEN = "AGENT_SANDBOX_MANAGER_TOKEN"
_TOKEN_NEXT = "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"
_CP_SERVICES = ["control-plane", "mcp-relay"]
_HOST_SERVICES = ["sandbox-manager", "runner"]
_TUNNELS = {
    "gateway": "AGENT_TUNNEL",
    "mcp": "AGENT_MCP_TUNNEL",
    "control": "AGENT_CONTROL_PLANE_TUNNEL",
}
_TUNNEL_RESTART = [
    "gateway-tunnel",
    "mcp-tunnel",
    "control-plane-tunnel",
    "sandbox-manager",
    "runner",
]


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


def _tunnel_paths(env: dict[str, str]) -> dict[str, Path]:
    """Each configured tunnel's private key file, as this machine's ``.env`` names it."""
    paths = {}
    for tunnel, prefix in _TUNNELS.items():
        value = env.get(f"{prefix}_SSH_KEY", "")
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise OperationFailed(f"the {tunnel} tunnel's key is not a regular file")
        paths[tunnel] = path
    if not paths:
        raise OperationFailed("this machine's .env names no tunnel keys")
    return paths


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _keygen(executor: Executor, *arguments: str) -> str:
    result = executor.runner.run(["ssh-keygen", *arguments], cwd=executor.store.deploy_root)
    if not result.ok:
        raise OperationFailed(f"ssh-keygen failed (exit {result.code})")
    return result.output.strip()


def _public_key(line: str) -> str:
    """``ssh-ed25519 <base64>`` without a comment."""
    return " ".join(line.split()[:2])


def _tunnel_generate(executor: Executor, env: dict[str, str]) -> Outcome:
    """Make a new key beside each current one; the private half never leaves this machine."""
    fresh, current = {}, {}
    for tunnel, path in _tunnel_paths(env).items():
        pending = _sibling(path, ".next")
        if not pending.exists():
            _keygen(
                executor,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "cloud-agent-tunnel",
                "-f",
                str(pending),
            )
        fresh[tunnel] = _public_key(_keygen(executor, "-y", "-f", str(pending)))
        current[tunnel] = _public_key(_keygen(executor, "-y", "-f", str(path)))
    return Outcome(
        "succeeded", "new tunnel keys generated", {"public_keys": {"new": fresh, "old": current}}
    )


def _authenticates(executor: Executor, env: dict[str, str], tunnel: str, key: Path) -> bool:
    """Whether the far end accepts ``key``: ssh exits 255 for its own failures only."""
    prefix = _TUNNELS[tunnel]
    result = executor.runner.run(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={env.get(f'{prefix}_SSH_KNOWN_HOSTS', '/dev/null')}",
            "-o",
            "ConnectTimeout=15",
            "-p",
            env.get(f"{prefix}_SSH_PORT") or "22",
            env.get(f"{prefix}_SSH_DESTINATION", ""),
            "true",
        ],
        cwd=executor.store.deploy_root,
        timeout=60.0,
    )
    return result.code != 255


def _tunnel_switch(executor: Executor, env: dict[str, str]) -> Outcome:
    """Prove each new key, swap it in, restart; put the old keys back if anything fails."""
    paths = _tunnel_paths(env)
    if not all(_sibling(path, ".next").exists() for path in paths.values()):
        if all(_sibling(path, ".previous").exists() for path in paths.values()):
            return Outcome("succeeded", "already switched", {})
        raise OperationFailed("generate the new tunnel keys first")
    refused = [
        t for t, p in paths.items() if not _authenticates(executor, env, t, _sibling(p, ".next"))
    ]
    if refused:
        raise OperationFailed("the far end refuses the new key of: " + ", ".join(refused))
    for path in paths.values():
        shutil.copy2(path, _sibling(path, ".previous"))
        os.replace(_sibling(path, ".next"), path)
    healthy, states = executor._restart_and_check(_TUNNEL_RESTART)
    if healthy:
        return Outcome("succeeded", "the tunnels run on the new keys", {"services": states})
    for path in paths.values():
        shutil.copy2(path, _sibling(path, ".next"))
        os.replace(_sibling(path, ".previous"), path)
    restored, restored_states = executor._restart_and_check(_TUNNEL_RESTART)
    return Outcome(
        "failed",
        "the tunnels did not come back on the new keys; the previous keys were restored"
        + ("" if restored else " but they are not healthy either"),
        {"services": restored_states},
    )


def _tunnel_finish(env: dict[str, str]) -> Outcome:
    for path in _tunnel_paths(env).values():
        for suffix in (".previous", ".next.pub"):
            _sibling(path, suffix).unlink(missing_ok=True)
    return Outcome("succeeded", "the previous tunnel keys are deleted", {})


def _authorized_keys(executor: Executor, tunnel: str) -> tuple[Path, str]:
    """The file and restrictions this machine's policy names for ``tunnel``; never a request's."""
    entry = (executor.store.policy().get("tunnel_authorized_keys") or {}).get(tunnel) or {}
    path, options = str(entry.get("path") or ""), str(entry.get("options") or "")
    if not path.startswith("/") or "restrict" not in options:
        raise OperationFailed(f"this machine's policy does not manage the {tunnel} tunnel")
    return Path(path), options


def _tunnel_far_end(executor: Executor, step: str, params: dict[str, Any]) -> Outcome:
    """Add or remove public keys in a file local policy names, under its fixed restrictions."""
    host = str(params.get("host") or "")
    for tunnel, public in (params.get("tunnels") or {}).items():
        path, options = _authorized_keys(executor, tunnel)
        lines = path.read_text().splitlines() if path.exists() else []
        blob = public.split()[1]
        kept = [line for line in lines if blob not in line.split()]
        if step == "authorize":
            kept.append(f"{options} {public} cloud-agent-tunnel:{host}:{tunnel}")
        if kept != lines:
            write_private(path, ("\n".join(kept) + "\n").encode())
    return Outcome("succeeded", f"tunnel keys {step}d on this machine", {})


def _tunnel_step(executor: Executor, step: str, params: dict[str, Any]) -> Outcome:
    if step in {"authorize", "revoke"}:
        if executor.role != "control-plane":
            raise OperationFailed("the far end of a tunnel is a control-plane machine")
        return _tunnel_far_end(executor, step, params)
    if executor.role != "host":
        raise OperationFailed("tunnel keys live on hosts")
    env = parse_env(executor.store.current_env())
    if step == "generate":
        return _tunnel_generate(executor, env)
    if step == "switch":
        return _tunnel_switch(executor, env)
    if step == "finish":
        return _tunnel_finish(env)
    raise OperationFailed("unknown tunnel key step")


def rotation_step(
    executor: Executor, params: dict[str, Any], sealed: dict[str, Any], _record_id: str
) -> Outcome:
    """Run one rotation step named by ``params['workflow']`` and ``params['step']``."""
    workflow, step = params["workflow"], params["step"]
    if workflow == "root_key":
        return _root_step(executor, step, sealed)
    if workflow == "sandbox_manager_token":
        return _token_step(executor, step, sealed)
    if workflow == "tunnel_keys":
        return _tunnel_step(executor, step, params)
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
