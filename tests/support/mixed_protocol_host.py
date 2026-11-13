"""Drive one release's host-side code for ``scripts/check_mixed_protocol.py``.

Run with that release's own interpreter and checkout. The calls are the
release's own: ``enrollment.register`` and ``enrollment.heartbeat`` (whose
``retained_sandboxes`` the release's own heartbeat-row builder reads from the
sandbox manager), ``host_control.claim_payload`` and ``ClaimedJob``, and the
attempt-scoped ``ControlPlane``. Only the sandbox manager is scripted: a
loopback fake that lists what this driver says the host's Docker holds, and
answers the control plane's ``/health`` probe.

Every request goes to the control plane through a recording proxy in this
process, so each answer can be read through this release's own response model
(``mixed_protocol_wire``): one it does not satisfy is a failure, and every key
it would drop is recorded. The control plane's side records what its own
request models drop.

The scripted turns follow §3.4 of the host protocol v2 design, steps 1 to 7:

1. register and heartbeat;
2. claim a new chat's turn, and later two follow-ups on the retained group;
3. reserve, running, residency, process and admit;
4. an event and an attempt heartbeat; on the second turn the owner cancels
   and the heartbeat must say so;
5. settle and finish, or (third turn) abandon and finish;
6. a beat with the group running, a stop the control plane orders once the
   settled group is idle, and a beat that reports it stopped;
7. an evicted residency the next beat must name in ``residencies_lost``.

Steps that need the control plane to act (a follow-up, a cancellation, an
eviction) are asked of the controller with one ``CALL`` line on stdout, and
its answer is one line on stdin; so is a ``snapshot`` of the database between
steps, which the controller takes itself. The last line is ``DONE`` and a JSON
report.

These are the previous release's host APIs when this drives the previous
release: a change to one of their signatures keeps the old spelling working
here until a release with the new one has been published. Where a module
moved, both homes are tried, newest first.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_tree = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(_tree / "apps" / "backend"))

import httpx
from mixed_protocol_wire import (
    contract_errors,
    host_routes,
    matching_route,
    unknown_fields,
)

import cloud_agent_host

if not Path(cloud_agent_host.__file__).resolve().is_relative_to(_tree):
    raise SystemExit(f"{cloud_agent_host.__file__} is not from {_tree}")

from cloud_agent_host.claimed_job import ClaimedJob, require_residency_connection
from cloud_agent_host.control_plane_client import ControlPlane
from cloud_agent_host.runtimes import NormalizedEvent
from cloud_agent_shared import shared_protocol
from cloud_agent_shared.model_connections import MODEL_CONNECTION_CAPABILITY


def _first(*names: str) -> Any:
    """The first of ``module`` or ``module:attribute`` that this release has."""
    for name in names:
        module_name, _, attribute = name.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        if not attribute:
            return module
        if hasattr(module, attribute):
            return getattr(module, attribute)
    raise SystemExit(f"this release has none of {names}")


enrollment = _first("cloud_agent_host.service.enrollment", "cloud_agent_host.enrollment")
host_control = _first("cloud_agent_host.service.host_control", "cloud_agent_host.host_control")
retained_rows = _first(
    "cloud_agent_host.service.host_service:_retained_sandbox_heartbeat_rows",
    "cloud_agent_host.runner:_retained_sandbox_heartbeat_rows",
)

_HOST = os.environ["MIXED_PROTOCOL_HOST"]
_IMAGE = "sha256:" + "0" * 64
_CAPABILITIES = [
    shared_protocol.SHARED_SANDBOX_CAPABILITY,
    shared_protocol.NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    shared_protocol.shared_deployment_capability("mixed-protocol"),
    shared_protocol.GOAL_REPORT_CAPABILITY,
    MODEL_CONNECTION_CAPABILITY,
]
_ATTESTATION = {
    "sandbox_image_digest": _IMAGE,
    "runner_capabilities": _CAPABILITIES,
    "runtime_versions": dict(shared_protocol.SHARED_SANDBOX_RUNTIME_PINS),
    "helper_versions": dict(shared_protocol.SHARED_SANDBOX_HELPER_PINS),
}
_LEASE_TTL_S = 120.0
_BASE_SHA = "a" * 40
_IDLE_TIMEOUT_S = 1800.0
#: Short enough that the stop is due by the next beat, so step 6 needs no wait.
_SHORT_IDLE_TIMEOUT_S = 0.2


# --------------------------------------------------------------------- wire
class Wire:
    """What crossed the proxy: one entry per call, in order."""

    def __init__(self) -> None:
        self.step = "start"
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append({"step": self.step, **entry})


def _proxy(upstream: str, wire: Wire) -> ThreadingHTTPServer:
    routes = host_routes()
    client = httpx.Client(timeout=60.0, trust_env=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() in {"authorization", "content-type", "accept"}
            }
            response = client.request(
                self.command, upstream + self.path, content=body, headers=headers
            )
            path = self.path.split("?", 1)[0]
            route = matching_route(
                routes, {"type": "http", "path": path, "method": self.command, "root_path": ""}
            )
            entry: dict[str, Any] = {
                "method": self.command,
                "route": getattr(route, "path", path),
                "status": response.status_code,
                "dropped": [],
                "contract": None,
            }
            annotation = getattr(route, "response_model", None)
            if response.is_success and annotation is not None and response.content:
                try:
                    answer = response.json()
                except ValueError:
                    entry["contract"] = "the answer is not JSON"
                else:
                    entry["contract"] = contract_errors(annotation, answer)
                    entry["dropped"] = unknown_fields(annotation, answer)
            wire.record(entry)
            self.send_response(response.status_code)
            for key in ("content-type",):
                if key in response.headers:
                    self.send_header(key, response.headers[key])
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = _forward
        do_POST = _forward

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------- sandbox manager
#: The control plane's direct calls this script's turns cause, by path, with
#: the request model this release's sandbox manager reads each body with (its
#: class name: the module moved between releases) and the scripted answer.
_SANDBOX_MANAGER_CALLS = (
    (
        re.compile(r"^/shared-sandboxes/[^/]+/register$"),
        "/shared-sandboxes/{thread_id}/register",
        "SharedSandboxIdentityRequest",
        lambda body: {
            "ok": True,
            "sandbox_id": body.get("sandbox_id"),
            "member_count": len(body.get("members") or []),
        },
    ),
    (
        re.compile(r"^/workspaces/[^/]+/terminals/suspend$"),
        "/workspaces/{workspace_id}/terminals/suspend",
        "TerminalCoordinationRequest",
        lambda body: {"ok": True, "suspended": 0},
    ),
    (
        re.compile(r"^/workspaces/[^/]+/terminals/resume(-settled)?$"),
        "/workspaces/{workspace_id}/terminals/resume",
        "TerminalCoordinationRequest",
        lambda body: {"ok": True, "resumed": 0},
    ),
)
_SANDBOX_MANAGER_MODELS = (
    "cloud_agent_host.manager_api.models",
    "cloud_agent_host.sandbox_manager",
)


class FakeSandboxManager:
    """The host's sandbox manager, as this script says Docker stands.

    Answers the host's own observation (``GET /sandboxes``), the control
    plane's probe (``GET /health``) and the direct calls listed above, whose
    bodies it reads through this release's own request models. Anything else
    is answered 404 and reported: a new dependency the script must learn.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.groups: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _answer(self, status: int, body: dict[str, Any], **call: Any) -> None:
                with manager.lock:
                    manager.calls.append(
                        {"method": self.command, "status": status, "dropped": [], "contract": None}
                        | call
                    )
                content = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                if self.headers.get("X-Agent-Workspace-Token") != manager.token:
                    self._answer(401, {"detail": "unauthorized"}, route=path)
                    return
                if self.command == "GET" and path in {"/health", "/sandboxes"}:
                    with manager.lock:
                        rows = [json.loads(json.dumps(row)) for row in manager.groups.values()]
                    body = {"status": "ok"} if path == "/health" else {"sandboxes": rows}
                    self._answer(200, body, route=path)
                    return
                for pattern, route, model_name, answer in _SANDBOX_MANAGER_CALLS:
                    if self.command == "POST" and pattern.match(path):
                        payload = json.loads(raw or b"{}")
                        model = _first(
                            *(f"{module}:{model_name}" for module in _SANDBOX_MANAGER_MODELS)
                        )
                        self._answer(
                            200,
                            answer(payload),
                            route=route,
                            contract=contract_errors(model, payload),
                            dropped=unknown_fields(model, payload),
                        )
                        return
                self._answer(404, {"detail": f"not scripted: {self.command} {path}"}, route=path)

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def hold(self, reservation: dict[str, Any], **member: Any) -> None:
        """Docker now runs this activation, with this one member."""
        with self.lock:
            self.groups[reservation["sandbox_id"]] = {
                "thread_id": reservation["thread_id"],
                "sandbox_id": reservation["sandbox_id"],
                "container_id": reservation["container_id"],
                "generation": int(reservation["generation"]),
                "activation_generation": int(reservation["activation_generation"]),
                "running": True,
                "group_busy": False,
                "terminal_count": 0,
                "operation_count": 0,
                "workspace_suspended": False,
                "idle_deadline_monotonic": None,
                "resource_usage": None,
                "shared_live": True,
                "checkpoint": None,
                "members": [{"thread_id": reservation["thread_id"], **member}],
            }

    def update(
        self, sandbox_id: str, *, member: dict[str, Any] | None = None, **group: Any
    ) -> None:
        with self.lock:
            row = self.groups[sandbox_id]
            row.update(group)
            if member is not None:
                row["members"][0].update(member)


