"""API assertions for the local legacy/default-off compatibility smoke.

The shell wrapper owns runner orchestration because the retained runner must be
stopped before the test job is queued.  This module only crosses public HTTP
boundaries: create one job, then prove that the disposable sandbox reached the
model and completed the final in-sandbox patch step.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from smoke import Client, wait_job


def create_job(base_url: str) -> str:
    client = Client(base_url)
    client.login()
    created = client.request(
        "/api/v1/agent/jobs",
        method="POST",
        body={
            "repo": "local/demo",
            "task_prompt": (
                "Confirm the legacy disposable sandbox path works without retained chat mode."
            ),
            "runtime": "claude-code",
            "model": "local-demo-model",
        },
    )
    return str(created["id"])


def wait_default_off(base_url: str, host: str) -> None:
    """Wait until the temporary runner's capability downgrade is durable."""
    client = Client(base_url)
    client.login()
    deadline = time.monotonic() + 30
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        pool = client.request("/api/admin/agent/runner-hosts")
        last = next(
            (item for item in pool.get("hosts") or [] if item.get("host") == host),
            None,
        )
        if last is not None and "shared-sandbox-v1" not in (last.get("capabilities") or []):
            print(f"  host {host}: default-off capability heartbeat observed", flush=True)
            return
        time.sleep(0.25)
    raise RuntimeError(f"host {host!r} did not drop shared capability: {last}")


def _event_phases(events: list[dict[str, Any]]) -> set[str]:
    return {
        str((event.get("payload") or {}).get("phase"))
        for event in events
        if event.get("event_type") == "lifecycle"
    }


def verify_job(base_url: str, job_id: str) -> None:
    client = Client(base_url)
    client.login()
    job = wait_job(client, job_id)
    if job.get("context_status") is not None:
        raise RuntimeError(
            f"legacy smoke unexpectedly used retained context: {job.get('context_status')!r}"
        )

    event_page = client.request(f"/api/v1/agent/jobs/{job_id}/events?after=0&limit=100")
    events = list(event_page.get("events") or [])
    phases = _event_phases(events)
    required = {"started", "checked_out", "workspace_ready", "checkpoint_exported"}
    missing = sorted(required - phases)
    if missing:
        raise RuntimeError(f"legacy smoke missed lifecycle phases: {missing}")
    if not any(event.get("event_type") == "diff" for event in events):
        raise RuntimeError("legacy smoke never completed in-sandbox patch generation")

    print(
        "PASS: legacy/default-off disposable sandbox reached the CLI, exported "
        "its checkpoint, and completed patch generation",
        flush=True,
    )


def cancel_job(base_url: str, job_id: str) -> None:
    """Keep a failed smoke's queued job away from the retained runner."""
    client = Client(base_url)
    client.login()
    client.request(f"/api/v1/agent/jobs/{job_id}/cancel", method="POST")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {
        "create",
        "verify",
        "cancel",
        "wait-default-off",
    }:
        print(
            "usage: legacy_smoke.py {create|verify|cancel|wait-default-off} BASE_URL [JOB_ID|HOST]",
            file=sys.stderr,
        )
        return 2
    mode = argv[1]
    base_url = argv[2] if len(argv) > 2 else "http://127.0.0.1:13002"
    if mode == "create":
        print(create_job(base_url))
        return 0
    if mode == "wait-default-off":
        if len(argv) != 4:
            print("wait-default-off requires HOST", file=sys.stderr)
            return 2
        wait_default_off(base_url, argv[3])
        return 0
    if len(argv) != 4:
        print(f"{mode} requires JOB_ID", file=sys.stderr)
        return 2
    if mode == "verify":
        verify_job(base_url, argv[3])
    else:
        cancel_job(base_url, argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
