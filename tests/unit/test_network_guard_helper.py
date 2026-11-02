"""The long-lived host-rule helper, and the guard's steps on it.

The helper's program runs here under a real interpreter with a stand-in
``nft`` on ``PATH``, so its stdin protocol, its deadlines and its exit at EOF
are the real ones; only Docker is missing. The guard's use of it runs against
the model of nft in ``test_network_guard``: the long-lived helper's client is a
stand-in speaking the same protocol over real pipes, and it runs each step as
a disposable helper would, so both kinds of helper change the same model.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.unit.test_network_guard import (
    _IMAGE,
    _KINDS,
    _NET,
    _X,
    _Y,
    FakeNft,
    _hold,
    _in_thread,
    _network,
    _update,
)

from cloud_agent_host import network_guard, network_guard_helper
from cloud_agent_host.network_guard import (
    _NFT_BATCH,
    _NFT_SERVE,
    HostNetworkGuard,
    NetworkGuardError,
)
from cloud_agent_host.network_guard_helper import LongLivedHelper, serve_program
from cloud_agent_shared.egress import render_ruleset

if TYPE_CHECKING:
    from collections.abc import Callable

# ── the program, under a real interpreter ───────────────────────────────

#: A stand-in for ``nft``: it lists what ``<record>.kernel`` holds now,
#: appends what it is told to apply to ``<record>.applied``, refuses a
#: transaction that says so, hangs on a listing of ``hang`` and lists what its
#: own stdin holds for a listing of ``stdin``.
_STAND_IN_NFT = """\
import json
import os
import sys
import time

args = sys.argv[1:]
record = os.environ["STAND_IN_NFT"]
if args[:2] == ["-f", "-"]:
    text = sys.stdin.read()
    if "refuse" in text:
        sys.stderr.write("Error: Could not process rule")
        sys.exit(1)
    with open(record + ".applied", "a") as applied:
        applied.write(text)
elif args[-1] == "hang":
    with open(record + ".pid", "w") as pid:
        pid.write(str(os.getpid()))
    time.sleep(60)
elif args[-1] == "stdin":
    print(json.dumps({"nftables": [{"stdin": sys.stdin.read()}]}))
else:
    with open(record + ".kernel") as kernel:
        print(json.dumps({"nftables": [{"args": args, "kernel": kernel.read()}]}))
"""
#: Stands in for the helper's ``docker run`` command line, which the stand-in
#: client below runs as the program alone.
_ARGV = ["docker", "run", "the-helper"]
_READ = json.dumps({"reads": [["list", "tables"]]})


def _listings(result: subprocess.CompletedProcess[str]) -> list[Any]:
    return json.loads(result.stdout)["listings"]


def _program_helper(
    tmp_path: Path, *, idle_s: int = 60, request_s: int = 60
) -> tuple[LongLivedHelper, list[subprocess.Popen[bytes]], Path]:
    """A long-lived helper whose client is the real program, run here instead of in Docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "nft.py").write_text(_STAND_IN_NFT)
    nft = bin_dir / "nft"
    nft.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "nft.py"}" "$@"\n')
    nft.chmod(0o755)
    record = tmp_path / "nft"
    Path(f"{record}.kernel").write_text("v1")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STAND_IN_NFT": str(record),
    }
    program = serve_program(_NFT_BATCH, idle_s=idle_s, request_s=request_s)
    started: list[subprocess.Popen[bytes]] = []

    def spawn(argv: list[str]) -> subprocess.Popen[bytes]:
        assert argv == _ARGV
        started.append(
            subprocess.Popen(
                [sys.executable, "-c", program],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env=env,
            )
        )
        return started[-1]

    return LongLivedHelper(spawn), started, record


def test_one_process_answers_every_request_from_its_own_read_of_the_kernel(tmp_path) -> None:
    helper, started, record = _program_helper(tmp_path)
    install = json.dumps({"install": "add table inet t\n", "reads": []})
    try:
        first = helper.run(_ARGV, _READ, timeout=30, accept=_listings)
        Path(f"{record}.kernel").write_text("v2")
        second = helper.run(_ARGV, _READ, timeout=30, accept=_listings)
        installed = helper.run(_ARGV, install, timeout=30, accept=_listings)
    finally:
        helper.close()

    listed = ["-j", "list", "tables"]
    assert first == [{"nftables": [{"args": listed, "kernel": "v1"}]}]
    assert second == [{"nftables": [{"args": listed, "kernel": "v2"}]}], "answered from memory"
    assert installed == []
    assert Path(f"{record}.applied").read_text() == "add table inet t\n"
    assert len(started) == 1, "each request started a helper of its own"
    assert started[0].returncode == 0, "it ends when its stdin closes"


