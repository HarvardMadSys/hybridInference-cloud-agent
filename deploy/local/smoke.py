"""End-to-end smoke test for ``deploy/local``.

The test deliberately crosses every public boundary: OAuth PKCE, the
control-plane session cookie, config and job APIs, host claim, every pinned
Agent CLI inside a retained Docker sandbox, idle ``docker stop``, native
resume, and same-workspace native session forks with independently live source
and child processes.
"""

from __future__ import annotations

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
SNAPSHOT_TIMEOUT_S = 100
CONCURRENT_RUNNING_TIMEOUT_S = 30
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
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} answered {exc.code}: {detail}") from exc

    def login(self) -> dict[str, Any]:
        """Sign in the way a browser does: navigate, consent, come back.

        The client builds no PKCE pair and never sees an identity token — the
        control plane owns the whole round trip now. What this drives is
        exactly what a person would: follow the redirect to the consent page,
        click through it, and let the callback set the cookie.
        """
        no_redirect = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect()
        )

        # 1. The control plane parks the flow and points at the consent page.
        try:
            no_redirect.open(f"{self.base}/api/v1/session/login?return_to=/", timeout=30)
            raise RuntimeError("sign-in did not redirect to the gateway")
        except urllib.error.HTTPError as exc:
            if exc.code not in (302, 303):
                raise
            authorize = exc.headers.get("Location") or ""
        if not self.cookies:
            raise RuntimeError("sign-in parked no flow cookie")

        # 2. The consent page really is served, and really is explicit.
        consent = self.opener.open(authorize, timeout=30).read().decode()
        if "Continue locally" not in consent:
            raise RuntimeError("the local OAuth consent page did not render")

        # 3. Clicking through redirects to the *control plane's* callback.
        query = urllib.parse.urlparse(authorize).query
        try:
            no_redirect.open(f"{self.base}/authorize/continue?{query}", timeout=30)
            raise RuntimeError("local authorization did not redirect")
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise
            callback = exc.headers.get("Location") or ""
        if "/api/v1/session/callback" not in callback:
            raise RuntimeError(f"authorization returned to {callback!r}, not the control plane")

        # 4. The callback exchanges the code server-side and sets the session.
        try:
            no_redirect.open(callback, timeout=30)
            raise RuntimeError("the callback did not redirect into the app")
        except urllib.error.HTTPError as exc:
            if exc.code not in (302, 303):
                raise
        if not any(cookie.name == "cloud_agent_session" for cookie in self.cookies):
            raise RuntimeError("sign-in completed without setting a session cookie")

        return self.request("/api/v1/session/me")


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
            require_assistant_reply(client, job_id)
            return job
        time.sleep(1)
    raise RuntimeError(f"job {job_id} did not settle within {JOB_TIMEOUT_S}s")


