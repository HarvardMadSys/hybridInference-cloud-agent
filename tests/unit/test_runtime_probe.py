"""The check a downloaded agent CLI must pass before an admin can select it.

``runtime_probe`` starts a networkless container whose main process is the
one-answer model in ``runtime_probe_model``, runs one real turn of the CLI
against it, and decides whether the version may be offered. Only
``tests/external/test_runtime_updates_live.py`` runs it end to end, and CI does
not run that file, so this one pins each part without Docker:

* the model server, started the way the probe container starts it (its source
  through ``python3 -I -c``), answers each dialect with a stream a client can
  read to the end, carrying the reply the verdict looks for;
* the verdict: which turns pass, and which are refused and why;
* the container and the member process get no network, no capabilities, and no
  credential on a command line;
* the steps before the turn refuse a binary that is not the requested version,
  and the probe as a whole never raises and never outlives its deadline.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import pytest

from cloud_agent_host import runtime_probe
from cloud_agent_host.drivers import TurnResult
from cloud_agent_host.runtime_probe import PROBE_MODEL_PORT, ProbeResult, probe_runtime
from cloud_agent_host.runtime_probe_model import MODEL, REPLY
from cloud_agent_host.runtimes.events import MESSAGE, RAW, NormalizedEvent
from cloud_agent_host.sandbox import RUNTIME_BIN_ROOT, SANDBOX_GID

if TYPE_CHECKING:
    from collections.abc import Iterator

VERSION = "2.1.300"
MEMBER = f"11001:{SANDBOX_GID}"
BINARY = f"{RUNTIME_BIN_ROOT}/claude-code/{VERSION}/bin/claude"


# --- The model server -------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def model(tmp_path_factory: pytest.TempPathFactory) -> Iterator[httpx.Client]:
    """The model server as the probe container runs it, and a client of it."""
    port = _free_port()
    # The same argv the container's entrypoint gets: isolated mode, the source
    # inline, the port as the only argument. An import outside the standard
    # library, or a dependency on the file's location, fails here.
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", runtime_probe._model_source(), str(port)],
        cwd=tmp_path_factory.mktemp("probe-model"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    stderr = process.stderr.read().decode() if process.stderr else ""
                    pytest.fail(f"the probe model did not start: {stderr}")
                time.sleep(0.05)
        # One keep-alive connection for every request, as a CLI holds one: a
        # wrong content-length would hang or corrupt the next answer.
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            yield client
    finally:
        process.kill()
        process.wait(timeout=5)


def _events(body: str) -> list[tuple[str | None, Any]]:
    """Each server-sent event as ``(event name, decoded data)``."""
    frames = []
    assert body.endswith("\n\n"), "the stream must end on a complete event"
    for frame in body.split("\n\n")[:-1]:
        name, data = None, None
        for line in frame.split("\n"):
            field, _, value = line.partition(": ")
            if field == "event":
                name = value
            elif field == "data":
                data = value if value == "[DONE]" else json.loads(value)
            else:
                pytest.fail(f"unexpected line in a server-sent event: {line!r}")
        frames.append((name, data))
    return frames


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages",
        # What Claude Code sends: a query string and the version prefix.
        "/v1/messages?beta=true",
        "/messages/",
    ],
)
def test_anthropic_stream_reads_to_one_settled_answer(model: httpx.Client, path: str) -> None:
    response = model.post(path, json={"model": MODEL, "stream": True, "messages": []})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    events = _events(response.text)
    # Every frame names its type twice, as the Messages API does; SDKs differ in
    # which of the two they read.
    assert all(name == data["type"] for name, data in events)
    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    text = "".join(data["delta"]["text"] for name, data in events if name == "content_block_delta")
    assert text == REPLY
    assert events[0][1]["message"]["model"] == MODEL
    assert events[4][1]["delta"]["stop_reason"] == "end_turn"


def test_anthropic_without_stream_answers_one_message(model: httpx.Client) -> None:
    response = model.post("/v1/messages", json={"model": MODEL})
    assert response.headers["content-type"] == "application/json"
    message = response.json()
    assert message["content"] == [{"type": "text", "text": REPLY}]
    assert message["stop_reason"] == "end_turn"


def test_responses_stream_numbers_its_events_and_completes(model: httpx.Client) -> None:
    response = model.post("/v1/responses", json={"model": MODEL, "stream": True})
    events = _events(response.text)
    assert all(name == data["type"] for name, data in events)
    assert [data["sequence_number"] for _, data in events] == list(range(len(events)))
    assert events[0][0] == "response.created"
    assert events[0][1]["response"]["status"] == "in_progress"
    assert events[-1][0] == "response.completed"
    completed = events[-1][1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0] == {
        "type": "output_text",
        "text": REPLY,
        "annotations": [],
    }


def test_responses_without_stream_answers_the_completed_response(model: httpx.Client) -> None:
    response = model.post("/v1/responses", json={"model": MODEL, "stream": False})
    assert response.json()["output"][0]["content"][0]["text"] == REPLY


def test_chat_stream_finishes_reports_usage_and_ends_with_done(model: httpx.Client) -> None:
    response = model.post("/v1/chat/completions", json={"model": MODEL, "stream": True})
    events = _events(response.text)
    # Chat completions name no events; the stream's end is the literal [DONE].
    assert all(name is None for name, _ in events)
    assert events[-1] == (None, "[DONE]")
    chunks = [data for _, data in events[:-1]]
    text = "".join(
        choice["delta"].get("content", "") for chunk in chunks for choice in chunk["choices"]
    )
    assert text == REPLY
    finishes = [c["finish_reason"] for chunk in chunks for c in chunk["choices"]]
    assert finishes[-1] == "stop" and finishes.count("stop") == 1
    # A client asking for usage gets it in a last chunk with no choices.
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] == 2


def test_chat_without_stream_answers_one_choice(model: httpx.Client) -> None:
    completion = model.post("/v1/chat/completions", json={"model": MODEL}).json()
    assert completion["choices"][0]["message"] == {"role": "assistant", "content": REPLY}
    assert completion["choices"][0]["finish_reason"] == "stop"


def test_what_a_cli_asks_before_a_turn_is_answered(model: httpx.Client) -> None:
    listing = model.get("/v1/models").json()
    assert [entry["id"] for entry in listing["data"]] == [MODEL]
    assert model.get("/models/?limit=5").status_code == 200
    assert model.head("/").status_code == 200
    counted = model.post("/v1/messages/count_tokens", json={"model": MODEL})
    assert counted.json() == {"input_tokens": 1}


def test_a_body_that_is_not_json_still_gets_an_answer(model: httpx.Client) -> None:
    response = model.post(
        "/v1/messages", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == REPLY
    # And the connection still serves the next request.
    assert model.get("/v1/models").status_code == 200


@pytest.mark.parametrize(("method", "path"), [("GET", "/v1/messages"), ("POST", "/v1/embeddings")])
def test_an_unknown_path_is_not_found(model: httpx.Client, method: str, path: str) -> None:
    response = model.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 404
    assert response.json() == {"error": {"message": "not found"}}


# --- The container and the member process ---------------------------------


def test_the_probe_container_has_no_network_and_no_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "container-id\n", "")

    monkeypatch.setattr(runtime_probe.subprocess, "run", run)
    box = runtime_probe._Container("docker", "agent-image:candidate", "probe=abc")
    assert box.id == "container-id"
    argv = calls[0]
    flags = list(pairwise(argv))
    assert ("--network", "none") in flags
    assert ("--cap-drop", "ALL") in flags
    assert ("--security-opt", "no-new-privileges") in flags
    assert ("--label", "probe=abc") in flags
    # The model is the container's main process, so removing the container is
    # what ends every exec in it.
    assert ("--entrypoint", "python3") in flags
    image = argv.index("agent-image:candidate")
    assert argv[image + 1 :] == [
        "-I",
        "-c",
        runtime_probe._model_source(),
        str(PROBE_MODEL_PORT),
    ]
    box.run("true", user=MEMBER)
    assert calls[-1] == ["docker", "exec", "--user", MEMBER, "container-id", "true"]
    box.close()
    assert calls[-1] == ["docker", "rm", "-f", "container-id"]


def test_a_container_docker_refused_is_an_error_naming_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 125, "", "no such image\n"),
    )
    with pytest.raises(RuntimeError, match="no such image"):
        runtime_probe._Container("docker", "missing:image", "probe=abc")


def test_the_member_is_started_without_its_values_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        started.update(argv=argv, env=kwargs["env"])
        return SimpleNamespace(stderr=None)

    monkeypatch.setattr(runtime_probe.subprocess, "Popen", popen)
    box = SimpleNamespace(docker="docker", id="container-id")
    context = runtime_probe._context(box, "claude-code", VERSION)
    assert context.gateway_base_url == f"http://127.0.0.1:{PROBE_MODEL_PORT}"
    assert context.spawn_live is not None
    context.spawn_live(["claude", "--print"], {"ANTHROPIC_API_KEY": context.credential})

    argv = started["argv"]
    assert argv[:2] == ["docker", "exec"]
    assert ("--user", MEMBER) in pairwise(argv)
    assert argv[-3:] == ["container-id", "claude", "--print"]
    # Every variable is named on the command line and valued only in the
    # client's own environment, which `ps` on the host does not show.
    names = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--env"]
    assert "ANTHROPIC_API_KEY" in names and "PATH" in names
    assert all("=" not in name for name in names)
    assert context.credential not in " ".join(argv)
    env = started["env"]
    assert env["ANTHROPIC_API_KEY"] == context.credential
    # The selected version's directory leads PATH, so it answers before the
    # image's default.
    assert env["PATH"].split(":")[0] == f"{RUNTIME_BIN_ROOT}/claude-code/{VERSION}/bin"


# --- Preparing the member -------------------------------------------------


class FakeBox:
    """``docker exec`` answers, scripted by the first argument of each call."""

    def __init__(self, *, version_stdout: str = f"{VERSION} (Claude Code)\n") -> None:
        self.docker = "docker"
        self.id = "container-id"
        self.version_stdout = version_stdout
        self.version_stderr = ""
        self.listening = True
        #: First arguments whose exec fails, as a read-only root would.
        self.failing: set[str] = set()
        self.runs: list[tuple[tuple[str, ...], str]] = []

    def run(self, *args: str, user: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        self.runs.append((args, user))
        if args[0] in self.failing:
            return subprocess.CompletedProcess(args, 1, "", "Permission denied\n")
        if args[0] == BINARY:
            return subprocess.CompletedProcess(args, 0, self.version_stdout, self.version_stderr)
        if args[0] == "python3":
            return subprocess.CompletedProcess(args, 0 if self.listening else 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """``docker start``, succeeding; the list records each call."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime_probe.subprocess, "run", run)
    return calls