def test_nothing_a_step_starts_can_read_the_requests_meant_for_the_helper(tmp_path) -> None:
    """Its nft calls find their stdin at its end, as in a disposable helper that read its request."""
    helper, _started, _record = _program_helper(tmp_path)
    try:
        listed = helper.run(
            _ARGV, json.dumps({"reads": [["list", "stdin"]]}), timeout=10, accept=_listings
        )
    finally:
        helper.close()
    assert listed == [{"nftables": [{"stdin": ""}]}]


def test_a_failed_step_is_handed_back_and_its_helper_stopped(tmp_path) -> None:
    """The disposable helper runs it again and reports the failure; until then none is trusted."""
    helper, started, _record = _program_helper(tmp_path)
    refused = json.dumps({"install": "refuse\n", "reads": []})

    assert helper.run(_ARGV, refused, timeout=30, accept=_listings) is None

    assert started[0].poll() == 0, "the helper was left running"
    assert helper.run(_ARGV, _READ, timeout=30, accept=_listings) is None, "not cooling down"
    assert len(started) == 1


def test_an_idle_helper_ends_by_itself_and_the_next_step_starts_another(tmp_path) -> None:
    helper, started, _record = _program_helper(tmp_path, idle_s=1)
    try:
        assert helper.run(_ARGV, _READ, timeout=30, accept=_listings)
        assert started[0].wait(10) == 3, "it did not end its own wait"
        assert helper.run(_ARGV, _READ, timeout=30, accept=_listings), "an idle exit is no failure"
    finally:
        helper.close()
    assert len(started) == 2


def test_a_request_past_its_deadline_ends_the_helper_before_the_guard_gives_up(tmp_path) -> None:
    """Its own alarm ends it; in its container the nft call it waited on dies with it."""
    helper, started, record = _program_helper(tmp_path, request_s=1)
    hang = json.dumps({"reads": [["list", "hang"]]})
    began = time.monotonic()
    try:
        served = helper.run(_ARGV, hang, timeout=30, accept=_listings)
        took = time.monotonic() - began
    finally:
        # Outside a container nothing ends the orphaned stand-in for it.
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(Path(f"{record}.pid").read_text()), signal.SIGKILL)
    assert served is None
    assert took < 10, "the guard waited out its own timeout"
    assert started[0].returncode == 3


def test_an_interrupted_step_stops_its_helper(tmp_path) -> None:
    """An answer may still be on its way, so nothing after it may read that helper."""
    helper, started, _record = _program_helper(tmp_path)

    def interrupted(_result: subprocess.CompletedProcess[str]) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        helper.run(_ARGV, _READ, timeout=30, accept=interrupted)
    assert started[0].poll() == 0


#: The guard's process, here: it starts the helper with its stdin a pipe,
#: shares its own stdout with it, says so and waits to be killed.
_STARTER = """\
import subprocess
import sys
import time

helper = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1]], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
)
print(helper.pid, flush=True)
time.sleep(600)
"""


