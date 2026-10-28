"""What a member's UID does before its CLI starts runs in one exec, unchanged.

The reap of its slot, the removal of stale pidfiles and credentials, the
provider home's preparation and Claude's hook merge were one ``docker exec``
each. They now run, in the order they ran, as processes of one host-supplied
runner (``cloud_agent_host.member_preparation``). The runner tests below run
it for real on this host, as the exec would run it in the guest; the launch
tests read the queue each exec carries.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import resident_transport
from cloud_agent_host.chat_sandbox import (
    MEMBER_REAPER_TARGET,
    MEMBER_UID_BASE,
    ChatSandboxManager,
    ChatSandboxSpec,
    claude_member_config_dir,
)
from cloud_agent_host.chat_sandbox_inspect import container_inspection
from cloud_agent_host.drivers import DriverError
from cloud_agent_host.member_preparation import (
    RUNNER,
    TOOL_HOOKS_SCRIPT,
    MemberPreparation,
    MemberStep,
)
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransportFactory
from cloud_agent_host.sandbox import ContainerBackend, SandboxError
from cloud_agent_shared.environment import BoundEnvironment, EnvironmentVariable

_SPEC = ChatSandboxSpec(
    thread_id="athr_member",
    sandbox_id="asbx_group",
    generation=1,
    runtime_id="claude-code",
    shared_live=True,
    resident_slot_capacity=4,
)


class _LocalExec:
    """Runs each exec on this host, as the guest would: the runner, or one command.

    ``environment`` is what ``docker exec`` would hand the process.
    """

    def __init__(self, environment: dict[str, str] | None = None) -> None:
        self.environment = environment
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.reaper_ensured = 0

    def exec_capture(self, _spec, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return subprocess.run(
            argv,
            input=kwargs.get("input_bytes"),
            capture_output=True,
            cwd=kwargs["workdir"],
            env=self.environment,
            timeout=kwargs["timeout_s"],
            check=False,
        )

    def ensure_member_process_reaper(self, _spec) -> None:
        self.reaper_ensured += 1


def _preparation(manager: Any) -> MemberPreparation:
    return MemberPreparation(manager, _SPEC, user_uid=os.getuid(), user_gid=os.getgid())


def _step(
    script: str, *, failure: str = "step failed", timeout_s: float = 10, **changes: Any
) -> MemberStep:
    return MemberStep(
        ("/bin/sh", "-c", script),
        timeout_s=timeout_s,
        failure=lambda stderr: SandboxError(f"{failure}: " + stderr.decode()[-300:]),
        **changes,
    )


def _queue(preparation: MemberPreparation, *steps: MemberStep) -> None:
    preparation.add(*steps, user_uid=os.getuid(), user_gid=os.getgid())


def test_queued_steps_run_in_order_in_one_exec(tmp_path):
    manager = _LocalExec()
    preparation = _preparation(manager)
    log = tmp_path / "log"
    _queue(
        preparation,
        _step(f"echo first >> {log}"),
        _step(f"cat >> {log}", input_bytes=b"second\n"),
        _step(f"read line || true; echo third:$line >> {log}"),
    )

    preparation.run()

    assert log.read_text() == "first\nsecond\nthird:\n", "no input is an empty stdin"
    ((argv, kwargs),) = manager.calls
    assert argv == ["python3", "-I", "-c", RUNNER]
    assert kwargs["workdir"] == "/"
    assert (kwargs["user_uid"], kwargs["user_gid"]) == (os.getuid(), os.getgid())
    assert kwargs["timeout_s"] == 30 + 10, "each step's own timeout, plus the runner's start"
    assert not preparation.pending
    assert manager.reaper_ensured == 0


def test_a_lone_step_is_the_exec_it_always_was():
    manager = _LocalExec()
    preparation = _preparation(manager)
    _queue(preparation, _step("cat > /dev/null", input_bytes=b"payload"))

    preparation.run()

    ((argv, kwargs),) = manager.calls
    assert argv == ["/bin/sh", "-c", "cat > /dev/null"]
    assert kwargs["input_bytes"] == b"payload"
    assert kwargs["timeout_s"] == 10


def test_a_failure_stops_the_queue_with_its_own_error(tmp_path):
    manager = _LocalExec()
    preparation = _preparation(manager)
    after = tmp_path / "after"
    _queue(
        preparation,
        _step("true"),
        _step("echo the home is not private >&2; exit 3", failure="home"),
        _step(f"touch {after}"),
    )

    with pytest.raises(SandboxError, match=r"^home: the home is not private"):
        preparation.run()

    assert not after.exists()
    assert len(manager.calls) == 1


def test_a_soft_failure_is_reported_and_the_queue_goes_on(tmp_path, capsys):
    manager = _LocalExec()
    preparation = _preparation(manager)
    after = tmp_path / "after"
    _queue(
        preparation,
        _step("echo hooks broke >&2; exit 1", failure="tool hooks", soft="hooks not merged"),
        _step(f"touch {after}"),
    )

    preparation.run()

    assert after.exists()
    assert "hooks not merged: tool hooks: hooks broke" in capsys.readouterr().err


def test_a_step_retries_once_from_where_it_stood(tmp_path):
    """An untrusted collection: the sandbox user secures it, and the member asks again."""
    manager = _LocalExec()
    preparation = _preparation(manager)
    first, attempts, later = tmp_path / "first", tmp_path / "attempts", tmp_path / "later"
    secured: list[str] = []
    _queue(
        preparation,
        _step(f"echo x >> {first}"),
        _step(
            f'echo x >> {attempts}; test "$(wc -l < {attempts})" -gt 1 || exit 75',
            retry_status=75,
            before_retry=lambda: secured.append("collections"),
        ),
        _step(f"echo x >> {later}"),
    )

    preparation.run()

    assert secured == ["collections"]
    assert first.read_text() == "x\n", "steps before the retried one ran once"
    assert attempts.read_text() == "x\nx\n"
    assert later.read_text() == "x\n"
    assert len(manager.calls) == 2


def test_a_second_retryable_failure_is_raised():
    manager = _LocalExec()
    preparation = _preparation(manager)
    secured: list[str] = []
    _queue(
        preparation,
        _step("true"),
        _step(
            "echo still squatted >&2; exit 75",
            failure="home",
            retry_status=75,
            before_retry=lambda: secured.append("collections"),
        ),
    )

    with pytest.raises(SandboxError, match="home: still squatted"):
        preparation.run()
    assert secured == ["collections"]


def test_a_step_that_outlives_its_timeout_fails_as_its_exec_did(tmp_path):
    manager = _LocalExec()
    preparation = _preparation(manager)
    after = tmp_path / "after"
    _queue(preparation, _step("exec sleep 30", timeout_s=0.5), _step(f"touch {after}"))

    with pytest.raises(SandboxError, match=r"chat sandbox exec failed: /bin/sh timed out"):
        preparation.run()
    assert not after.exists()


def test_each_step_sees_the_environment_its_own_exec_gave_it(tmp_path):
    """The sandbox image sets no locale, so Python's start adds one to ``os.environ``.

    PEP 538 sets ``LC_CTYPE`` in the runner's own environment; its steps must
    not inherit it, or their tools would quote and classify differently.
    """
    exec_environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LANG": ""}
    manager = _LocalExec(exec_environment)
    preparation = _preparation(manager)
    seen = tmp_path / "seen"
    _queue(preparation, _step("true"), _step(f"env > {seen}"))

    preparation.run()

    alone = subprocess.run(
        ["/bin/sh", "-c", "env"], env=exec_environment, cwd="/", capture_output=True, check=True
    )
    assert sorted(seen.read_bytes().splitlines()) == sorted(alone.stdout.splitlines())
    assert b"LC_CTYPE" not in seen.read_bytes()


def test_a_reap_spares_exactly_the_runner_running_it():
    """The reaper kills every process of its UID not in its baseline, save itself."""
    manager = _LocalExec()
    preparation = _preparation(manager)
    # Stands in for the reaper: its baseline must name its parent, the runner,
    # by (pid, start time) as the reaper reads them, after what the host sent.
    check = (
        "import os, sys\n"
        "baseline = sys.stdin.buffer.read().decode('ascii')\n"
        "parent = os.getppid()\n"
        "with open(f'/proc/{parent}/stat', encoding='ascii') as handle:\n"
        "    started = handle.read().rsplit(') ', 1)[1].split()[19]\n"
        "sys.exit(0 if baseline == f'41 400\\n{parent} {started}\\n' else 3)\n"
    )
    reap = MemberStep(
        ("python3", "-c", check),
        timeout_s=10,
        failure=lambda stderr: SandboxError("reap failed: " + stderr.decode()),
        input_bytes=b"41 400\n",
        reaps=True,
    )
    _queue(preparation, reap, _step("true"))

    preparation.run()

    assert manager.reaper_ensured == 1, "the reaper is in place before the exec"


def test_a_runner_that_ends_early_fails_the_step_it_was_in(tmp_path, capsys):
    """Killed mid-queue: the step it was in failed as the exec did; a soft one goes on."""
    reports = b'{"status": 0, "stderr": ""}\n'
    manager = _LocalExec()
    calls: list[list[str]] = []

    def killed(_spec, argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["python3", "-I"]:
            return subprocess.CompletedProcess(argv, 137, reports, b"killed")
        return manager.exec_capture(_spec, argv, **kwargs)

    hard = _preparation(SimpleNamespace(exec_capture=killed))
    _queue(hard, _step("true"), _step("true", failure="state"), _step("true"))
    with pytest.raises(SandboxError, match=r"^state: killed$"):
        hard.run()

    after = tmp_path / "after"
    soft = _preparation(SimpleNamespace(exec_capture=killed))
    _queue(soft, _step("true"), _step("true", soft="hooks not merged"), _step(f"touch {after}"))
    soft.run()
    assert "hooks not merged: step failed: killed" in capsys.readouterr().err
    assert after.exists(), "what the killed runner never reached runs in an exec of its own"
    assert calls[-1] == ["/bin/sh", "-c", f"touch {after}"]


def test_a_step_for_another_uid_is_refused():
    preparation = _preparation(_LocalExec())
    with pytest.raises(ValueError, match="as the member it is queued for"):
        preparation.add(_step("true"), user_uid=os.getuid() + 1, user_gid=os.getgid())


# ── a member's launch ─────────────────────────────────────────────────────


def _name(argv: list[str]) -> str:
    """Which member command ``argv`` is."""
    script = argv[2] if len(argv) > 2 else ""
    if argv[0] == MEMBER_REAPER_TARGET:
        return "reap"
    if script == TOOL_HOOKS_SCRIPT:
        return "hooks"
    for marker, name in (
        ("resident-*.pid", "stale"),
        ("Claude member home is not private", "claude-home"),
        (".freeinference-member-state-v1", "home"),
        ("mkdir -m 3770", "collections"),
        ('cat > "$temporary"', "inference-env"),
    ):
        if marker in script:
            return name
    return argv[0]


class _Guest:
    """Records each exec of a launch, and the queue each runner exec carries."""

    def __init__(self, statuses: dict[str, list[int]] | None = None) -> None:
        self.events: list[tuple[str, Any, int | None]] = []
        self.statuses = statuses or {}

    def exec_capture(self, _spec, argv, **kwargs):
        if argv[:3] == ["python3", "-I", "-c"]:
            steps = json.loads(kwargs["input_bytes"])
            names = [_name(step["argv"]) for step in steps]
            self.events.append(("runner", names, kwargs["user_uid"]))
            reports = []
            for name in names:
                status = self.statuses.get(name, [0]).pop(0) if self.statuses.get(name) else 0
                reports.append(json.dumps({"status": status, "stderr": ""}) + "\n")
                if status != 0:
                    break
            stdout = "".join(reports).encode()
            return subprocess.CompletedProcess(argv, 0, stdout, b"")
        name = _name(argv)
        self.events.append(("exec", name, kwargs.get("user_uid")))
        status = self.statuses.get(name, [0]).pop(0) if self.statuses.get(name) else 0
        return subprocess.CompletedProcess(argv, status, b"", b"")


def _manager(guest: _Guest) -> ChatSandboxManager:
    manager = ChatSandboxManager(ContainerBackend(image="img", sandbox_namespace="ns"))
    # The reaper was found in this container at an earlier census.
    proven = manager._proven.record(
        _SPEC,
        container_inspection(
            {"Id": "container", "Name": "/chat", "State": {"Status": "running", "Running": True}}
        ),
    )
    manager._proven.note_helper(proven, MEMBER_REAPER_TARGET)
    manager.exec_capture = guest.exec_capture
    manager.spawn = lambda *_a, **_kw: guest.events.append(("spawn", None, None))
    for root_only in ("ensure_mcp_helpers", "ensure_kilo_mcp_runtime", "ensure_codex_mcp_policy"):
        setattr(manager, root_only, lambda *_a: None)
    return manager


def _launch(runtime_id: str, *, slot: int = 1, **changes: Any) -> ResidentLaunch:
    return replace(
        ResidentLaunch(
            thread_id="athr_member",
            runtime_id=runtime_id,
            runtime_version=None,
            native_session_id="native-member",
            model="model",
            reasoning_effort=None,
            gateway_base_url="http://gateway",
            credential="residency-grant",
            slot_id=slot,
        ),
        **changes,
    )


_OWNER_SECRET = BoundEnvironment(
    binding_id="binding",
    variables=[EnvironmentVariable(name="ANTHROPIC_API_KEY", value="owner-" + "7" * 20)],
)


@pytest.fixture
def driver(monkeypatch):
    """A driver that, like Claude's, merges its hooks before it spawns."""
    seen: list[Any] = []

    def session(context, runtime_id):
        if runtime_id == "claude-code" and context.install_tool_hooks is not None:
            try:
                context.install_tool_hooks({"hooks": {"PreToolUse": []}})
            except Exception as exc:  # what ClaudeCodeLiveSession does
                print(f"tool hooks not installed: {exc}", file=sys.stderr)
        seen.append(context)
        return SimpleNamespace(
            ensure_started=lambda **_kw: context.spawn_live(["agent"], dict(context.base_env)),
            process_alive=lambda: True,
            close=lambda: None,
        )

    monkeypatch.setattr(
        resident_transport,
        "get_driver",
        lambda runtime_id: SimpleNamespace(
            launch_live=lambda context: session(context, runtime_id),
            launch_resident_live=lambda context, session_id: session(context, runtime_id),
        ),
    )
    return seen


