"""Serialize retained environment preparation without persisting user values."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.enrollment import credential_path
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.setup import run_setup
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from pathlib import Path

    from cloud_agent_host.chat_sandbox import RetainedChatBackend
    from cloud_agent_shared.environment import BoundEnvironment


class EnvironmentPreparation:
    """One host-private receipt per exact container and frozen binding."""

    def __init__(self, root: Path | None = None) -> None:
        """Keep receipts outside all container mounts and source archives."""
        self.root = root or credential_path().parent / "environment-preparation"

    def prepare(
        self,
        backend: RetainedChatBackend,
        environment: BoundEnvironment,
        *,
        callback: Callable[[dict[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run installation once and startup once per actual Docker boot."""
        inspection = backend.manager.inspect(backend.spec.container_name)
        if inspection is None or not inspection.running:
            raise SandboxError("Environment container is not running")
        container_id = inspection.container_id
        boot_id = inspection.started_at
        if not boot_id:
            raise SandboxError("Container boot identity is unavailable")
        key = hashlib.sha256(container_id.encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / f"{key}.json"
        lock_path = self.root / f"{key}.lock"
        with lock_path.open("a+") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            receipt = json.loads(path.read_text()) if path.exists() else {}
            if receipt and receipt.get("binding_id") != environment.binding_id:
                raise SandboxError("Workspace is already bound to another environment")
            receipt.setdefault("binding_id", environment.binding_id)
            receipt.setdefault("container_id", container_id)
            receipt.setdefault("steps", [])
            redactor = SecretRedactor(environment.secrets())
            for phase, script, timeout in (
                ("setup", environment.setup_script, 900),
                ("start", environment.start_script, 120),
            ):
                done = (
                    receipt.get("setup_done")
                    if phase == "setup"
                    else receipt.get("start_done") and receipt.get("boot_id") == boot_id
                )
                if done:
                    continue
                step: dict[str, Any] = {
                    "phase": phase,
                    "state": "running",
                    "output": "",
                    "duration_seconds": 0,
                }
                receipt["steps"] = [s for s in receipt["steps"] if s.get("phase") != phase] + [step]

                self._save(path, receipt)
                last_saved = [0.0]

                def output(
                    chunk: str, step: dict[str, Any] = step, last_saved: list[float] = last_saved
                ) -> None:
                    step["output"] = (step["output"] + chunk)[-65536:]
                    if time.monotonic() - last_saved[0] > 0.5:
                        self._save(path, receipt)
                        last_saved[0] = time.monotonic()
                    if callback:
                        callback(dict(step))

                started = time.monotonic()
                result = run_setup(
                    script=script,
                    workdir="/workspace",
                    backend=backend,
                    timeout_s=timeout,
                    env=environment.values("setup" if phase == "setup" else "runtime"),
                    secrets=environment.secrets(),
                    on_output=output,
                    cancel=cancel,
                    cleanup_background=phase == "setup",
                    phase=phase,
                )
                step.update(
                    state="succeeded" if result.exit_code == 0 else "failed",
                    exit_code=result.exit_code,
                    duration_seconds=time.monotonic() - started,
                    ran=result.ran,
                    detail=redactor.text(result.detail),
                )
                receipt[phase + "_done"] = result.exit_code == 0
                receipt["boot_id"] = boot_id
                self._save(path, receipt)
                if callback:
                    callback(dict(step))
                if result.exit_code:
                    raise SandboxError(
                        f"Environment {phase} failed ({result.exit_code}): {step['detail']}"
                    )
            return {
                "binding_id": environment.binding_id,
                "container_id": container_id,
                "boot_id": boot_id,
                "steps": receipt["steps"],
            }

    def status(self, backend: RetainedChatBackend, binding_id: str) -> dict[str, Any]:
        """Read a masked receipt for this exact running container only."""
        inspection = backend.manager.inspect(backend.spec.container_name)
        if inspection is None:
            raise SandboxError("Environment container is unavailable")
        key = hashlib.sha256(inspection.container_id.encode()).hexdigest()
        path = self.root / f"{key}.json"
        receipt = json.loads(path.read_text()) if path.exists() else {}
        if receipt and receipt.get("binding_id") != binding_id:
            raise SandboxError("Workspace binding does not match preparation")
        return {
            **receipt,
            "ready": bool(
                receipt.get("setup_done")
                and receipt.get("start_done")
                and receipt.get("boot_id") == inspection.started_at
            ),
        }

    @staticmethod
    def _save(path: Path, receipt: dict[str, Any]) -> None:
        temporary = path.with_suffix(f".{os.urandom(8).hex()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(receipt, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