def test_a_prepared_member_runs_the_requested_version_as_the_member(
    started: list[list[str]],
) -> None:
    box = FakeBox()
    assert runtime_probe._prepare(box, "claude-code", VERSION) is None
    assert started == [["docker", "start", "container-id"]]
    # Everything inside the container runs as the member, never as root.
    assert {user for _, user in box.runs} == {MEMBER}
    assert (BINARY, "--version") in [args for args, _ in box.runs]


def test_a_version_printed_before_a_warning_on_stderr_is_accepted(
    started: list[list[str]],
) -> None:
    box = FakeBox(version_stdout="")
    box.version_stderr = f"{VERSION}\n"
    assert runtime_probe._prepare(box, "claude-code", VERSION) is None


@pytest.mark.parametrize(
    "reported",
    [
        # The image's default answering because the version's directory is
        # missing: the probe would otherwise pass a version that is not there.
        "2.1.200 (Claude Code)\n",
        f"{VERSION}-alpha.1\n",
        "",
    ],
)
def test_a_binary_that_is_not_the_requested_version_is_refused(
    started: list[list[str]], reported: str
) -> None:
    problem = runtime_probe._prepare(FakeBox(version_stdout=reported), "claude-code", VERSION)
    assert problem is not None and f"does not report version {VERSION}" in problem
    assert BINARY in problem


