"""End-to-end smoke test for ``deploy/local``.

The test deliberately crosses every public boundary: OAuth PKCE, the
control-plane session cookie, config and job APIs, host claim, every pinned
Agent CLI inside a retained Docker sandbox, idle ``docker stop``, native
resume, and a latest-settled full Docker fork.
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
JOB_TIMEOUT_S = 240
IDLE_STOP_TIMEOUT_S = 100
RUNTIMES = ("claude-code", "codex", "pi", "opencode", "kilo")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Expose the OAuth callback location instead of loading the Next page."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(
        self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} answered {exc.code}: {detail}") from exc

    def login(self) -> dict[str, Any]:
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        redirect_uri = f"{self.base}/callback"
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": "cloud-agent",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }

        # First prove the explicit local consent page is actually served.
        consent = (
            self.opener.open(f"{self.base}/authorize?{urllib.parse.urlencode(params)}", timeout=30)
            .read()
            .decode()
        )
        if "Continue locally" not in consent:
            raise RuntimeError("the local OAuth consent page did not render")

        no_redirect = urllib.request.build_opener(NoRedirect())
        try:
            no_redirect.open(
                f"{self.base}/authorize/continue?{urllib.parse.urlencode(params)}", timeout=30
            )
            raise RuntimeError("local authorization did not redirect")
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise
            location = exc.headers.get("Location") or ""
        callback = urllib.parse.urlparse(location)
        callback_params = urllib.parse.parse_qs(callback.query)
        code = (callback_params.get("code") or [""])[0]
        returned_state = (callback_params.get("state") or [""])[0]
        if not code or returned_state != state:
            raise RuntimeError("local authorization returned an invalid code or state")

        token = self.request(
            "/v1/identity/token",
            method="POST",
            body={
                "code": code,
                "code_verifier": verifier,
                "client_id": "cloud-agent",
                "redirect_uri": redirect_uri,
            },
        )["access_token"]
        return self.request("/api/v1/session/login", method="POST", body={"identity_token": token})


def wait_job(client: Client, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT_S
    last_state = ""
    while time.monotonic() < deadline:
        job = client.request(f"/api/v1/agent/jobs/{urllib.parse.quote(job_id)}")
        state = str(job.get("state") or "")
        if state != last_state:
            print(f"  job {job_id}: {state}", flush=True)
            last_state = state
        if state in TERMINAL_STATES:
            if state != "succeeded":
                raise RuntimeError(f"job {job_id} ended {state}: {job.get('detail')}")
            return job
        time.sleep(1)
    raise RuntimeError(f"job {job_id} did not settle within {JOB_TIMEOUT_S}s")


def chat_container(thread_id: str) -> tuple[str, str, str]:
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=org.freeinference.chat-thread-id={thread_id}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if len(listed) != 1:
        raise RuntimeError(f"expected one retained container for {thread_id}, found {listed}")
    container_id = listed[0]
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{json .Mounts}}",
            container_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status, mounts = inspected.split("|", 1)
    return container_id, status, mounts


def wait_stopped(thread_id: str) -> str:
    deadline = time.monotonic() + IDLE_STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        container_id, status, mounts = chat_container(thread_id)
        if mounts != "[]":
            raise RuntimeError(f"retained container unexpectedly has host/volume mounts: {mounts}")
        if status in {"exited", "created"}:
            return container_id
        time.sleep(2)
    raise RuntimeError("retained container was not docker-stopped after the inactivity timeout")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:13002"
    client = Client(base)

    print("[1/6] PKCE login and HttpOnly control-plane session", flush=True)
    user = client.login()
    if user.get("id") != "local-developer":
        raise RuntimeError(f"unexpected local identity: {user}")

    print("[2/6] Runnable config from real gateway/entitlement predicates", flush=True)
    config = client.request("/api/v1/agent/config")
    expected_config = (
        "local/demo",
        "local-demo-model",
        *RUNTIMES,
    )
    offered = [
        *(config.get("repos") or []),
        *(config.get("models") or []),
        *(config.get("runtimes") or []),
    ]
    for expected in expected_config:
        values = offered
        if expected not in values:
            raise RuntimeError(f"config does not offer {expected!r}: {config}")
    if not config.get("github_connected"):
        raise RuntimeError("the allowlisted local repository left the composer disabled")

    print("[3/6] Every pinned CLI creates NEW then natively resumes LIVE", flush=True)
    sessions: dict[str, tuple[dict[str, Any], str, str]] = {}
    for runtime in RUNTIMES:
        print(f"  {runtime}: first turn", flush=True)
        created = client.request(
            "/api/v1/agent/jobs",
            method="POST",
            body={
                "repo": "local/demo",
                "task_prompt": f"Confirm that the local {runtime} session is working.",
                "runtime": runtime,
                "model": "local-demo-model",
            },
        )
        first = wait_job(client, str(created["id"]))
        if first.get("context_status") != "new":
            raise RuntimeError(f"{runtime} first turn was not NEW: {first.get('context_status')!r}")
        thread_id = str(first["thread_id"])
        original_id, status, mounts = chat_container(thread_id)
        if status != "running" or mounts != "[]":
            raise RuntimeError(f"{runtime} new chat container is {status} with mounts {mounts}")

        print(f"  {runtime}: follow-up", flush=True)
        follow_up = client.request(
            f"/api/v1/agent/jobs/{urllib.parse.quote(str(first['id']))}/follow-ups",
            method="POST",
            body={"prompt": "Confirm that the same native session received this follow-up."},
        )
        live = wait_job(client, str(follow_up["id"]))
        if live.get("context_status") != "live":
            raise RuntimeError(
                f"{runtime} running-container follow-up was not LIVE: "
                f"{live.get('context_status')!r}"
            )
        live_id, _, _ = chat_container(thread_id)
        if live_id != original_id:
            raise RuntimeError(f"{runtime} LIVE follow-up replaced the chat container")
        sessions[runtime] = (live, thread_id, original_id)

    print("[4/6] One inactivity clock docker-stops every mount-free container", flush=True)
    for runtime, (_live, thread_id, original_id) in sessions.items():
        stopped_id = wait_stopped(thread_id)
        if stopped_id != original_id:
            raise RuntimeError(f"{runtime} idle stop replaced the chat container")

    print("[5/6] Every CLI starts the same container and reports RESTORED", flush=True)
    restored_sessions: dict[str, tuple[dict[str, Any], str, str]] = {}
    for runtime, (live, thread_id, original_id) in sessions.items():
        print(f"  {runtime}: stopped-container follow-up", flush=True)
        restored_turn = client.request(
            f"/api/v1/agent/jobs/{urllib.parse.quote(str(live['id']))}/follow-ups",
            method="POST",
            body={"prompt": f"Confirm that the stopped {runtime} sandbox restored this session."},
        )
        restored = wait_job(client, str(restored_turn["id"]))
        if restored.get("context_status") != "restored":
            raise RuntimeError(
                f"{runtime} stopped-container follow-up was not RESTORED: "
                f"{restored.get('context_status')!r}"
            )
        restored_id, restored_status, restored_mounts = chat_container(thread_id)
        if restored_id != original_id or restored_status != "running" or restored_mounts != "[]":
            raise RuntimeError(
                f"{runtime} restore did not preserve the same running mount-free container: "
                f"{restored_id} {restored_status} {restored_mounts}"
            )
        restored_sessions[runtime] = (restored, thread_id, original_id)

    print("[6/6] Latest-settled fork clones history + Docker state, then restores", flush=True)
    source, source_thread_id, source_container_id = restored_sessions["claude-code"]
    forked = client.request(
        f"/api/v1/agent/jobs/{urllib.parse.quote(str(source['id']))}/fork",
        method="POST",
    )
    target_thread_id = str(forked.get("thread_id") or "")
    if not target_thread_id or target_thread_id == source_thread_id:
        raise RuntimeError("full fork did not allocate an independent chat thread")
    target_container_id, target_status, target_mounts = chat_container(target_thread_id)
    if target_container_id == source_container_id:
        raise RuntimeError("full fork reused the source container")
    if target_status not in {"created", "exited"} or target_mounts != "[]":
        raise RuntimeError(
            "full fork did not leave an independent stopped mount-free container: "
            f"{target_container_id} {target_status} {target_mounts}"
        )
    source_thread = client.request(
        f"/api/v1/agent/jobs/{urllib.parse.quote(str(source['id']))}/thread"
    )
    target_thread = client.request(
        f"/api/v1/agent/jobs/{urllib.parse.quote(str(forked['id']))}/thread"
    )
    source_words = [(item["role"], item["content"]) for item in source_thread["messages"]]
    target_words = [(item["role"], item["content"]) for item in target_thread["messages"]]
    if len(target_thread.get("jobs") or []) != len(source_thread.get("jobs") or []):
        raise RuntimeError("full fork did not copy the complete settled job prefix")
    if target_words != source_words:
        raise RuntimeError("full fork did not copy the complete conversation prefix")

    fork_follow_up = client.request(
        f"/api/v1/agent/jobs/{urllib.parse.quote(str(forked['id']))}/follow-ups",
        method="POST",
        body={"prompt": "Confirm that this independent full-sandbox fork restored natively."},
    )
    fork_restored = wait_job(client, str(fork_follow_up["id"]))
    if fork_restored.get("context_status") != "restored":
        raise RuntimeError(
            f"fork follow-up was not RESTORED: {fork_restored.get('context_status')!r}"
        )
    resumed_target_id, resumed_target_status, resumed_target_mounts = chat_container(
        target_thread_id
    )
    if (
        resumed_target_id != target_container_id
        or resumed_target_status != "running"
        or resumed_target_mounts != "[]"
    ):
        raise RuntimeError("fork follow-up replaced or mounted the cloned container")
    observed_source_id, _, source_mounts = chat_container(source_thread_id)
    if observed_source_id != source_container_id or source_mounts != "[]":
        raise RuntimeError("full fork changed the source container")

    print(
        "PASS: OAuth + all five CLIs NEW → LIVE → docker stop → RESTORED; "
        "latest-settled full fork → RESTORED; all containers keep mounts=[]",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
