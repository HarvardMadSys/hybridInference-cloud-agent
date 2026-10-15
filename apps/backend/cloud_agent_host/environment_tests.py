"""Execute isolated environment tests without an Agent CLI or inference grant."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

import httpx

from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec, RetainedChatBackend
from cloud_agent_host.chat_sandbox_models import ENVIRONMENT_TEST_LABEL
from cloud_agent_host.environment_preparation import EnvironmentPreparation, write_setup_marker
from cloud_agent_host.setup import run_setup
from cloud_agent_shared.environment import ENVIRONMENT_CAPABILITY, BoundEnvironment
from cloud_agent_shared.environment_snapshots import ENVIRONMENT_SNAPSHOT_CAPABILITY
from cloud_agent_shared.secret_redaction import SecretRedactor

#: Snapshot ids per question to the control plane; one Sandbox Manager delete
#: request takes as many.
_RETIRED_IMAGES_BATCH = 100


def environment_run_spec(run_id: str, generation: int, image_digest: str | None) -> ChatSandboxSpec:
    """The disposable group one environment run (test or prebuild) executes in."""
    return ChatSandboxSpec(
        thread_id=f"athr_env_{run_id}",
        sandbox_id=f"aenv_{run_id}",
        generation=generation,
        runtime_id="claude-code",
        sandbox_image_digest=image_digest,
        labels={ENVIRONMENT_TEST_LABEL: run_id},
    )


def claim_and_execute(
    *,
    base_url: str,
    credential: str,
    backend: Any,
    stop: threading.Event | None = None,
    cleanup_orphans: bool = True,
    cleanup_snapshots: bool = False,
) -> bool:
    """Claim and clean up one test or prebuild using a normal registered host slot."""
    from cloud_agent_host.runner import _claim_attestation

    with httpx.Client(
        base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {credential}"}, timeout=30
    ) as client:
        if cleanup_orphans:
            orphan_response = client.get("/v1/agent/worker/environment-runs/orphans")
            orphan_response.raise_for_status()
            manager = ChatSandboxManager(backend)
            for orphan in orphan_response.json().get("runs", []):
                spec = environment_run_spec(
                    orphan["id"], int(orphan["lease_generation"]), orphan.get("image_digest")
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
        if cleanup_snapshots:
            _delete_retired_snapshots(client)
        if stop is not None and stop.is_set():
            return False
        response = client.post(
            "/v1/agent/worker/environment-runs/claim",
            json={
                "capabilities": [ENVIRONMENT_CAPABILITY, ENVIRONMENT_SNAPSHOT_CAPABILITY],
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
    prebuild = run.get("purpose") == "prebuild"
    # A prebuild only installs: what it saves is the installation. Only a
    # prebuild saves one. A test also runs the start and verify scripts with
    # runtime values, and nothing they write may reach another workspace.
    prepared = bound.model_copy(update={"start_script": ""}) if prebuild else bound
    target = run.get("snapshot") if prebuild and isinstance(run.get("snapshot"), dict) else None
    redactor = SecretRedactor(bound.secrets())
    manager = ChatSandboxManager(backend)
    spec = environment_run_spec(
        run["id"],
        int(run["lease_generation"]),
        backend.version_stamp().get("sandbox_image_digest"),
    )
    cancelled = stop or threading.Event()
    heartbeat_stop = threading.Event()
    steps: list[dict[str, Any]] = []
    data_lock = threading.Lock()
    path = f"/v1/agent/worker/environment-runs/{run['id']}"
    began = time.monotonic()

    def report(
        state: str = "running",
        detail: str = "",
        *,
        cleanup_complete: bool = False,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        with data_lock:
            payload = {
                "generation": int(run["lease_generation"]),
                "state": state,
                "steps": [dict(s) for s in steps],
                "detail": redactor.text(detail)[-1000:],
                "image_digest": spec.sandbox_image_digest,
                "cleanup_complete": cleanup_complete,
                **({"snapshot": snapshot} if snapshot is not None else {}),
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
        if prebuild and (target is None or not bound.setup_fingerprint):
            # Its snapshot was deleted after it was queued: installing would
            # save nothing.
            raise RuntimeError("This prebuild has no installation snapshot to save")
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
        source_sha = source.import_into(manager, spec)
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

        EnvironmentPreparation().prepare(retained, prepared, callback=on_step, cancel=cancelled)
        if bound.verify_script and not prebuild:
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
        if target is not None:
            report(
                snapshot=_save_snapshot(
                    manager,
                    spec,
                    bound,
                    run,
                    target,
                    container_id=container_id,
                    source_sha=source_sha,
                    lockfiles=source.lockfiles(),
                )
            )
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


def _save_snapshot(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    bound: BoundEnvironment,
    run: dict[str, Any],
    target: dict[str, Any],
    *,
    container_id: str,
    source_sha: str,
    lockfiles: dict[str, str] | None,
) -> dict[str, Any]:
    """Turn this run's container into an installation snapshot and return its receipt.

    The marker names the snapshot, the repository's own files and history are
    removed (a workspace started from it imports its own), and the stopped
    container is committed and uploaded by the sandbox manager, which holds
    the bucket's credentials.
    """
    from cloud_agent_host.runner import _SAFE_DIRECTORY_ENV, _session_supervisor_from_env

    if lockfiles is None:
        raise RuntimeError("the checkout's dependency manifests are unknown")
    write_setup_marker(
        manager.write_environment_setup_marker,
        spec,
        container_id,
        setup_fingerprint=bound.setup_fingerprint,
        binding_id=bound.binding_id,
        snapshot_id=str(target["snapshot_id"]),
    )
    removed = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            "set -eu; git ls-files -z | xargs -0 -r rm -f --; rm -rf .git",
        ],
        env=dict(_SAFE_DIRECTORY_ENV),
        workdir="/workspace",
        timeout_s=300,
        user_uid=manager.backend.uid,
        user_gid=manager.backend.gid,
        expected_container_id=container_id,
    )
    if removed.returncode != 0:
        raise RuntimeError("the repository could not be removed from the snapshot")
    manager.stop(spec, expected_container_id=container_id)
    receipt = _session_supervisor_from_env().capture_environment_snapshot(
        run_id=str(run["id"]),
        lease_generation=int(run["lease_generation"]),
        container_id=container_id,
        sandbox_image_digest=spec.sandbox_image_digest,
        owner=str(target["owner"]),
        snapshot_id=str(target["snapshot_id"]),
    )
    return {**receipt, "lockfiles": lockfiles, "source_sha": source_sha}


def _delete_retired_snapshots(client: httpx.Client) -> None:
    """Delete retired snapshots' stored objects, and drop retired images held here."""
    from cloud_agent_host.runner import _session_supervisor_from_env
    from cloud_agent_host.sandbox import SandboxError
    from cloud_agent_host.session_supervisor_client import SessionSupervisorError

    try:
        supervisor = _session_supervisor_from_env()
    except SandboxError:
        # Without a Sandbox Manager this host neither stores nor holds snapshots.
        return
    response = client.get("/v1/agent/worker/environment-snapshots/deletions")
    response.raise_for_status()
    retired = response.json().get("snapshots", [])
    if retired:
        for snapshot_id in supervisor.delete_environment_snapshots(retired):
            client.post(f"/v1/agent/worker/environment-snapshots/{snapshot_id}/deleted")
    # Each host names the images it holds, so it clears all of its own however
    # many snapshots were retired since.
    try:
        held = supervisor.environment_snapshot_images()
    except SessionSupervisorError:
        # An older Sandbox Manager cannot list them.
        return
    for start in range(0, len(held), _RETIRED_IMAGES_BATCH):
        answer = client.post(
            "/v1/agent/worker/environment-snapshots/retired",
            json={"snapshot_ids": held[start : start + _RETIRED_IMAGES_BATCH]},
        )
        if answer.status_code == 404:
            # An older control plane names them among its deletions instead.
            return
        answer.raise_for_status()
        drop = [
            {**item, "object_key": None, "version_id": None}
            for item in answer.json().get("snapshots", [])
        ]
        if drop:
            supervisor.delete_environment_snapshots(drop)


class EnvironmentTestWorker:
    """Keep test execution and orphan RPCs off the host's coding-task claim loop."""

    def __init__(self, base_url: str, backend: Any) -> None:
        self._base_url = base_url
        self._backend = backend
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_poll = 0.0
        self._next_cleanup = 0.0
        self._next_snapshot_cleanup = 0.0

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
        snapshots = time.monotonic() >= self._next_snapshot_cleanup
        if snapshots:
            self._next_snapshot_cleanup = time.monotonic() + 600

        def execute() -> None:
            try:
                claim_and_execute(
                    base_url=self._base_url,
                    credential=credential,
                    backend=self._backend,
                    stop=self._stop,
                    cleanup_orphans=cleanup,
                    cleanup_snapshots=snapshots,
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