@pytest.mark.parametrize(
    "step, problem",
    [
        ("install", "could not prepare the probe member: Permission denied"),
        ("/bin/sh", "could not prepare the probe workspace: Permission denied"),
    ],
)
def test_a_member_that_cannot_be_laid_out_is_reported(
    started: list[list[str]], step: str, problem: str
) -> None:
    box = FakeBox()
    box.failing.add(step)
    assert runtime_probe._prepare(box, "claude-code", VERSION) == problem
    assert BINARY not in [args[0] for args, _ in box.runs]


def test_a_model_that_never_listens_is_reported(
    started: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_probe, "_MODEL_READY_S", 0.05)
    box = FakeBox()
    box.listening = False
    assert runtime_probe._prepare(box, "claude-code", VERSION) == "the probe model did not start"


def test_a_container_that_does_not_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "OCI runtime error\n"),
    )
    box = FakeBox()
    problem = runtime_probe._prepare(box, "claude-code", VERSION)
    assert problem == "the probe container did not start: OCI runtime error"
    assert box.runs == []


# --- The verdict on one turn ----------------------------------------------


class FakeTurn:
    def __init__(self, events: list[NormalizedEvent], result: TurnResult) -> None:
        self.events = events
        self.result = result

    def iter_events(self) -> Iterator[NormalizedEvent]:
        yield from self.events

    def wait(self) -> TurnResult:
        return self.result


