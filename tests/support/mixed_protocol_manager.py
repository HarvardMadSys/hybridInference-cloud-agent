"""Both ends of the sandbox manager wire for ``scripts/check_mixed_protocol.py``.

The control plane calls a host's sandbox manager directly, and the two can be
one release apart just as a host and a control plane can. Run with one
release's own interpreter and checkout, in one of two roles:

``serve TREE``
    That release's real sandbox manager app (``create_app``), on loopback, with
    the fake shared-session supervisor from that release's own
    ``tests/unit/test_agent_sandbox_manager.py`` behind it: no Docker, no
    container. Every request body is read again through the route's own model
    (``mixed_protocol_wire``) and every key it would drop is recorded. Prints
    ``READY {"port": ...}``, then on a ``report`` line on stdin prints
    ``REPORT`` and what it recorded.

``call TREE URL``
    That release's control-plane ``SandboxManagerClient`` making the calls the
    host protocol v2 design (§9.1) counts as commands or state: health, the
    inventory, a member's status, the group registration, a residency loss,
    an SSH-access push, and terminal suspend and resume. Interactive calls
    (files, terminal I/O, git, questions, Fork) and the deletion state machine
    are left out. Prints ``DONE`` and, per call, whether the client accepted
    the answer.

The fake supervisor, its member ids and the token are that release's tests'
own; a change to them keeps the old spelling working here until a release with
the new one has been published.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_mode, _tree = sys.argv[1], Path(sys.argv[2]).resolve()
# The release's own packages, and its own ``tests`` package for the fake.
sys.path[1:1] = [str(_tree / "apps" / "backend"), str(_tree)]

import cloud_agent_shared

if not Path(cloud_agent_shared.__file__).resolve().is_relative_to(_tree):
    raise SystemExit(f"{cloud_agent_shared.__file__} is not from {_tree}")

#: The token ``_app`` in the release's tests builds the sandbox manager with.
TOKEN = "sandbox-manager-secret"
#: The member the fake supervisor owns, and the group around it.
_THREAD = "athr_source"
_JOB = "ajob_mixed_protocol"


def _first(*names: str) -> Any:
    """The first ``module:attribute`` this release has (modules moved between releases)."""
    for name in names:
        module_name, _, attribute = name.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        if hasattr(module, attribute):
            return getattr(module, attribute)
    raise SystemExit(f"this release has none of {names}")


# -------------------------------------------------------------------- serve
async def serve() -> None:
    import uvicorn
    from mixed_protocol_wire import WireRecorder

    fakes = importlib.import_module("tests.unit.test_agent_sandbox_manager")
    if not Path(fakes.__file__).resolve().is_relative_to(_tree):
        raise SystemExit(f"{fakes.__file__} is not from {_tree}")

    class Supervisor(fakes._SharedSupervisor):  # type: ignore[name-defined,misc]
        """The release's fake, with what its tests never needed of it."""

        def residency_lost(self, thread_id: str, *, generation: int) -> Any:
            self.calls.append(("residency_lost", thread_id, generation))
            return fakes._descriptor(thread_id)

    supervisor = Supervisor()
    # An SSH push inspects the member's container first; a stopped one waits
    # for its next turn, which is the answer the wire needs.
    backend = supervisor.workspace_backend
    backend.spec.container_name = "freeinference-chat-shared"
    if isinstance(backend.manager, SimpleNamespace):
        backend.manager.inspect = lambda _name: None
    recorder = WireRecorder(fakes._app(supervisor))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(recorder, log_level="warning", access_log=False, lifespan="on")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
            raise SystemExit("the sandbox manager stopped before it started")
        await asyncio.sleep(0.05)
    print("READY " + json.dumps({"port": listener.getsockname()[1]}), flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            if line.strip() == "report":
                print("REPORT " + json.dumps({"calls": recorder.calls}), flush=True)
                break
    finally:
        server.should_exit = True
        await serving


# --------------------------------------------------------------------- call
def _group() -> dict[str, Any]:
    """The durable row of the fake's group, as the control plane registers it."""
    member = {
        "thread_id": _THREAD,
        "native_session_id": "native-source",
        "native_state": "ready",
        "context_status": "live",
        "activation_generation": 4,
        "process_generation": 2,
        "residency_generation": 7,
        "resident_state": "live",
        "resident_slot_id": 0,
        "last_settled_job_id": "ajob_source",
    }
    return {
        "thread_id": _THREAD,
        "origin_thread_id": _THREAD,
        "sandbox_id": "asbx_shared",
        "generation": 3,
        "host": "mixed-protocol-host",
        "container_id": "container-shared",
        "container_name": "freeinference-chat-shared",
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "sandbox_image_digest": "sha256:image",
        "execution_profile": "trusted-v1",
        "memory_limit": "4g",
        "cpu_limit": "2",
        "disk_limit_mib": 0,
        "stop_timeout_seconds": 10,
        "state": "running",
        "shared_live": True,
        "resident_slot_capacity": 8,
        "start_seq": 1,
        "group_members": [member],
    }


async def call(url: str) -> None:
    client_type = _first(
        "cloud_agent.sandbox_manager.sandbox_manager_client:SandboxManagerClient",
        "cloud_agent.sandbox_manager_client:SandboxManagerClient",
    )
    from cloud_agent_shared.ssh_access import SshAccess

    client = client_type(url, TOKEN)
    access = SshAccess.model_validate(
        {"revision": 1, "members": [{"thread_id": _THREAD, "slot_id": 0, "servers": []}]}
    )
    steps: list[tuple[str, Any]] = [
        ("health", lambda: client.health()),
        ("list_sandboxes", lambda: client.list_sandboxes()),
        ("list_sandboxes(thread_id)", lambda: client.list_sandboxes(thread_id=_THREAD)),
        ("shared_chat_sandbox_status", lambda: client.shared_chat_sandbox_status(_THREAD)),
        ("register_shared_sandbox", lambda: client.register_shared_sandbox(_group())),
        ("shared_residency_lost", lambda: client.shared_residency_lost(_THREAD, generation=7)),
        ("sync_ssh_access", lambda: client.sync_ssh_access(_THREAD, access)),
        (
            "suspend_terminals",
            lambda: client.suspend_terminals(_THREAD, lease_generation=1, job_id=_JOB),
        ),
        (
            "resume_terminals",
            lambda: client.resume_terminals(_THREAD, lease_generation=1, job_id=_JOB),
        ),
        ("resume_settled_terminals", lambda: client.resume_settled_terminals(_THREAD, job_id=_JOB)),
    ]
    results = []
    for name, step in steps:
        try:
            answer = await step()
        except Exception as exc:
            results.append({"call": name, "error": f"{type(exc).__name__}: {exc}"[:500]})
        else:
            results.append(
                {
                    "call": name,
                    "error": None,
                    "keys": sorted(answer) if isinstance(answer, dict) else None,
                }
            )
    print("DONE " + json.dumps({"calls": results}), flush=True)


if __name__ == "__main__":
    if _mode == "serve":
        asyncio.run(serve())
    elif _mode == "call":
        asyncio.run(call(sys.argv[3]))
    else:
        raise SystemExit(f"unknown role {_mode!r}")
