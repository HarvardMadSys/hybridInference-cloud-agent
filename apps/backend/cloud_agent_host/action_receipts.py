"""Host-private durable tombstones and masked results for manual commands."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from cloud_agent_host.enrollment import credential_path
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.sandbox import SandboxError


class ActionReceipts:
    """Reserve before spawning; keep results after terminal eviction and broker restart."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or credential_path().parent / "environment-actions"

    def _path(self, workspace_id: str, terminal_id: str) -> Path:
        key = hashlib.sha256(json.dumps([workspace_id, terminal_id]).encode()).hexdigest()
        return self.root / f"{key}.json"

    def read(self, workspace_id: str, terminal_id: str) -> dict[str, Any] | None:
        """Return a terminal result, explicitly marking an interrupted launch as unknown."""
        path = self._path(workspace_id, terminal_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        result = value["result"]
        if result["state"] in {"starting", "running", "paused", "closing", "cleanup_failed"}:
            return {
                **result,
                "state": "unknown",
                "exit_code": None,
                "output": "Execution result is unavailable. This run will not be replayed.",
            }
        return dict(result)

    def reserve(
        self, workspace_id: str, terminal_id: str, binding_id: str, action_id: str
    ) -> dict[str, Any] | None:
        """Persist execution identity before any command can produce side effects."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(workspace_id, terminal_id)
        with path.with_suffix(".lock").open("a+") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                saved = json.loads(path.read_text())
                if saved["binding_id"] is not None and (
                    saved["binding_id"] != binding_id or saved["action_id"] != action_id
                ):
                    raise SandboxError("Action run identity conflicts with its receipt")
                return self.read(workspace_id, terminal_id)
            self._save(
                path,
                {
                    "binding_id": binding_id,
                    "action_id": action_id,
                    "result": {
                        "id": terminal_id,
                        "state": "starting",
                        "output": "",
                        "exit_code": None,
                        "shell": "sh",
                        "cwd": "/workspace",
                        "rows": 24,
                        "cols": 100,
                        "last_seq": 0,
                    },
                },
            )
        return None

    def finish(self, workspace_id: str, terminal_id: str, result: dict[str, Any]) -> None:
        """Keep the first final result; reaping must not overwrite a successful exit."""
        path = self._path(workspace_id, terminal_id)
        with path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            saved = json.loads(path.read_text())
            if saved["result"]["state"] in {"exited", "closed", "failed", "unknown"}:
                return
            saved["result"] = result
            self._save(path, saved)

    @staticmethod
    def _save(path: Path, receipt: dict[str, Any]) -> None:
        EnvironmentPreparation._save(path, receipt)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def stop(self, workspace_id: str, terminal_id: str) -> None:
        """Fence an action stopped before its delayed create request reaches the broker."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(workspace_id, terminal_id)
        with path.with_suffix(".lock").open("a+") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                return
            self._save(
                path,
                {
                    "binding_id": None,
                    "action_id": None,
                    "result": {
                        "id": terminal_id,
                        "state": "closed",
                        "exit_code": None,
                        "output": "Stopped before launch",
                        "shell": "sh",
                        "cwd": "/workspace",
                        "rows": 24,
                        "cols": 100,
                        "last_seq": 0,
                    },
                },
            )