class FakeSession:
    def __init__(self, turn: FakeTurn, *, alive: bool = True, stderr: str = "") -> None:
        self.turn = turn
        self.alive = alive
        self.stderr = stderr
        self.started_with: dict[str, Any] | None = None
        self.prompts: list[str] = []
        self.closed = False

    def ensure_started(self, **kwargs: Any) -> None:
        self.started_with = kwargs

    def process_alive(self) -> bool:
        return self.alive

    def stderr_text(self) -> str:
        return self.stderr

    def start_turn(self, turn_input: Any) -> FakeTurn:
        self.prompts.append(turn_input.prompt)
        assert turn_input.model == MODEL
        return self.turn

    def close(self) -> None:
        self.closed = True


def _answer(text: str = REPLY) -> NormalizedEvent:
    return NormalizedEvent(MESSAGE, {"text": text})


def _raw(reason: str) -> NormalizedEvent:
    return NormalizedEvent(RAW, {"text": "...", "reason": reason})


def _completed(final: str = REPLY) -> TurnResult:
    return TurnResult(status="completed", final_output=final, exit_code=0)


def _judge(
    monkeypatch: pytest.MonkeyPatch, session: FakeSession
) -> tuple[ProbeResult, FakeSession]:
    monkeypatch.setattr(
        runtime_probe, "get_driver", lambda _runtime: SimpleNamespace(launch_live=lambda _: session)
    )
    box = SimpleNamespace(docker="docker", id="container-id")
    return runtime_probe._turn(box, "claude-code", VERSION), session


def test_a_turn_that_completes_with_the_answer_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    result, session = _judge(monkeypatch, FakeSession(FakeTurn([_answer()], _completed())))
    assert result == ProbeResult(True)
    assert session.started_with == {"model": MODEL, "reasoning_effort": None}
    assert len(session.prompts) == 1
    assert session.closed


def test_an_unfamiliar_line_type_does_not_fail_a_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every dialect passes protocol acknowledgements through as raw rows (pi's
    # {"type": "response"} to each command); only an unreadable line fails.
    events = [_raw("unknown type 'response'"), _answer()]
    result, _ = _judge(monkeypatch, FakeSession(FakeTurn(events, _completed())))
    assert result.ok


@pytest.mark.parametrize("reason", ["not json", "not an object"])
def test_output_the_driver_cannot_read_fails_even_a_completed_turn(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    events = [_raw(reason), _answer()]
    result, session = _judge(monkeypatch, FakeSession(FakeTurn(events, _completed())))
    assert not result.ok
    assert "prints output this release cannot read" in result.detail and reason in result.detail
    assert session.closed


@pytest.mark.parametrize(
    "events, final",
    [
        # The turn settled, but no message event carried the answer: the chat
        # would show the turn end without what the model said.
        ([], REPLY),
        ([_answer("something else")], REPLY),
        # The message arrived, but the turn's result lost it.
        ([_answer()], "Done."),
    ],
)
def test_a_completed_turn_without_the_answer_fails(
    monkeypatch: pytest.MonkeyPatch, events: list[NormalizedEvent], final: str
) -> None:
    result, _ = _judge(monkeypatch, FakeSession(FakeTurn(events, _completed(final))))
    assert result == ProbeResult(
        False, f"claude-code {VERSION} completed a turn without the model's answer"
    )


def test_a_turn_that_does_not_complete_fails_with_what_the_cli_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = TurnResult(status="failed", final_output="", exit_code=1)
    session = FakeSession(FakeTurn([], failed), stderr="Error: unsupported event\n")
    result, _ = _judge(monkeypatch, session)
    assert result == ProbeResult(
        False, f"claude-code {VERSION} did not complete a turn: Error: unsupported event"
    )


def test_a_cli_that_exits_on_start_fails_without_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(FakeTurn([], _completed()), alive=False, stderr="segfault\n")
    result, _ = _judge(monkeypatch, session)
    assert result == ProbeResult(False, f"claude-code {VERSION} did not start: segfault")
    assert session.prompts == []
    assert session.closed


def test_a_runtime_without_a_live_transport_cannot_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_probe, "get_driver", lambda _runtime: SimpleNamespace())
    box = SimpleNamespace(docker="docker", id="container-id")
    result = runtime_probe._turn(box, "claude-code", VERSION)
    assert result == ProbeResult(False, "claude-code has no live session transport")


def test_a_session_is_closed_when_reading_the_turn_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeTurn):
        def iter_events(self) -> Iterator[NormalizedEvent]:
            raise OSError("pipe closed")

    session = FakeSession(Broken([], _completed()))
    with pytest.raises(OSError, match="pipe closed"):
        _judge(monkeypatch, session)
    assert session.closed


