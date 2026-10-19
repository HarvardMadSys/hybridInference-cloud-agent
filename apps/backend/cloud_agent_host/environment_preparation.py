"""Serialize retained environment preparation without persisting user values."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from typing import TYPE_CHECKING, Any

from cloud_agent_host.enrollment import credential_path
from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.setup import run_setup
from cloud_agent_shared.secret_redaction import SecretRedactor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cloud_agent_host.chat_sandbox import RetainedChatBackend
    from cloud_agent_host.setup import SetupResult
    from cloud_agent_shared.environment import BoundEnvironment


def step_state(result: SetupResult, cancel: threading.Event | None) -> str:
    """Name the state an environment script's finished step is recorded in.

    Cancelling the preparation or test run kills its script, which run_setup
    then reports as stopped (130): the step was cancelled. A timeout, and any
    nonzero exit of the script's own, 130 included, failed.
    """
    if result.exit_code == 0:
        return "succeeded"
    if result.exit_code == 130 and cancel is not None and cancel.is_set():
        return "cancelled"
    return "failed"


def write_setup_marker(
    write: Callable[..., None],
    spec: Any,
    container_id: str,
    *,
    setup_fingerprint: str,
    binding_id: str,
    snapshot_id: str | None = None,
) -> None:
    """Write the root-only "installation done" marker through ``write``."""
    payload = json.dumps(
        {
            "v": 1,
            "setup_fingerprint": setup_fingerprint,
            "binding_id": binding_id,
            **({"snapshot_id": snapshot_id} if snapshot_id else {}),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    write(spec, payload, expected_container_id=container_id)


class EnvironmentPreparation:
    """One host-private receipt per exact container and frozen binding."""

    def __init__(self, root: Path | None = None) -> None:
        """Keep receipts outside all container mounts and source archives."""
        self.root = root or credential_path().parent / "environment-preparation"
        self._inspections: dict[str, tuple[float, Any]] = {}
        self._inspection_lock = threading.Lock()

    def prepare(
        self,
        backend: RetainedChatBackend,
        environment: BoundEnvironment,
        *,
        callback: Callable[[dict[str, Any]], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run installation once and startup once per actual Docker boot."""
        if hasattr(backend, "spawn_environment_script"):
            from cloud_agent_host.environment_process import prepare_tool_home

            # Create shared package directories even when both scripts are empty.
            prepare_tool_home(backend.manager, backend.spec, 30000)
        inspection = backend.manager.inspect(backend.spec.container_name)
        if inspection is None or not inspection.running:
            raise SandboxError("Environment container is not running")
        with self._inspection_lock:
            self._inspections[backend.spec.container_name] = (time.monotonic() + 15, inspection)
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
            if not receipt.get("setup_done") and environment.setup_fingerprint:
                self._adopt_installation(backend, environment, receipt, container_id, callback)
                self._save(path, receipt)
            redactor = SecretRedactor(environment.secrets())
            for phase, script, timeout in (
                ("setup", environment.setup_script, 900),
                ("update", environment.update_script, 900),
                ("start", environment.start_script, 120),
            ):
                if phase == "update":
                    done = not receipt.get("update_pending")
                elif phase == "setup":
                    done = bool(receipt.get("setup_done"))
                else:
                    done = bool(receipt.get("start_done") and receipt.get("boot_id") == boot_id)
                if done:
                    continue
                if phase == "start" and environment.skip_start:
                    skipped = {
                        "phase": "start",
                        "state": "skipped",
                        "output": "",
                        "detail": "Startup explicitly skipped for this turn",
                    }
                    receipt["steps"] = [
                        s for s in receipt["steps"] if s.get("phase") != "start"
                    ] + [skipped]
                    self._save(path, receipt)
                    if callback:
                        callback(dict(skipped))
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
                    env=environment.values("runtime" if phase == "start" else "setup"),
                    secrets=environment.secrets(),
                    on_output=output,
                    cancel=cancel,
                    cleanup_background=phase != "start",
                    phase=phase,
                )
                step.update(
                    state=step_state(result, cancel),
                    exit_code=result.exit_code,
                    duration_seconds=time.monotonic() - started,
                    ran=result.ran,
                    detail=redactor.text(result.detail),
                )
                receipt[phase + "_done"] = result.exit_code == 0
                receipt["boot_id"] = boot_id
                if phase == "update" and result.exit_code == 0:
                    receipt["update_pending"] = False
                if phase in ("setup", "update") and result.exit_code == 0:
                    self._mark_installed(backend, environment, container_id)
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

    @staticmethod
    def _adopt_installation(
        backend: RetainedChatBackend,
        environment: BoundEnvironment,
        receipt: dict[str, Any],
        container_id: str,
        callback: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """Count an installation this container's layer already holds as done.

        This host has no record of the container: it was just created from an
        installation snapshot, or restored from a checkpoint here. The
        root-only marker says which installation the layer holds; the same
        fingerprint means the same script and setup values. A snapshot was
        built for another workspace, so its update script still runs here.
        """
        read = getattr(backend.manager, "read_environment_setup_marker", None)
        if read is None:
            return
        try:
            raw = read(backend.spec, expected_container_id=container_id)
            marker = json.loads(raw) if raw else None
        except (SandboxError, ValueError):
            return
        if (
            not isinstance(marker, dict)
            or marker.get("v") != 1
            or marker.get("setup_fingerprint") != environment.setup_fingerprint
        ):
            return
        from_snapshot = marker.get("binding_id") != environment.binding_id
        snapshot_id = marker.get("snapshot_id") if from_snapshot else None
        step: dict[str, Any] = {
            "phase": "setup",
            "state": "succeeded",
            "output": "",
            "duration_seconds": 0,
            "ran": False,
            "detail": (
                "Installed from a snapshot" if from_snapshot else "Installed before a restore"
            ),
            **({"snapshot_id": snapshot_id} if isinstance(snapshot_id, str) else {}),
        }
        receipt["steps"] = [s for s in receipt["steps"] if s.get("phase") != "setup"] + [step]
        receipt["setup_done"] = True
        receipt["update_pending"] = from_snapshot
        if callback:
            callback(dict(step))

    @staticmethod
    def _mark_installed(
        backend: RetainedChatBackend, environment: BoundEnvironment, container_id: str
    ) -> None:
        """Record in the layer that this binding's installation is complete."""
        write = getattr(backend.manager, "write_environment_setup_marker", None)
        if write is None or not environment.setup_fingerprint:
            return
        write_setup_marker(
            write,
            backend.spec,
            container_id,
            setup_fingerprint=environment.setup_fingerprint,
            binding_id=environment.binding_id,
        )

    def status(self, backend: RetainedChatBackend, binding_id: str) -> dict[str, Any]:
        """Read a masked receipt for this exact running container only."""
        with self._inspection_lock:
            name = backend.spec.container_name
            cached = self._inspections.get(name)
            if cached is not None and cached[0] > time.monotonic():
                inspection = cached[1]
            else:
                inspection = backend.manager.inspect(name)
                if len(self._inspections) >= 256:
                    self._inspections.clear()
                self._inspections[name] = (time.monotonic() + 15, inspection)
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
                and not receipt.get("update_pending")
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