@pytest.mark.parametrize(
    ("runtime_id", "queue"),
    [
        # Claude reaps before its home, as it always has; the hook merge follows.
        ("claude-code", ["reap", "stale", "claude-home", "hooks"]),
        # The others prepared their home at open, and reaped at the spawn.
        ("codex", ["home", "reap", "stale"]),
        ("opencode", ["home", "reap", "stale"]),
        ("kilo", ["home", "reap", "stale"]),
        ("pi", ["reap", "stale"]),
    ],
)
def test_a_launch_runs_its_member_steps_in_one_exec_in_their_order(driver, runtime_id, queue):
    guest = _Guest()
    factory = ResidentTransportFactory(_manager(guest))

    factory.open(_SPEC, _launch(runtime_id))

    assert guest.events == [("runner", queue, MEMBER_UID_BASE + 1), ("spawn", None, None)]


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi"])
def test_owner_credentials_are_written_by_their_own_exec_after_the_reap(driver, runtime_id):
    """Until the reap ends, a process an earlier launch left could read the runner's input."""
    guest = _Guest()
    factory = ResidentTransportFactory(_manager(guest))

    factory.open(_SPEC, _launch(runtime_id, environment=_OWNER_SECRET))

    (runner, write, spawn) = guest.events
    assert runner[0] == "runner" and "reap" in runner[1]
    assert write == ("exec", "inference-env", MEMBER_UID_BASE + 1)
    assert spawn[0] == "spawn"