def send_message(client: Client, turn: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Send the next message of a finished turn's conversation; return its turn.

    The turn before it has ended, so the conversation is idle and the message
    becomes the next turn in the same request. A message that was queued
    instead would mean the control plane still counts a turn as running.
    """
    result = client.request(
        f"/api/v1/agent/threads/{urllib.parse.quote(str(turn['thread_id']))}/messages",
        method="POST",
        body={"prompt": prompt},
        headers={"Idempotency-Key": f"local-smoke-message-{secrets.token_hex(8)}"},
    )
    job = result.get("job")
    if not isinstance(job, dict):
        raise RuntimeError(f"a message to an idle conversation did not start a turn: {result}")
    return job


def require_assistant_reply(client: Client, job_id: str) -> None:
    """A successful confirmation task must have its own nonempty reply."""
    thread = client.request(f"/api/v1/agent/jobs/{urllib.parse.quote(job_id)}/thread")
    if not any(
        item.get("job_id") == job_id
        and item.get("role") == "assistant"
        and str(item.get("content") or "").strip()
        for item in thread.get("messages", [])
    ):
        raise RuntimeError(f"job {job_id} succeeded without an assistant reply")


def wait_concurrently_running(client: Client, source_job_id: str, child_job_id: str) -> None:
    """Prove two fork siblings are admitted together, not merely queued together."""
    deadline = time.monotonic() + CONCURRENT_RUNNING_TIMEOUT_S
    last_states: tuple[str, str] = ("", "")
    while time.monotonic() < deadline:
        source = client.request(f"/api/v1/agent/jobs/{urllib.parse.quote(source_job_id)}")
        child = client.request(f"/api/v1/agent/jobs/{urllib.parse.quote(child_job_id)}")
        states = (str(source.get("state") or ""), str(child.get("state") or ""))
        if states != last_states:
            print(f"  concurrent states: source={states[0]} child={states[1]}", flush=True)
            last_states = states
        if states == ("running", "running"):
            return
        if states[0] in TERMINAL_STATES or states[1] in TERMINAL_STATES:
            break
        time.sleep(0.1)
    raise RuntimeError(
        "fork siblings were never simultaneously RUNNING; "
        f"last states were source={last_states[0]} child={last_states[1]}"
    )


def chat_container(sandbox_id: str) -> tuple[str, str, str]:
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=org.freeinference.chat-sandbox-id={sandbox_id}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if len(listed) != 1:
        raise RuntimeError(f"expected one retained container for {sandbox_id}, found {listed}")
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


def _docker_json(*argv: str) -> Any:
    return json.loads(
        subprocess.run(["docker", *argv], check=True, capture_output=True, text=True).stdout
    )


def chat_private_network(container_id: str) -> str:
    """Require one guarded group bridge containing only this chat and two relays."""
    networks = _docker_json(
        "inspect", "--format", "{{json .NetworkSettings.Networks}}", container_id
    )
    if len(networks) != 1:
        raise RuntimeError(f"chat {container_id[:12]} has non-exclusive networks: {networks}")
    network_name = next(iter(networks))
    if not network_name.startswith("freeinference-chat-net-"):
        raise RuntimeError(f"chat {container_id[:12]} remained on shared network {network_name!r}")
    raw = _docker_json("network", "inspect", network_name)[0]
    labels = raw.get("Labels") or {}
    members = raw.get("Containers") or {}
    options = raw.get("Options") or {}
    if labels.get("org.freeinference.chat-sandbox-network") != "true":
        raise RuntimeError(f"chat network {network_name!r} has no ownership label")
    # Routable on purpose: the host rules, not the absence of a route, are the
    # boundary. They match the group by this interface name.
    if raw.get("Internal") or raw.get("EnableIPv6"):
        raise RuntimeError(f"chat network {network_name!r} is not the one routable IPv4 bridge")
    if not options.get("com.docker.network.bridge.name"):
        raise RuntimeError(f"chat network {network_name!r} has no guarded interface name")
    if len(members) != 3 or not any(str(member).startswith(container_id) for member in members):
        raise RuntimeError(
            f"chat network {network_name!r} must contain one sandbox and two relays, "
            f"found {[item.get('Name') for item in members.values()]}"
        )
    return network_name


# Run inside the chat as its own user. A refusal must be a refusal: a timeout
# says nothing about which rule, if any, stopped the packet.
_BOUNDARY_PROBE = """
import socket, sys
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=8).close()
        print(target, "open")
    except TimeoutError:
        print(target, "timeout")
    except OSError as exc:
        print(target, "refused", exc.errno)
