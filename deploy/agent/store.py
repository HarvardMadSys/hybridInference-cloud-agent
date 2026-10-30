"""The deployment agent's store: ``<DEPLOY_ROOT>/agent/`` beside the deployment's ``.env``.

Everything the agent needs to act without the control plane lives here: its
identity and credential, the key sealed values are opened with, the machine's
local policy, every applied configuration version, the verified release lock
and the operation journal. Files holding secrets are written ``0600``; every
write goes to a temporary file in the same directory, is flushed and renamed,
so a crash leaves either the old file or the new one.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

#: How many configuration versions are kept for rollback.
KEEP_VERSIONS = 20
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class StoreError(RuntimeError):
    """The store is missing, damaged, or asked for something it does not hold."""


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically replace ``path`` with ``data``, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    with contextlib.suppress(OSError):
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    """Atomically write ``value`` as JSON."""
    write_private(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(), mode)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, or ``default`` when the file does not exist."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise StoreError(f"{path.name} is not valid JSON") from exc


def parse_env(text: str) -> dict[str, str]:
    """The ``KEY=value`` assignments of a dotenv file, in order; quotes kept as written."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def render_env(previous_text: str, changes: dict[str, str]) -> str:
    """``previous_text`` with ``changes`` applied, every other line kept byte for byte.

    A changed key keeps its position; a new key is appended. Values are
    written bare: the contract refuses newlines and NUL, and Compose's dotenv
    parser takes the rest of the line literally.
    """
    pending = dict(changes)
    lines = previous_text.splitlines()
    out: list[str] = []
    for line in lines:
        match = _ENV_LINE.match(line.strip())
        if match and match.group(1) in pending:
            name = match.group(1)
            out.append(f"{name}={pending.pop(name)}")
        else:
            out.append(line)
    out.extend(f"{name}={value}" for name, value in pending.items())
    return "\n".join(out) + "\n"


@dataclass
class AgentStore:
    """One machine's deployment root and the agent directory inside it."""

    deploy_root: Path

    @property
    def root(self) -> Path:
        """The agent's own directory."""
        return self.deploy_root / "agent"

    @property
    def env_path(self) -> Path:
        """The deployment's ``.env``: always a copy of the current configuration version."""
        return self.deploy_root / ".env"

    # -- identity -----------------------------------------------------------

    def exists(self) -> bool:
        """Whether ``init`` has run here."""
        return (self.root / "identity.json").is_file()

    def identity(self) -> dict[str, Any]:
        """The agent's identity; raises before ``init``."""
        identity = read_json(self.root / "identity.json")
        if not isinstance(identity, dict):
            raise StoreError("this machine has no deployment agent; run init first")
        return identity

    def save_identity(self, identity: dict[str, Any]) -> None:
        """Replace the identity file."""
        write_json(self.root / "identity.json", identity, 0o644)

    def private_key(self) -> bytes:
        """The key sealed values are opened with."""
        try:
            return (self.root / "key.pem").read_bytes()
        except FileNotFoundError as exc:
            raise StoreError("the agent key is missing") from exc

    def save_private_key(self, pem: bytes) -> None:
        """Store the key, ``0600``."""
        write_private(self.root / "key.pem", pem)

    def credential(self) -> str | None:
        """The control-plane credential, once registered."""
        try:
            return (self.root / "credential").read_text().strip() or None
        except FileNotFoundError:
            return None

    def save_credential(self, credential: str) -> None:
        """Store the credential, ``0600``."""
        write_private(self.root / "credential", (credential + "\n").encode())

    # -- policy -------------------------------------------------------------

    def policy(self) -> dict[str, Any]:
        """This machine's local policy. Never written by the agent's network path."""
        policy = read_json(self.root / "policy.json", {})
        if not isinstance(policy, dict):
            raise StoreError("policy.json must be an object")
        return policy

    # -- state and journal --------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The agent's progress: last intent sequence and current version."""
        state = read_json(self.root / "state.json", {})
        return state if isinstance(state, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        """Atomically replace the progress record."""
        write_json(self.root / "state.json", state)

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        """One journal entry."""
        _check_id(operation_id)
        record = read_json(self.root / "operations" / f"{operation_id}.json")
        return record if isinstance(record, dict) else None

    def save_operation(self, record: dict[str, Any]) -> None:
        """Write one journal entry; ``updated_at`` is set here."""
        _check_id(record["id"])
        record["updated_at"] = time.time()
        write_json(self.root / "operations" / f"{record['id']}.json", record)

    def operations(self) -> list[dict[str, Any]]:
        """Every journal entry, oldest first."""
        directory = self.root / "operations"
        if not directory.is_dir():
            return []
        records = [read_json(path) for path in sorted(directory.glob("*.json"))]
        return sorted(
            (r for r in records if isinstance(r, dict)), key=lambda r: r.get("created_at", 0)
        )

    # -- configuration versions --------------------------------------------

    def current_env(self) -> str:
        """The deployment's current ``.env`` text, empty when there is none."""
        try:
            return self.env_path.read_text()
        except FileNotFoundError:
            return ""

    def versions(self) -> list[int]:
        """Stored configuration versions, ascending."""
        directory = self.root / "config"
        if not directory.is_dir():
            return []
        return sorted(int(p.stem) for p in directory.glob("*.env") if p.stem.isdigit())

    def version_text(self, rev: int) -> str:
        """One stored version's full text."""
        path = self.root / "config" / f"{int(rev)}.env"
        try:
            return path.read_text()
        except FileNotFoundError as exc:
            raise StoreError(f"configuration version {rev} is not stored here") from exc

    def adopt_existing(self) -> int:
        """Record the deployment's current ``.env`` as version 0 when nothing is stored."""
        if self.versions():
            return int(self.state().get("config_rev", self.versions()[-1]))
        write_private(self.root / "config" / "0.env", self.current_env().encode())
        state = self.state()
        state["config_rev"] = 0
        self.save_state(state)
        return 0

    def stage_version(self, text: str) -> int:
        """Store a new version (not yet active) and return its number."""
        versions = self.versions()
        rev = (versions[-1] + 1) if versions else 0
        write_private(self.root / "config" / f"{rev}.env", text.encode())
        for old in versions[: max(0, len(versions) + 1 - KEEP_VERSIONS)]:
            if old != int(self.state().get("config_rev", -1)):
                with contextlib.suppress(FileNotFoundError):
                    (self.root / "config" / f"{old}.env").unlink()
        return rev

    def activate_version(self, rev: int) -> None:
        """Make a stored version the deployment's ``.env``."""
        write_private(self.env_path, self.version_text(rev).encode())
        state = self.state()
        state["config_rev"] = int(rev)
        self.save_state(state)

    # -- releases -----------------------------------------------------------

    def release_dir(self, sha256: str) -> Path:
        """Where one imported release lives."""
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise StoreError("a release is named by its manifest SHA-256")
        return self.root / "releases" / sha256

    def release_lock(self) -> dict[str, dict[str, Any]]:
        """Imported, verified releases by manifest SHA-256."""
        lock = read_json(self.root / "releases" / "lock.json", {})
        return lock if isinstance(lock, dict) else {}

    def save_release_lock(self, lock: dict[str, dict[str, Any]]) -> None:
        """Replace the release lock."""
        write_json(self.root / "releases" / "lock.json", lock, 0o644)


def _check_id(operation_id: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise StoreError("an operation is named by 32 hexadecimal digits")


__all__ = [
    "KEEP_VERSIONS",
    "AgentStore",
    "StoreError",
    "parse_env",
    "read_json",
    "render_env",
    "write_json",
    "write_private",
]