@pytest.mark.parametrize("runtime_id", ["claude-code", "codex", "pi"])
def test_plugin_installers_find_their_slot_reaped_and_home_prepared(
    monkeypatch, driver, runtime_id
):
    guest = _Guest()
    factory = ResidentTransportFactory(_manager(guest))

    def install(*_args, **_kwargs):
        guest.events.append(("install", None, None))
        return {}

    for installer in (
        "install_claude_plugins",
        "install_codex_plugins",
        "install_other_runtime_plugins",
    ):
        monkeypatch.setattr(resident_transport, installer, install)
    plugins = (
        ({"plugin": "@owner/plugin@1.2.3"},)
        if runtime_id == "pi"
        else ({"plugin": "review@fixture", "marketplace": "owner/marketplace"},)
    )
    launch = _launch(runtime_id, plugins=plugins, environment=_OWNER_SECRET)

    factory.open(_SPEC, launch)

    home = {"claude-code": ["claude-home"], "codex": ["home"], "pi": []}[runtime_id]
    hooks = [("exec", "hooks", MEMBER_UID_BASE + 1)] if runtime_id == "claude-code" else []
    assert guest.events == [
        ("runner", ["reap", "stale", *home], MEMBER_UID_BASE + 1),
        ("install", None, None),
        # After the installers, alone, as before: its failure is the driver's to report.
        *hooks,
        ("exec", "inference-env", MEMBER_UID_BASE + 1),
        ("spawn", None, None),
    ]