"""


def chat_network_boundary(container_id: str, network_name: str) -> None:
    """The one network from inside: the relays and the public web, not the stack.

    The negative target is a real listener: the local gateway itself, at its
    private address on the control network. The same service answers through
    the group bridge, so only the host rules can make this one fail.
    """
    relay_ids = [
        member
        for member in _docker_json("network", "inspect", network_name)[0]["Containers"]
        if not member.startswith(container_id)
    ]
    private = None
    for relay_id in relay_ids:
        relay = _docker_json("inspect", relay_id)[0]
        attached = relay["NetworkSettings"]["Networks"]
        if "local-gateway" not in {
            alias for settings in attached.values() for alias in settings.get("Aliases") or []
        }:
            continue
        private = next(
            settings["IPAddress"]
            for name, settings in attached.items()
            if name != network_name and "relays" not in name and settings.get("IPAddress")
        )
    if private is None:
        raise RuntimeError(f"no local gateway endpoint found beside chat {container_id[:12]}")
    expected = {
        "local-gateway:8080": "open",
        "mcp-relay:8001": "open",
        "example.com:443": "open",
        f"{private}:8080": "refused",
    }
    probed = subprocess.run(
        ["docker", "exec", container_id, "python3", "-c", _BOUNDARY_PROBE, *expected],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    ).stdout.split("\n")
    observed = {line.split()[0]: line.split()[1] for line in probed if line.strip()}
    if observed != expected:
        raise RuntimeError(
            f"chat {container_id[:12]} network boundary: {observed}, want {expected}"
        )


def wait_stopped(sandbox_id: str) -> str:
    deadline = time.monotonic() + IDLE_STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        container_id, status, mounts = chat_container(sandbox_id)
        if mounts != "[]":
            raise RuntimeError(f"retained container unexpectedly has host/volume mounts: {mounts}")
        if status in {"exited", "created"}:
            return container_id
        time.sleep(2)
    raise RuntimeError("retained container was not docker-stopped after the inactivity timeout")


def wait_workspace_published(client: Client, job_id: str) -> None:
    """Require the asynchronous shared capture to leave its pending queue."""
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_S
    last_state = ""
    while time.monotonic() < deadline:
        job = client.request(f"/api/v1/agent/jobs/{urllib.parse.quote(job_id)}")
        state = str(job.get("workspace_publication_state") or "")
        if state != last_state:
            print(f"  snapshot for {job_id}: {state or 'missing'}", flush=True)
            last_state = state
        if state == "published":
            return
        if state == "failed":
            raise RuntimeError(f"shared snapshot for {job_id} failed: {job.get('detail')}")
        time.sleep(1)
    raise RuntimeError(
        f"shared snapshot for {job_id} remained {last_state or 'missing'} for {SNAPSHOT_TIMEOUT_S}s"
    )


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:13002"
    client = Client(base)

    print("[1/7] PKCE login and HttpOnly control-plane session", flush=True)
    user = client.login()
    if user.get("id") != "local-developer":
        raise RuntimeError(f"unexpected local identity: {user}")

    print("[2/7] Runnable config from real gateway/entitlement predicates", flush=True)
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

    print("[3/7] Every pinned CLI creates NEW then continues one live process", flush=True)
    sessions: dict[str, tuple[dict[str, Any], str, str]] = {}
    private_networks: set[str] = set()
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
        if first.get("workspace_shared") is not True or not first.get("sandbox_id"):
            raise RuntimeError(f"{runtime} first turn did not create a shared sandbox: {first}")
        sandbox_id = str(first["sandbox_id"])
        original_id, status, mounts = chat_container(sandbox_id)
        if status != "running" or mounts != "[]":
            raise RuntimeError(f"{runtime} new chat container is {status} with mounts {mounts}")
        private_network = chat_private_network(original_id)
        chat_network_boundary(original_id, private_network)
        if private_network in private_networks:
            raise RuntimeError(
                f"{runtime} shares chat network {private_network!r} with another group"
            )
        private_networks.add(private_network)

        print(f"  {runtime}: follow-up", flush=True)
        follow_up = send_message(
            client, first, "Confirm that the same native session received this follow-up."
        )
        live = wait_job(client, str(follow_up["id"]))
        if live.get("context_status") != "live":
            raise RuntimeError(
                f"{runtime} running-container follow-up was not LIVE: "
                f"{live.get('context_status')!r}"
            )
        live_id, _, _ = chat_container(sandbox_id)
        if live_id != original_id:
            raise RuntimeError(f"{runtime} LIVE follow-up replaced the chat container")
        stopped_id = wait_stopped(sandbox_id)
        if stopped_id != original_id:
            raise RuntimeError(f"{runtime} idle stop replaced the chat container")
        sessions[runtime] = (live, sandbox_id, original_id)

    print("[4/7] One inactivity clock docker-stopped every mount-free container", flush=True)

    print("[5/7] Every CLI starts the same container and reports RESTORED", flush=True)
    restored_sessions: dict[str, tuple[dict[str, Any], dict[str, Any], str, str]] = {}
    for runtime, (live, sandbox_id, original_id) in sessions.items():
        print(f"  {runtime}: stopped-container follow-up", flush=True)
        restored_turn = send_message(
            client, live, f"Confirm that the stopped {runtime} sandbox restored this session."
        )
        restored = wait_job(client, str(restored_turn["id"]))
        if restored.get("context_status") != "restored":
            raise RuntimeError(
                f"{runtime} stopped-container follow-up was not RESTORED: "
                f"{restored.get('context_status')!r}"
            )
        restored_id, restored_status, restored_mounts = chat_container(sandbox_id)
        if restored_id != original_id or restored_status != "running" or restored_mounts != "[]":
            raise RuntimeError(
                f"{runtime} restore did not preserve the same running mount-free container: "
                f"{restored_id} {restored_status} {restored_mounts}"
            )
        stopped_id = wait_stopped(sandbox_id)
        if stopped_id != original_id:
            raise RuntimeError(f"{runtime} restored container changed before its next idle stop")
        # Keep turn two as a historical native anchor. At fork time the source
        # session already has turn three, so this exercises real prefix rewind
        # rather than the old latest-only path.
        restored_sessions[runtime] = (restored, live, sandbox_id, original_id)

    print(
        "[6/7] Every CLI forks immediately into a second session in the same workspace", flush=True
    )
    forked_sessions: dict[str, tuple[dict[str, Any], str, str]] = {}
    for runtime, (
        source,
        historical_anchor,
        sandbox_id,
        source_container_id,
    ) in restored_sessions.items():
        print(
            f"  {runtime}: historical native fork + two independently admitted turns",
            flush=True,
        )
        source_thread_id = str(source["thread_id"])
        forked = client.request(
            f"/api/v1/agent/jobs/{urllib.parse.quote(str(historical_anchor['id']))}/fork",
            method="POST",
            headers={"Idempotency-Key": f"local-smoke-{runtime}-{secrets.token_hex(8)}"},
        )
        target_thread_id = str(forked.get("thread_id") or "")
        if not target_thread_id or target_thread_id == source_thread_id:
            raise RuntimeError(f"{runtime} native fork did not allocate a second chat")
        if forked.get("workspace_shared") is not True or forked.get("sandbox_id") != sandbox_id:
            raise RuntimeError(f"{runtime} native fork did not retain the shared sandbox: {forked}")
        if not forked.get("native_fork_available"):
            raise RuntimeError(f"{runtime} fork child is not itself native-forkable")
        shared_id, shared_status, shared_mounts = chat_container(sandbox_id)
        if shared_id != source_container_id or shared_status != "running" or shared_mounts != "[]":
            raise RuntimeError(
                f"{runtime} fork replaced, stopped, or mounted the shared container: "
                f"{shared_id} {shared_status} {shared_mounts}"
            )

        source_thread = client.request(
            f"/api/v1/agent/jobs/{urllib.parse.quote(str(source['id']))}/thread"
        )
        target_thread = client.request(
            f"/api/v1/agent/jobs/{urllib.parse.quote(str(forked['id']))}/thread"
        )
        anchor_turn_no = int(historical_anchor.get("turn_no") or 0)
        source_prefix_ids = {
            str(item["id"])
            for item in source_thread.get("jobs") or []
            if int(item.get("turn_no") or 0) <= anchor_turn_no
        }
        source_words = [
            (item["role"], item["content"])
            for item in source_thread["messages"]
            if str(item["job_id"]) in source_prefix_ids
        ]
        target_words = [(item["role"], item["content"]) for item in target_thread["messages"]]
        if len(target_thread.get("jobs") or []) != len(source_prefix_ids):
            raise RuntimeError(f"{runtime} historical fork copied the wrong job prefix")
        if len(source_prefix_ids) >= len(source_thread.get("jobs") or []):
            raise RuntimeError(f"{runtime} smoke did not select a genuinely historical anchor")
        if target_words != source_words:
            raise RuntimeError(f"{runtime} historical fork copied the wrong conversation prefix")

        # Queue both before awaiting either. With two host slots this crosses
        # the real no-group-turn-lock path: source and child own distinct
        # attempts, grants, resident processes, and native session ids.
        concurrency_probe_key = f"{runtime}-{secrets.token_hex(8)}"
        concurrency_probe = f"[local-concurrency-probe:{concurrency_probe_key}]"
        source_follow_up = send_message(
            client,
            source,
            f"Continue the source {runtime} session after its native fork. {concurrency_probe}",
        )
        child_follow_up = send_message(
            client,
            forked,
            f"Continue the child {runtime} session after its native fork. {concurrency_probe}",
        )
        wait_concurrently_running(
            client,
            str(source_follow_up["id"]),
            str(child_follow_up["id"]),
        )
        source_live = wait_job(client, str(source_follow_up["id"]))
        child_live = wait_job(client, str(child_follow_up["id"]))
        completed_probes = client.request("/health").get("completed_concurrency_probes") or []
        if concurrency_probe_key not in completed_probes:
            raise RuntimeError(
                f"{runtime} turns were RUNNING but did not overlap at the inference barrier"
            )
        for side, turn in (("source", source_live), ("child", child_live)):
            if turn.get("context_status") != "live":
                raise RuntimeError(
                    f"{runtime} {side} post-fork turn was not LIVE: {turn.get('context_status')!r}"
                )
            if turn.get("sandbox_id") != sandbox_id or turn.get("workspace_shared") is not True:
                raise RuntimeError(f"{runtime} {side} escaped the shared sandbox")
        stopped_id = wait_stopped(sandbox_id)
        if stopped_id != source_container_id:
            raise RuntimeError(f"{runtime} fork group idle stop replaced the shared container")
        forked_sessions[runtime] = (child_live, sandbox_id, source_container_id)

    print(
        "[7/7] Group idle stops once; waking either child restores that exact container", flush=True
    )
    for runtime, (child_live, sandbox_id, original_id) in forked_sessions.items():
        restored_turn = send_message(
            client, child_live, f"Restore the stopped shared {runtime} fork group from this child."
        )
        restored_child = wait_job(client, str(restored_turn["id"]))
        if restored_child.get("context_status") != "restored":
            raise RuntimeError(
                f"{runtime} stopped fork child was not RESTORED: "
                f"{restored_child.get('context_status')!r}"
            )
        resumed_id, resumed_status, resumed_mounts = chat_container(sandbox_id)
        if resumed_id != original_id or resumed_status != "running" or resumed_mounts != "[]":
            raise RuntimeError(f"{runtime} child wake replaced or mounted the shared container")
        stopped_id = wait_stopped(sandbox_id)
        if stopped_id != original_id:
            raise RuntimeError(f"{runtime} child wake changed the next group idle stop target")
        wait_workspace_published(client, str(restored_child["id"]))
        # Snapshot capture may legitimately have won just after idle stop and
        # started the exact container. It must then obey the same group clock.
        stopped_id = wait_stopped(sandbox_id)
        if stopped_id != original_id:
            raise RuntimeError(f"{runtime} snapshot capture replaced the retained container")

    print(
        "PASS: OAuth + all five CLIs NEW → LIVE → docker stop → RESTORED; "
        "all five native-fork source/child sessions stay LIVE in one workspace; "
        "group stop → child RESTORED; snapshots publish; all containers keep mounts=[] "
        "and unrelated groups use distinct guarded bridges that reach the relays and "
        "the public web but not the stack",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