def _ends_within(stream: Any, seconds: float) -> bool:
    """Whether ``stream`` reaches its end within ``seconds``, whatever comes before it."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if select.select([stream], [], [], left)[0] and not os.read(stream.fileno(), 1 << 16):
            return True
    return False


def test_the_helper_ends_when_the_process_that_started_it_is_killed() -> None:
    """Even by SIGKILL: the kernel closes that process's end of the pipe, and the helper reads EOF.

    Docker carries the EOF on to a helper container: ``docker run -i`` sends
    it on, and the container's stdin is closed when the client's stream ends.
    """
    program = serve_program(_NFT_BATCH, idle_s=600, request_s=600)
    starter = subprocess.Popen([sys.executable, "-c", _STARTER, program], stdout=subprocess.PIPE)
    assert starter.stdout is not None
    helper = int(starter.stdout.readline())
    ended = False
    try:
        starter.kill()
        starter.wait(5)
        # The helper holds this pipe too, so it ends only once the helper has.
        ended = _ends_within(starter.stdout, 10)
    finally:
        if not ended:
            with contextlib.suppress(OSError):
                os.kill(helper, signal.SIGKILL)
        starter.stdout.close()
    assert ended, "the helper outlived the process that started it"


# ── the guard's steps on its long-lived helper ──────────────────────────

#: Appended to a step the stand-in runs, so the record tells its steps from a
#: disposable helper's; the model of nft ignores it.
_SERVED = "run-by-the-long-lived-helper"


class _Steps:
    """The guard's runner over the model of nft, recording which kind of helper ran each step."""

    def __init__(self, nft: FakeNft) -> None:
        self.nft = nft
        #: ("long-lived" or "disposable", the step's request), in order.
        self.ran: list[tuple[str, dict[str, Any]]] = []
        self._model = threading.Lock()

    def __call__(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        with self._model:
            if "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "python3":
                kind = "long-lived" if argv[-1] == _SERVED else "disposable"
                self.ran.append((kind, json.loads(stdin or "{}")))
            return self.nft(argv, stdin)

    def kinds(self) -> list[str]:
        return [kind for kind, _request in self.ran]


class _PipeEnd:
    """One end of a pipe, as the guard holds a client's stdin or stdout."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.closed = False

    def fileno(self) -> int:
        return self.fd

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self.fd)


class _StandIn:
    """The long-lived helper's client, here: the program's protocol over real pipes.

    Each request is the step a disposable helper would run, run through the
    guard's runner. What goes back is ``answer``'s to decide: ``None`` ends it
    without an answer.
    """

    def __init__(self, argv: list[str], run: Any, answer: Any) -> None:
        self.argv = argv
        self.requests: list[dict[str, Any]] = []
        self.returncode: int | None = None
        self.killed = False
        self.released = threading.Event()
        self._run, self._answer = run, answer
        requests, stdin = os.pipe()
        stdout, self._answers = os.pipe()
        self.stdin, self.stdout = _PipeEnd(stdin), _PipeEnd(stdout)
        self._thread = threading.Thread(target=self._serve, args=(requests,), daemon=True)
        self._thread.start()

    def step(self, frame: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        """Run a request as a disposable helper runs it."""
        return self._run([*self.argv[:-1], _NFT_BATCH, _SERVED], frame["stdin"])

    def blurt(self, data: bytes) -> None:
        """Write to the guard without being asked."""
        os.write(self._answers, data)

    def _serve(self, requests_fd: int) -> None:
        try:
            with open(requests_fd, "rb") as requests, open(self._answers, "wb", 0) as answers:
                for line in requests:
                    frame = json.loads(line)
                    self.requests.append(json.loads(frame["stdin"]))
                    reply = self._answer(self, frame)
                    if reply is None:
                        break
                    answers.write(reply)
        except BrokenPipeError:
            pass  # the guard stopped reading, as a client's write would find
        finally:
            self.returncode = -9 if self.killed else 0

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.released.set()


def _line(request_id: str, returncode: Any, stdout: Any, stderr: Any = "") -> bytes:
    answer = {"id": request_id, "returncode": returncode, "stdout": stdout, "stderr": stderr}
    return json.dumps(answer).encode() + b"\n"


def _answers(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
    """As the program answers: the step's status, output and errors, for this request."""
    result = stand_in.step(frame)
    return _line(frame["id"], result.returncode, result.stdout, result.stderr)


def _silent(_stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
    """Never answers, and ends when its stdin closes."""
    return b""


def _exits(_stand_in: _StandIn, _frame: dict[str, Any]) -> None:
    """Ends without answering."""
    return None


def _stuck(stand_in: _StandIn, _frame: dict[str, Any]) -> bytes:
    """Never answers, and ends only once killed."""
    stand_in.released.wait(10)
    return b""


def _long_lived_guard(
    *answers: Any,
) -> tuple[FakeNft, _Steps, HostNetworkGuard, list[_StandIn]]:
    """A guard whose helpers both run on the model; its first helpers answer as ``answers`` say."""
    nft = FakeNft()
    steps = _Steps(nft)
    started: list[_StandIn] = []

    def spawn(argv: list[str]) -> _StandIn:
        answer = answers[len(started)] if len(started) < len(answers) else _answers
        started.append(_StandIn(argv, lambda *step: guard._run(*step), answer))
        return started[-1]

    guard = HostNetworkGuard(_network(), helper_image=_IMAGE, run=steps, spawn=spawn)
    return nft, steps, guard, started


def test_every_step_runs_on_one_long_lived_helper_and_routes_stay_unprivileged() -> None:
    nft, steps, guard, started = _long_lived_guard()

    guard.ensure()
    guard.sync_relays(_X, {("10.240.0.3", 8080)})
    guard.sync_ssh_targets(_X, {("93.184.216.34", 22)})
    guard.recheck()

    assert len(started) == 1, "a helper container per step"
    assert steps.kinds() == ["long-lived"] * 4
    assert [request for _kind, request in steps.ran] == started[0].requests
    routes = [argv for argv, _ in nft.calls if argv[argv.index("--entrypoint") + 1] == "ip"]
    assert len(routes) == 1 and "--cap-add" not in routes[0], "the route read gained a capability"
    assert guard.relay_endpoints(_X) == {("10.240.0.3", 8080)}


def test_the_long_lived_helper_is_a_disposable_helper_flag_for_flag() -> None:
    """Today's flags, ``--interactive`` included as a disposable helper has it; only the program differs."""
    nft, _steps, guard, started = _long_lived_guard()
    guard.ensure()
    calls = len(nft.calls)
    HostNetworkGuard(_network(), helper_image=_IMAGE, run=nft).ensure()
    (step, _), (route, _) = nft.calls[calls:]

    flags = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--network",
        "host",
        "--runtime",
        "runc",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--user",
        "0:0",
    ]
    assert started[0].argv == [*flags, "--entrypoint", "python3", _IMAGE, "-c", _NFT_SERVE]
    assert step == [*flags, "--entrypoint", "python3", _IMAGE, "-c", _NFT_BATCH]
    without = {"--interactive", "--cap-add", "NET_ADMIN"}
    routes = ["-4", "-j", "route", "show", "table", "all"]
    assert route == [
        *(flag for flag in flags if flag not in without),
        "--entrypoint",
        "ip",
        _IMAGE,
        *routes,
    ]


def test_a_step_on_the_long_lived_helper_gets_a_disposable_helpers_whole_minute() -> None:
    """So the guard gives up on a helper that may still write no sooner than it ever did."""
    _nft, _steps, guard, _started = _long_lived_guard()
    assert guard._long_lived is not None
    serve = guard._long_lived.run
    timeouts: list[float] = []

    def run(*args: Any, timeout: float, **kwargs: Any) -> Any:
        timeouts.append(timeout)
        return serve(*args, timeout=timeout, **kwargs)

    guard._long_lived.run = run  # type: ignore[method-assign]

    guard.ensure()

    assert timeouts == [network_guard._HELPER_TIMEOUT_S] == [60]


@pytest.mark.parametrize("answer", [_silent, _exits, _stuck], ids=["silent", "exits", "stuck"])
def test_a_helper_that_does_not_answer_is_stopped_and_a_disposable_one_runs_its_step(
    monkeypatch, answer
) -> None:
    monkeypatch.setattr(network_guard, "_HELPER_TIMEOUT_S", 0.5)
    monkeypatch.setattr(network_guard_helper, "DISCARD_GRACE_S", 0.2)
    nft, steps, guard, started = _long_lived_guard(answer)

    guard.ensure()

    assert ("inet", guard.network.table) in nft.tables
    assert steps.kinds() == ["disposable"]
    assert started[0].requests == [steps.ran[0][1]], "not the same step run again"
    assert started[0].poll() is not None, "the helper was left running"
    assert started[0].killed is (answer is _stuck), "killed although closing its stdin ended it"


def _misanswering(make: Callable[[str, subprocess.CompletedProcess[str]], bytes]) -> Any:
    """A helper that runs each relay update, so the kernel changes, then answers as ``make`` says.

    A relay update's answer is a few hundred bytes, so even two of them reach
    the guard in one atomic pipe write.
    """

    def answer(stand_in: _StandIn, frame: dict[str, Any]) -> bytes:
        if "relays" not in json.loads(frame["stdin"]):
            return _answers(stand_in, frame)
        return make(frame["id"], stand_in.step(frame))

    return answer


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda _id, _done: b"{not json\n", id="not-json"),
        pytest.param(lambda _id, _done: b"[]\n", id="not-an-object"),
        pytest.param(lambda _id, done: _line("0" * 16, 0, done.stdout), id="another-request"),
        pytest.param(lambda id_, done: _line(id_, "0", done.stdout), id="status-as-text"),
        pytest.param(lambda id_, done: _line(id_, False, done.stdout), id="status-false"),
        pytest.param(lambda id_, _done: _line(id_, 0, ["listings"]), id="output-not-text"),
        pytest.param(lambda id_, done: _line(id_, 0, done.stdout, ["boom"]), id="errors-not-text"),
        pytest.param(lambda id_, done: _line(id_, 0, done.stdout) * 2, id="two-answers"),
        pytest.param(lambda id_, done: _line(id_, 0, done.stdout) + b"\n", id="a-line-after-it"),
        pytest.param(lambda id_, done: _line(id_, 1, done.stdout, "boom"), id="step-failed"),
        pytest.param(lambda id_, _done: _line(id_, 0, "not json"), id="unreadable-output"),
    ],
)
def test_any_answer_but_a_readable_success_runs_the_step_again_on_a_disposable_helper(
    make,
) -> None:
    """Even one whose output looks right: a status, its own request's id and one line, or nothing."""
    _nft, steps, guard, started = _long_lived_guard(_misanswering(make))
    guard.ensure()

    guard.sync_relays(_X, {("10.240.0.3", 8080)})

    assert steps.kinds() == ["long-lived", "long-lived", "disposable"]
    assert steps.ran[-2][1] == steps.ran[-1][1], "not the same step run again"
    assert started[0].poll() == 0 and not started[0].killed, "it should end at EOF on its stdin"
    assert guard.relay_endpoints(_X) == {("10.240.0.3", 8080)}


