"""One network proof per request, and Docker reads the supervisor does not repeat.

Each admitted request -- a turn's lease, an environment preparation that runs
a script, a workspace operation or a terminal -- proves its group's network
once, and the runner's commands stand on that proof. A preparation already
done for the running boot runs nothing and proves nothing. A keystroke reaches
a terminal that is already open without inspecting the container first.

The core module retains the shared fakes; this file keeps these cases
independently schedulable without extracting their fixture machinery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.unit.test_shared_session_supervisor import (
    ASGITransport,
    AsyncClient,
    ChatSandboxInspection,
    ProcessBackend,
    RetainedChatLost,
    SharedChatSessionSupervisor,
    SimpleNamespace,
    _Manager,
    _members,
    _spec,
    asdict,
    create_app,
    pytest,
    replace,
    threading,
    time,
)

from cloud_agent_host.sandbox import TerminalProcess
from cloud_agent_host.terminal_session import _TerminalSession

if TYPE_CHECKING:
    from cloud_agent_host.shared_session_supervisor import SharedSessionLease


def test_a_lease_claims_a_bridge_proof_only_for_a_call_that_made_one():
    """The runner's commands rely on it for their turn, so a repeat must not claim it."""
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())

    def acquire() -> SharedSessionLease:
        return supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )

    first = acquire()
    repeat = acquire()

    assert first.network_verified is True
    assert asdict(first)["network_verified"] is True, "what the sandbox manager endpoint sends"
    # The same owner is handed its existing lease without touching the group.
    assert repeat.token == first.token
    assert manager.starts == [_spec().container_name]
    assert repeat.network_verified is False


@pytest.mark.parametrize("entrypoint", ["acquire", "environment", "operation", "terminal"])
def test_a_request_after_a_failed_start_completion_is_proved_exactly_once(entrypoint: str) -> None:
    """Every admitted request gets one fresh network proof before it runs.

    A start that proved the network and then failed to complete its archive
    marker leaves the group running. A request that only retries that
    completion starts nothing, so the running container is proved as a start
    proves one; a request that starts it afterwards is proved by that start.
    """
    manager = _Manager()
    completions = 0

    def complete(_spec, inspection) -> None:
        nonlocal completions
        assert inspection.running is True
        completions += 1
        if completions == 1:
            raise RuntimeError("marker clear failed")

    supervisor = SharedChatSessionSupervisor(
        manager,
        starting_archive_prepare_hook=lambda *_args: None,
        started_archive_hook=complete,
    )
    supervisor.register_manifest(_spec(), _members())
    manager.stop(_spec(), release_networks=False)
    with pytest.raises(RuntimeError, match="marker clear failed"):
        supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:1",
            activation_generation=1,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )
    assert manager.items[_spec().container_name].running is True
    manager.starts.clear()

    if entrypoint == "acquire":
        lease = supervisor.acquire(
            _spec(),
            thread_id="athr_source",
            owner="attempt:2",
            activation_generation=2,
            has_settled_context=True,
            manifest=_members(),
            expected_container_id="container-shared",
        )
        assert lease.network_verified is True, "the runner's commands stand on this proof"
    elif entrypoint == "environment":
        ready = {"ready": True, "container_id": "container-shared", "boot_id": ""}
        preparer = SimpleNamespace(status=lambda _backend, _binding_id: ready)
        supervisor.prepare_environment(
            "athr_source", SimpleNamespace(binding_id="binding"), preparer
        )
    else:
        token, _ = getattr(supervisor, f"{entrypoint}_opened")("athr_source")
        getattr(supervisor, f"{entrypoint}_closed")("athr_source", token)

    assert completions == 2
    assert len(manager.starts) + len(manager.network_proofs) == 1
    if entrypoint in {"acquire", "environment"}:
        # Their admission only retried the completion and started nothing, so
        # the running container was proved on its own.
        assert manager.network_proofs == [_spec().container_name]