# ------------------------------------------------------------------ driver
class Driver:
    """One host, one chat, three turns: the scripted §3.4 steps."""

    def __init__(self, base_url: str, enroll_token: str, manager: FakeSandboxManager, wire: Wire):
        self.base = base_url
        self.enroll_token = enroll_token
        self.manager = manager
        self.wire = wire
        self.identity: Any = None
        self.lost: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.failures: list[str] = []

    # -- the controller ---------------------------------------------------
    def control_plane(self, op: str, **arguments: Any) -> dict[str, Any]:
        print("CALL " + json.dumps({"op": op, **arguments}), flush=True)
        answer = json.loads(sys.stdin.readline())
        if not answer.get("ok"):
            raise AssertionError(f"control plane could not {op}: {answer.get('error')}")
        return answer["result"]

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(f"[{self.wire.step}] {message}")

    # -- host surface -----------------------------------------------------
    def beat(self) -> Any:
        lost: list[dict[str, Any]] = []
        stops: list[dict[str, Any]] = []
        rows = retained_rows()
        self.identity = enrollment.heartbeat(
            base_url=self.base,
            identity=self.identity,
            slots_total=2,
            sandbox_manager_url=self.manager.url,
            capabilities=_CAPABILITIES,
            runner_version="mixed-protocol",
            sandbox_runtime="",
            sandbox_image_digest=_IMAGE,
            runtime_versions=dict(shared_protocol.SHARED_SANDBOX_RUNTIME_PINS),
            retained_sandboxes=rows,
            on_residencies_lost=lost.extend,
            on_retained_sandboxes_stop=stops.extend,
        )
        self.observations.append(
            {"step": self.wire.step, "rows": len(rows or []), "lost": lost, "stops": stops}
        )
        self.check(rows is not None, "the heartbeat-row builder could not read the sandbox manager")
        self.check(self.identity.status == "online", f"host status {self.identity.status!r}")
        return lost, stops

    def enroll(self) -> None:
        self.wire.step = "1 register"
        self.identity = enrollment.register(
            base_url=self.base,
            host=_HOST,
            enroll_token=self.enroll_token,
            sandbox_manager_url=self.manager.url,
            slots_total=2,
            capabilities=_CAPABILITIES,
            runner_version="mixed-protocol",
        )
        self.check(self.identity.host == _HOST, f"registered as {self.identity.host!r}")
        self.wire.step = "1 heartbeat"
        self.beat()

    def claim(self, step: str) -> ClaimedJob:
        self.wire.step = step
        body = host_control.claim_payload(
            base_url=self.base,
            host_credential=self.identity.credential,
            worker_id="mixed-protocol-runner",
            lease_ttl=_LEASE_TTL_S,
            attestation=_ATTESTATION,
        )
        if body is None:
            raise AssertionError("the claim found no turn")
        return ClaimedJob.from_response(body)

    def start(
        self, job: ClaimedJob, context_status: str
    ) -> tuple[ControlPlane, dict[str, Any], int]:
        """Reserve, run, establish the residency, record the process and admit (step 3)."""
        control = ControlPlane(self.base, job.worker_token)
        control.job_id = job.job_id
        retained = job.thread_sandbox or {}
        sandbox_id = str(retained.get("sandbox_id") or f"asbx_mixed_{uuid.uuid4().hex[:16]}")
        self.wire.step = "3 reserve"
        reservation = control.reserve_chat_sandbox(
            {
                "sandbox_id": sandbox_id,
                "container_name": str(retained.get("container_name") or f"mixed-{sandbox_id}"),
                "runtime_id": str(retained.get("runtime_id") or job.runtime),
                "runtime_version": retained.get("runtime_version") or job.runtime_version,
                "sandbox_image_digest": retained.get("sandbox_image_digest") or _IMAGE,
                "execution_profile": job.execution_profile,
                "host": _HOST,
                "shared_live": True,
                "resident_slot_capacity": 8,
                "memory_limit": "4g",
                "cpu_limit": "2",
                "disk_limit_mib": 0,
                "stop_timeout_seconds": 10,
                "fork_helper_version": None
                if retained
                else dict(shared_protocol.SHARED_SANDBOX_HELPER_PINS).get("claude-session-fork"),
            }
        )
        self.check(
            (reservation.get("memory_limit"), reservation.get("cpu_limit")) == ("4g", "2"),
            f"reservation did not confirm the limits: {reservation}",
        )
        group = {
            "sandbox_generation": int(reservation["generation"]),
            "activation_generation": int(reservation["activation_generation"]),
        }
        container_id = str(reservation.get("container_id") or f"c{uuid.uuid4().hex}")
        self.wire.step = "3 running"
        control.mark_chat_sandbox_running(
            **group, container_id=container_id, context_status=context_status
        )
        # §3.4 step 8: the workspace phase that freezes the owner's terminals,
        # which the control plane passes on to the sandbox manager directly.
        self.wire.step = "8 terminals suspend"
        control.suspend_terminals("workspace_preparing")
        self.wire.step = "8 terminals resume"
        control.resume_terminals()
        self.wire.step = "3 residency"
        residency = control.establish_shared_residency(
            sandbox_id=sandbox_id,
            runtime_id=str(reservation["runtime_id"]),
            runtime_version=reservation.get("runtime_version"),
        )
        require_residency_connection(job, residency)
        generation = int(residency["generation"])
        process_generation = int(reservation.get("process_generation") or 0) + 1
        self.wire.step = "3 process"
        control.record_chat_process(
            **group,
            residency_generation=generation,
            process_generation=process_generation,
            continuity=(None, context_status),
        )
        self.wire.step = "3 admit"
        control.admit_chat_turn(**group, residency_generation=generation)
        self.manager.hold(
            {**reservation, "thread_id": job.thread_id, "container_id": container_id},
            native_state="ready",
            context_status=context_status,
            activation_generation=group["activation_generation"],
            process_generation=process_generation,
            residency_generation=generation,
            resident_state="live",
        )
        return control, {**group, "sandbox_id": sandbox_id}, generation

    def work(self, control: ControlPlane, *, cancel: str | None = None) -> None:
        """Report an event and renew the lease; with ``cancel``, see it (step 4)."""
        self.wire.step = "4 event"
        control.append_event(NormalizedEvent("message", {"text": "mixed protocol"}))
        control.flush()
        if cancel is not None:
            self.control_plane("cancel", job_id=cancel)
        self.wire.step = "4 attempt heartbeat"
        beat = control.heartbeat(_LEASE_TTL_S)
        self.check(
            bool(beat.get("cancel_requested")) is (cancel is not None),
            f"cancel_requested={beat.get('cancel_requested')!r}",
        )

    def settle(
        self,
        job: ClaimedJob,
        control: ControlPlane,
        group: dict[str, Any],
        generation: int,
        context_status: str,
        idle_timeout: float,
        state: str,
    ) -> None:
        """Settle and finish (step 5)."""
        self.wire.step = "5 settle"
        control.settle_chat_sandbox(
            sandbox_generation=group["sandbox_generation"],
            activation_generation=group["activation_generation"],
            residency_generation=generation,
            context_status=context_status,
            native_session_id=f"mixed-session-{job.thread_id}",
            idle_timeout_seconds=idle_timeout,
            # The commit the runner resolved the workspace to.
            base_sha=_BASE_SHA,
        )
        self.wire.step = "5 finish"
        control.finish(state)
        control.close()

    def run(self) -> None:
        self.enroll()

        # Turn 1: a new chat, settled and finished.
        job = self.claim("2 claim new chat")
        self.check(job.thread_sandbox is None, "a new chat's claim carried a retained group")
        control, group, generation = self.start(job, "new")
        self.work(control)
        self.settle(job, control, group, generation, "new", _IDLE_TIMEOUT_S, "succeeded")
        self.control_plane("snapshot", label="after the first turn")

        # Step 6, first half: a beat with the group running renews its residency.
        self.wire.step = "6 heartbeat retained"
        lost, stops = self.beat()
        self.check(not lost and not stops, f"a live idle group was told lost={lost} stops={stops}")

        # Step 7: an evicted residency comes back lost, exactly that generation.
        self.control_plane(
            "release_residency", thread_id=job.thread_id, generation=generation, host=_HOST
        )
        self.wire.step = "7 heartbeat evicted"
        lost, _ = self.beat()
        self.check(
            [(item.get("thread_id"), item.get("generation")) for item in lost]
            == [(job.thread_id, generation)],
            f"residencies_lost={lost}",
        )
        # What the host does with the verdict, only once it has one: that
        # member's process goes.
        if any(item.get("thread_id") == job.thread_id for item in lost):
            self.manager.update(
                group["sandbox_id"], member={"resident_state": "stopped", "residency_generation": 0}
            )
        self.wire.step = "7 heartbeat after eviction"
        lost, _ = self.beat()
        self.check(not lost, f"an evicted member was named again: {lost}")
        self.control_plane("snapshot", label="after the eviction")

        # Turn 2: a follow-up on the live group; the owner cancels it.
        follow_up = self.control_plane(
            "follow_up", parent_job_id=job.job_id, prompt="Mixed protocol: second turn"
        )
        second = self.claim("2 claim follow-up")
        self.check(second.job_id == follow_up["job_id"], "the claim was not the follow-up")
        self.check(second.thread_sandbox is not None, "the follow-up's claim carried no group")
        control, group, generation = self.start(second, "live")
        self.work(control, cancel=second.job_id)
        self.settle(second, control, group, generation, "live", _SHORT_IDLE_TIMEOUT_S, "cancelled")

        # Step 6, second half: the idle group's stop is due; the host stops it
        # and the next beat reports it stopped.
        time.sleep(_SHORT_IDLE_TIMEOUT_S * 3)
        self.wire.step = "6 heartbeat idle"
        _, stops = self.beat()
        self.check(
            [(item.get("thread_id"), item.get("reason")) for item in stops]
            == [(second.thread_id, "idle")],
            f"retained_sandboxes_stop={stops}",
        )
        # The host stops the group only on the control plane's word.
        if any(item.get("thread_id") == second.thread_id for item in stops):
            self.manager.update(
                group["sandbox_id"], running=False, member={"resident_state": "stopped"}
            )
        self.wire.step = "6 heartbeat stopped"
        _, stops = self.beat()
        self.check(not stops, f"a stopped group was told to stop again: {stops}")
        self.control_plane("snapshot", label="after the idle stop")

        # Turn 3: a follow-up restarts the stopped group, and the turn fails
        # before admission: abandon, then finish.
        self.control_plane(
            "follow_up", parent_job_id=second.job_id, prompt="Mixed protocol: third turn"
        )
        third = self.claim("2 claim follow-up of a stopped group")
        control = ControlPlane(self.base, third.worker_token)
        control.job_id = third.job_id
        retained = third.thread_sandbox or {}
        self.wire.step = "3 reserve"
        reservation = control.reserve_chat_sandbox(
            {
                "sandbox_id": retained.get("sandbox_id"),
                "container_name": retained.get("container_name"),
                "runtime_id": retained.get("runtime_id"),
                "runtime_version": retained.get("runtime_version"),
                "sandbox_image_digest": retained.get("sandbox_image_digest"),
                "execution_profile": third.execution_profile,
                "host": _HOST,
                "shared_live": True,
                "resident_slot_capacity": 8,
                "memory_limit": "4g",
                "cpu_limit": "2",
                "disk_limit_mib": 0,
                "stop_timeout_seconds": 10,
            }
        )
        self.wire.step = "5 abandon"
        control.abandon_chat_sandbox(
            sandbox_generation=int(reservation["generation"]),
            activation_generation=int(reservation["activation_generation"]),
            lost=False,
            detail="mixed protocol: the container did not start",
            idle_timeout_seconds=_IDLE_TIMEOUT_S,
        )
        self.wire.step = "5 finish"
        control.finish("failed", detail="mixed protocol: the container did not start")
        control.close()
        self.wire.step = "6 heartbeat final"
        self.beat()


def main() -> None:
    base_url, enroll_token = sys.argv[2], sys.argv[3]
    wire = Wire()
    proxy = _proxy(base_url.rstrip("/"), wire)
    manager = FakeSandboxManager(os.environ["AGENT_SANDBOX_MANAGER_TOKEN"])
    os.environ["AGENT_SANDBOX_MANAGER_URL"] = manager.url
    driver = Driver(f"http://127.0.0.1:{proxy.server_address[1]}", enroll_token, manager, wire)
    error = None
    try:
        driver.run()
    except Exception:
        error = traceback.format_exc()
    report = {
        "error": error,
        "failures": driver.failures,
        "calls": wire.calls,
        "observations": driver.observations,
        "sandbox_manager": manager.calls,
    }
    print("DONE " + json.dumps(report, default=str), flush=True)


if __name__ == "__main__":
    main()