def test_a_step_that_fails_on_both_raises_as_a_failing_helper_always_did() -> None:
    nft, steps, guard, _started = _long_lived_guard()
    guard.ensure()
    del nft.table("bridge", guard.network.table)["sets"]["relays"]

    with pytest.raises(NetworkGuardError, match=r"^nft failed: No such file or directory"):
        guard.sync_relays(_X, {("10.240.0.3", 8080)})

    assert steps.kinds()[-2:] == ["long-lived", "disposable"]


def test_a_read_back_on_the_helper_that_installed_sees_the_kernel_change_since() -> None:
    """A failed check on its answer raises as before; nothing runs it again to hope for another."""
    nft, steps, guard, started = _long_lived_guard()
    guard.ensure()
    nft.table("inet", guard.network.table)["rules"]["from_group"] = 0  # a flushed chain

    with pytest.raises(NetworkGuardError, match="from_group"):
        guard._verify(render_ruleset(guard.network))

    assert steps.kinds() == ["long-lived", "long-lived"]
    assert len(started) == 1


def test_after_a_failure_every_step_runs_on_disposable_helpers_for_a_while(monkeypatch) -> None:
    """A helper that fails every time costs one failure per period, not one per step."""
    _nft, steps, guard, started = _long_lived_guard(_exits)
    guard.ensure()
    guard.recheck()
    assert steps.kinds() == ["disposable", "disposable"]
    assert len(started) == 1
    now = time.monotonic()
    monkeypatch.setattr(
        network_guard_helper.time, "monotonic", lambda: now + network_guard_helper.COOLDOWN_S
    )

    guard.relay_endpoints(_X)

    assert len(started) == 2
    assert steps.kinds()[-1] == "long-lived"