def test_a_failed_hook_merge_in_the_queue_still_starts_the_cli(driver, capsys):
    guest = _Guest({"hooks": [1]})
    factory = ResidentTransportFactory(_manager(guest))

    factory.open(_SPEC, _launch("claude-code"))

    assert guest.events[-1] == ("spawn", None, None)
    assert "tool hooks not installed: tool hooks could not be installed" in capsys.readouterr().err


def test_a_home_that_cannot_be_prepared_stops_the_launch_before_its_reap(driver):
    guest = _Guest({"home": [1]})
    factory = ResidentTransportFactory(_manager(guest))

    with pytest.raises(DriverError, match="codex member state could not be prepared"):
        factory.open(_SPEC, _launch("codex"))

    assert guest.events == [("runner", ["home", "reap", "stale"], MEMBER_UID_BASE + 1)]


def test_an_untrusted_collection_is_secured_and_the_queue_resumed_from_the_home(driver):
    guest = _Guest({"home": [75, 0]})
    factory = ResidentTransportFactory(_manager(guest))

    factory.open(_SPEC, _launch("opencode"))

    assert guest.events == [
        ("runner", ["home", "reap", "stale"], MEMBER_UID_BASE + 1),
        ("exec", "collections", None),  # as the sandbox user, never a member
        ("runner", ["home", "reap", "stale"], MEMBER_UID_BASE + 1),
        ("spawn", None, None),
    ]


def test_the_hooks_reach_the_members_own_settings_through_the_queue(driver):
    guest = _Guest()
    captured: list[dict[str, Any]] = []
    manager = _manager(guest)
    recorded = manager.exec_capture

    def capture(spec, argv, **kwargs):
        if argv[:3] == ["python3", "-I", "-c"]:
            captured.extend(json.loads(kwargs["input_bytes"]))
        return recorded(spec, argv, **kwargs)

    manager.exec_capture = capture

    ResidentTransportFactory(manager).open(_SPEC, _launch("claude-code"))

    hooks = captured[-1]
    assert hooks["argv"][3:] == [
        claude_member_config_dir(1, "athr_member"),
        "/agent-state/tool-marks.log",
    ]
    assert json.loads(base64.b64decode(hooks["input"])) == {"hooks": {"PreToolUse": []}}
    assert hooks["soft"] is True and hooks["timeout"] == 10
    assert all(not step["soft"] for step in captured[:-1])