class _Shell(TerminalProcess):
    """An open interactive shell that records what the owner typed."""

    def __init__(self) -> None:
        self.inputs: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self._killed = threading.Event()

    def chunks(self):
        self._killed.wait(timeout=5)
        yield from ()

    def write(self, data: bytes) -> None:
        self.inputs.append(data)

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def poll(self) -> int | None:
        return 0 if self._killed.is_set() else None

    def kill(self) -> None:
        self._killed.set()

    def wait(self) -> int:
        return 0


def _counting_inspections(manager: _Manager) -> list[str]:
    inspected: list[str] = []
    inspect = manager.inspect

    def counting(container_name: str) -> ChatSandboxInspection | None:
        inspected.append(container_name)
        return inspect(container_name)

    manager.inspect = counting  # type: ignore[method-assign]
    return inspected


@pytest.mark.asyncio
async def test_terminal_keystrokes_reach_the_shell_without_inspecting_the_container():
    """The browser sends about one input per keystroke, and each inspected the container first.

    That is a Docker CLI process in front of every echo. A keystroke reaches
    only a terminal already open, so it needs only the member's group, from
    memory. A container that died, or that its group lost, ended its
    terminals with EOF, and they refuse the keystroke themselves. Listing the
    terminals still observes the container.
    """
    manager = _Manager()
    supervisor = SharedChatSessionSupervisor(manager)
    supervisor.register_manifest(_spec(), _members())
    inspected = _counting_inspections(manager)
    app = create_app(
        backend=ProcessBackend(acknowledged_unsafe=True),
        token="sandbox-manager-secret",
        shared_session_supervisor=supervisor,
    )
    shell = _Shell()
    session = _TerminalSession(
        terminal_id="term_shell",
        process=shell,
        workdir=None,
        rows=24,
        cols=80,
        now=time.monotonic(),
    )
    session.start()
    app.state.terminal_manager._sessions["athr_source"] = {session.id: session}
    headers = {"X-Agent-Workspace-Token": "sandbox-manager-secret"}
    # A Fork member's alias: the terminal lives in its group's origin namespace.
    terminal = "/workspaces/athr_peer/terminals/term_shell"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://sandbox_manager"
    ) as client:
        wrote = await client.post(f"{terminal}/input", json={"data": "bHMK"}, headers=headers)
        resized = await client.post(
            f"{terminal}/resize", json={"rows": 30, "cols": 100}, headers=headers
        )
        stranger = await client.post(
            "/workspaces/athr_stranger/terminals/term_shell/input",
            json={"data": "bHMK"},
            headers=headers,
        )
        inspected_by_keystrokes = list(inspected)
        listed = await client.get("/workspaces/athr_peer/terminals", headers=headers)
        inspected_by_listing = inspected[len(inspected_by_keystrokes) :]
        original = manager.items[_spec().container_name]
        manager.items[_spec().container_name] = replace(
            original, container_id="container-replacement"
        )
        with pytest.raises(RetainedChatLost):
            supervisor.status("athr_source")
        # The original container is gone, and its Docker exec with it.
        shell.kill()
        deadline = time.monotonic() + 5
        while not session.has_exited and time.monotonic() < deadline:
            time.sleep(0.01)
        lost = await client.post(f"{terminal}/input", json={"data": "bHMK"}, headers=headers)
    session.close(mark_activity=False)

    assert (wrote.status_code, resized.status_code) == (200, 200)
    assert shell.inputs == [b"ls\n"]
    assert shell.resizes == [(30, 100)]
    assert stranger.status_code == 404
    assert stranger.json()["detail"] == "shared chat workspace is not materialized"
    assert inspected_by_keystrokes == []
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["terminals"]] == ["term_shell"]
    assert inspected_by_listing == [_spec().container_name]
    assert lost.status_code == 409
    assert lost.json()["detail"] == "terminal is not running"