@pytest.mark.parametrize("limit", ["RETIRE_IDLE_S", "RETIRE_AGE_S"])
def test_a_helper_past_its_time_is_retired_and_a_new_one_takes_the_step(monkeypatch, limit) -> None:
    _nft, steps, guard, started = _long_lived_guard()
    guard.ensure()
    monkeypatch.setattr(network_guard_helper, limit, 0.0)

    guard.relay_endpoints(_X)

    assert len(started) == 2
    assert started[0].poll() == 0 and not started[0].killed
    assert steps.kinds() == ["long-lived", "long-lived"], "a retirement is no failure"


def test_output_a_helper_wrote_unasked_is_never_read_as_an_answer() -> None:
    _nft, steps, guard, started = _long_lived_guard()
    guard.ensure()
    started[0].blurt(_line("unasked", 0, json.dumps({"listings": [{"nftables": []}]})))

    guard.relay_endpoints(_X)

    assert len(started) == 2, "the helper that spoke unasked was used again"
    assert started[0].poll() == 0
    assert steps.kinds() == ["long-lived", "long-lived"]


def test_a_step_for_another_command_line_gets_a_helper_of_its_own() -> None:
    _nft, _steps, guard, started = _long_lived_guard()
    guard.ensure()
    image = "sha256:" + "b" * 64
    guard._helper_image = image

    guard.relay_endpoints(_X)

    assert [image in stand_in.argv for stand_in in started] == [False, True]
    assert started[0].poll() == 0