# --- The probe as a whole -------------------------------------------------


class FakeContainer:
    made: ClassVar[list[FakeContainer]] = []

    def __init__(self, docker: str, image: str, label: str) -> None:
        self.docker, self.image, self.label = docker, image, label
        self.id = "container-id"
        self.removed = threading.Event()
        FakeContainer.made.append(self)

    def close(self) -> None:
        self.removed.set()


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> list[FakeContainer]:
    FakeContainer.made = []
    monkeypatch.setattr(runtime_probe, "_Container", FakeContainer)
    monkeypatch.setattr(runtime_probe, "_prepare", lambda *_: None)
    return FakeContainer.made


def _probe(timeout_s: float = 5) -> ProbeResult:
    return probe_runtime(
        docker="docker",
        image="agent-image:candidate",
        runtime_id="claude-code",
        version=VERSION,
        label="probe=abc",
        timeout_s=timeout_s,
    )


def test_a_passing_probe_removes_its_container(
    container: list[FakeContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_probe, "_turn", lambda *_: ProbeResult(True))
    assert _probe() == ProbeResult(True)
    (box,) = container
    assert (box.image, box.label) == ("agent-image:candidate", "probe=abc")
    assert box.removed.is_set()


def test_a_failed_preparation_is_the_verdict_and_no_turn_runs(
    container: list[FakeContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_probe, "_prepare", lambda *_: "the probe model did not start")

    def turn(*_: Any) -> ProbeResult:
        raise AssertionError("no turn after a failed preparation")

    monkeypatch.setattr(runtime_probe, "_turn", turn)
    assert _probe() == ProbeResult(False, "the probe model did not start")
    assert container[0].removed.is_set()


def test_an_error_inside_the_probe_is_reported_not_raised(
    container: list[FakeContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    def turn(*_: Any) -> ProbeResult:
        raise ValueError("driver bug")

    monkeypatch.setattr(runtime_probe, "_turn", turn)
    assert _probe() == ProbeResult(False, f"claude-code {VERSION} probe error: driver bug")
    assert container[0].removed.is_set()


def test_a_hung_cli_is_ended_by_removing_its_container_at_the_deadline(
    container: list[FakeContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    def turn(box: FakeContainer, *_: Any) -> ProbeResult:
        # A real hung turn returns only when removing the container ends its
        # exec, and then as a turn that did not complete.
        assert box.removed.wait(10), "the probe never removed the container"
        return ProbeResult(False, "did not complete a turn: exit 137")

    monkeypatch.setattr(runtime_probe, "_turn", turn)
    began = time.monotonic()
    assert _probe(timeout_s=0.2) == ProbeResult(False, "did not complete a turn: exit 137")
    assert time.monotonic() - began < 5


def test_a_turn_that_outlives_its_removal_fails_as_not_finished(
    container: list[FakeContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def turn(*_: Any) -> ProbeResult:
        release.wait(10)
        return ProbeResult(True)

    class ShortJoin(threading.Thread):
        # The probe waits 30 s more after removing the container; a test need not.
        def join(self, timeout: float | None = None) -> None:
            super().join(min(timeout or 0.2, 0.2))

    monkeypatch.setattr(runtime_probe, "_turn", turn)
    monkeypatch.setattr(runtime_probe.threading, "Thread", ShortJoin)
    try:
        result = _probe(timeout_s=0.2)
    finally:
        release.set()
    assert result == ProbeResult(False, f"claude-code {VERSION} did not finish a turn in time")
    assert container[0].removed.is_set()


def test_a_container_that_cannot_be_created_is_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_: Any) -> None:
        raise RuntimeError("could not create the probe container: no space left on device")

    monkeypatch.setattr(runtime_probe, "_Container", refuse)
    assert _probe() == ProbeResult(
        False, "could not create the probe container: no space left on device"
    )


def test_the_model_source_is_the_module_the_tests_import() -> None:
    # The container runs the file's text, not the imported module; both must
    # be the same file for the tests above to describe what the probe runs.
    from cloud_agent_host import runtime_probe_model

    assert runtime_probe._model_source() == Path(runtime_probe_model.__file__).read_text(
        encoding="utf-8"
    )
