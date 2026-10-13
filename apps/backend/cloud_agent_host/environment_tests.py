"""Execute isolated environment tests without an Agent CLI or inference grant."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

import httpx

from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec, RetainedChatBackend
from cloud_agent_host.environment_preparation import EnvironmentPreparation
from cloud_agent_host.setup import run_setup
from cloud_agent_shared.environment import ENVIRONMENT_CAPABILITY, BoundEnvironment
from cloud_agent_shared.secret_redaction import SecretRedactor

ENVIRONMENT_TEST_LABEL = "hybridinference.environment-test"


def claim_and_execute(
    *,
    base_url: str,
    credential: str,
    backend: Any,
    stop: threading.Event | None = None,
    cleanup_orphans: bool = True,
) -> bool:
    """Claim and clean up one test using a normal registered host slot."""
    from cloud_agent_host.runner import _claim_attestation

    with httpx.Client(
        base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {credential}"}, timeout=30
    ) as client:
        if cleanup_orphans:
            orphan_response = client.get("/v1/agent/worker/environment-runs/orphans")
            orphan_response.raise_for_status()
            manager = ChatSandboxManager(backend)
            for orphan in orphan_response.json().get("runs", []):
                spec = ChatSandboxSpec(
                    thread_id=f"athr_env_{orphan['id']}",
                    sandbox_id=f"aenv_{orphan['id']}",
                    generation=int(orphan["lease_generation"]),
                    runtime_id="claude-code",
                    sandbox_image_digest=orphan.get("image_digest"),
                    labels={ENVIRONMENT_TEST_LABEL: orphan["id"]},
                )
                inspection = manager.inspect(spec.container_name)
                if inspection is not None:
                    manager.remove_environment_test(spec, container_id=inspection.container_id)
                result = client.post(
                    f"/v1/agent/worker/environment-runs/{orphan['id']}",
                    json={
                        "generation": orphan["lease_generation"],
                        "state": orphan["state"],
                        "cleanup_complete": True,
                    },
                )
                result.raise_for_status()
        if stop is not None and stop.is_set():
            return False
        response = client.post(
            "/v1/agent/worker/environment-runs/claim",
            json={
                "capabilities": [ENVIRONMENT_CAPABILITY],
                "attestation": _claim_attestation(backend),
            },
        )
        response.raise_for_status()
        run = response.json()
        if not run:
            return False
        execute_test(client=client, run=run, backend=backend, stop=stop)
        return True


def execute_test(
    *, client: Any, run: dict[str, Any], backend: Any, stop: threading.Event | None = None
) -> None:
    """Prepare exact source, install/start/verify, then remove the ephemeral group."""
    from cloud_agent_host.runner import ClaimedJob, _SourcePrefetch

    bound = BoundEnvironment.model_validate(run["environment"])
    redactor = SecretRedactor(bound.secrets())
    manager = ChatSandboxManager(backend)
    spec = ChatSandboxSpec(
        thread_id=f"athr_env_{run['id']}",
        sandbox_id=f"aenv_{run['id']}",
        generation=int(run["lease_generation"]),
        runtime_id="claude-code",
        sandbox_image_digest=backend.version_stamp().get("sandbox_image_digest"),
        labels={ENVIRONMENT_TEST_LABEL: run["id"]},
    )
    cancelled = stop or threading.Event()
    heartbeat_stop = threading.Event()
    steps: list[dict[str, Any]] = []
    data_lock = threading.Lock()
    path = f"/v1/agent/worker/environment-runs/{run['id']}"
    began = time.monotonic()

    def report(state: str = "running", detail: str = "", *, cleanup_complete: bool = False) -> None:
        with data_lock:
            payload = {
                "generation": int(run["lease_generation"]),
                "state": state,
                "steps": [dict(s) for s in steps],
                "detail": redactor.text(detail)[-1000:],
                "image_digest": spec.sandbox_image_digest,
                "cleanup_complete": cleanup_complete,
            }
        answer = client.post(path, json=payload)
        if answer.status_code == 409:
            cancelled.set()
        answer.raise_for_status()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(10):
            if time.monotonic() - began >= 1800:
                cancelled.set()
            try:
                report()
            except (httpx.HTTPError, ValueError):
                cancelled.set()
                return

    beat = threading.Thread(target=heartbeat, name="environment-test-heartbeat")
    beat.start()
    container_id = None
    source = None
    outcome = "failed"
    detail = "Environment test interrupted"
    try:
        if cancelled.is_set():
            raise RuntimeError("Environment test cancelled")
        manager.create(spec)
        inspection = manager.start(spec)
        container_id = inspection.container_id
        job = ClaimedJob(
            job_id=run["id"],
            thread_id=spec.thread_id,
            attempt_id=0,
            attempt_no=1,
            repo=run["repo"],
            base_sha=run["base_sha"],
            task_prompt="",
            runtime="claude-code",
            model="",
            worker_token="",
            sandbox_token="",
            clone_token=run.get("clone_token"),
        )
        source = _SourcePrefetch(job, uid=backend.uid, gid=backend.gid, max_bytes=None)
        source.import_into(manager, spec)
        manager.prepare_shared_workspace(spec)
        retained = RetainedChatBackend(manager, spec)
        last_upload = 0.0

        def on_step(step: dict[str, Any]) -> None:
            nonlocal last_upload
            with data_lock:
                prior = next((i for i, s in enumerate(steps) if s["phase"] == step["phase"]), None)
                if prior is None:
                    steps.append(dict(step))
                else:
                    steps[prior] = dict(step)
            now = time.monotonic()
            if heartbeat_stop.is_set():
                return
            if step["state"] != "running" or now - last_upload >= 0.5:
                report()
                last_upload = now

        EnvironmentPreparation().prepare(retained, bound, callback=on_step, cancel=cancelled)
        if bound.verify_script:
            step: dict[str, Any] = {"phase": "verify", "state": "running", "output": ""}

            def output(chunk: str) -> None:
                step["output"] = (str(step["output"]) + chunk)[-65536:]
                on_step(step)

            started = time.monotonic()
            result = run_setup(
                script=bound.verify_script,
                workdir="/workspace",
                backend=retained,
                env=bound.values("runtime"),
                secrets=bound.secrets(),
                timeout_s=300,
                on_output=output,
                cancel=cancelled,
                phase="verify",
            )
            step.update(
                state="succeeded" if result.exit_code == 0 else "failed",
                exit_code=result.exit_code,
                duration_seconds=time.monotonic() - started,
            )
            on_step(step)
            if result.exit_code:
                raise RuntimeError(f"Verification failed ({result.exit_code})")
        if cancelled.is_set():
            raise RuntimeError("Environment test cancelled")
        outcome, detail = "succeeded", ""
    except Exception as exc:
        outcome, detail = ("cancelled" if cancelled.is_set() else "failed"), str(exc)
    finally:
        heartbeat_stop.set()
        beat.join(timeout=35)
        if source is not None:
            source.discard()
        cleanup_inspection = manager.inspect(spec.container_name)
        if cleanup_inspection is not None:
            container_id = cleanup_inspection.container_id
            try:
                manager.remove_environment_test(spec, container_id=container_id)
            except Exception:
                with contextlib.suppress(httpx.HTTPError):
                    report("running", "Environment cleanup is pending")
                return
        if container_id:
            key = __import__("hashlib").sha256(container_id.encode()).hexdigest()
            root = EnvironmentPreparation().root
            for suffix in (".json", ".lock"):
                with contextlib.suppress(FileNotFoundError):
                    (root / f"{key}{suffix}").unlink()

        with contextlib.suppress(httpx.HTTPError):
            report(outcome, detail, cleanup_complete=True)


class EnvironmentTestWorker:
    """Keep test execution and orphan RPCs off the host's coding-task claim loop."""

    def __init__(self, base_url: str, backend: Any) -> None:
        self._base_url = base_url
        self._backend = backend
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_poll = 0.0
        self._next_cleanup = 0.0

    @property
    def busy(self) -> bool:
        """Count an in-flight claim or test as one reserved local slot."""
        return self._thread is not None and self._thread.is_alive()

    def poll(self, credential: str) -> None:
        """Start at most one bounded poll without waiting for its HTTP or script work."""
        if self._stop.is_set() or self.busy or time.monotonic() < self._next_poll:
            return
        self._next_poll = time.monotonic() + 5
        cleanup = time.monotonic() >= self._next_cleanup
        if cleanup:
            self._next_cleanup = time.monotonic() + 60

        def execute() -> None:
            try:
                claim_and_execute(
                    base_url=self._base_url,
                    credential=credential,
                    backend=self._backend,
                    stop=self._stop,
                    cleanup_orphans=cleanup,
                )
            except Exception:
                logging.getLogger(__name__).warning("environment_test_poll_failed")

        self._thread = threading.Thread(target=execute, name="environment-test", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Cancel in-flight preparation and allow its fenced cleanup to finish."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=45)