def test_closing_the_guard_ends_its_long_lived_helper_and_starts_no_other() -> None:
    _nft, steps, guard, started = _long_lived_guard()
    guard.ensure()

    guard.close()

    assert started[0].poll() == 0 and not started[0].killed, "it should end at EOF on its stdin"
    guard.ensure(max_age_s=0)
    assert len(started) == 1
    assert steps.kinds() == ["long-lived", "disposable"]


# ── who waits for whom, with the long-lived helper in use ────────────────


def _ssh_long_lived() -> tuple[_Steps, HostNetworkGuard, list[_StandIn]]:
    """A verified guard on its long-lived helper that wrote SSH targets for both bridges."""
    _nft, steps, guard, started = _long_lived_guard()
    guard.ensure()
    for bridge in (_X, _Y):
        guard.sync_ssh_targets(bridge, {("93.184.216.34", 22)}, network_id=_NET)
    return steps, guard, started


def test_a_step_that_finds_the_helper_busy_runs_beside_it_on_a_disposable_one() -> None:
    """Concurrent updates of different bridges do not queue behind the long-lived helper."""
    steps, guard, _started = _ssh_long_lived()
    helpers = _hold(guard)

    first = _in_thread(lambda: guard.sync_relays(_X, {("10.240.0.3", 8080)}))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    second = _in_thread(lambda: guard.sync_relays(_Y, {("10.240.0.11", 8080)}))
    try:
        assert helpers.wait_for(lambda: helpers.running[_Y] or helpers.parked(second))
        assert not helpers.parked(second), "an update waited for another bridge's helper"
        assert helpers.running[_X] == helpers.running[_Y] == 1
    finally:
        helpers.release()
    first.result(5)
    second.result(5)

    assert helpers.overlaps == []
    assert {(kind, request["relays"]["bridge"]) for kind, request in steps.ran[-2:]} == {
        ("long-lived", _X),
        ("disposable", _Y),
    }


@pytest.mark.parametrize("kind", _KINDS)
def test_an_install_still_waits_for_an_update_on_the_long_lived_helper(kind) -> None:
    steps, guard, _started = _ssh_long_lived()
    helpers = _hold(guard)

    update = _in_thread(lambda: _update(kind, guard, _X))
    assert helpers.wait_for(lambda: helpers.running[_X] == 1)
    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    try:
        assert helpers.wait_for(lambda: helpers.parked(install) or helpers.running["install"])
    finally:
        helpers.release()
    update.result(5)
    install.result(5)

    assert helpers.overlaps == [], "an install ran beside an update"
    assert helpers.started == [_X, "install", "routes"]
    assert steps.kinds()[-2:] == ["long-lived", "long-lived"]


@pytest.mark.parametrize("kind", _KINDS)
def test_an_update_waits_for_an_install_on_the_long_lived_helper_not_for_a_disposable_one(
    kind,
) -> None:
    """A busy long-lived helper sends a step to a disposable one only once its lock allows the step."""
    steps, guard, _started = _ssh_long_lived()
    helpers = _hold(guard)

    install = _in_thread(lambda: guard.ensure(max_age_s=0))
    assert helpers.wait_for(lambda: helpers.running["install"] == 1)
    update = _in_thread(lambda: _update(kind, guard, _X))
    try:
        assert helpers.wait_for(lambda: helpers.parked(update) or helpers.running[_X])
    finally:
        helpers.release()
    install.result(5)
    update.result(5)

    assert helpers.overlaps == [], "an update ran beside an install"
    assert helpers.started == ["install", "routes", _X]
    assert steps.kinds()[-2:] == ["long-lived", "long-lived"]
