"""A script phase reaps and prepares its identity in one exec, step by step."""

from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cloud_agent_host import environment_process
from cloud_agent_host.environment_process import prepare_operation
from cloud_agent_host.sandbox import SandboxError

#: Stand-ins for the steps, recording where they ran: the real reap would kill
#: every process of the identity on this machine.
_REAP = (
    "import os,sys\nopen(os.environ['MARKS'],'a').write(f'reap {sys.argv[1]} {os.getpid()}\\n')\n"
)
_HOME = (
    "import os,sys\nopen(os.environ['MARKS'],'a').write(f'home {sys.argv[1]} {os.getpid()}\\n')\n"
)
_TOOLS = 'printf "tools %s %s\\n" "$$" "$(pwd -P)" >> "$MARKS"'


def _prepare(tmp_path, *, reap=_REAP, home=_HOME, tools=_TOOLS, limit="10", workspace_exists=True):
    """Run the one exec's script as the sandbox would, with the stand-ins."""
    marks = tmp_path / "marks"
    marks.touch()
    workspace = tmp_path / "workspace"
    if workspace_exists:
        workspace.mkdir(exist_ok=True)
    steps = [limit, reap, home, tools, str(workspace)]
    done = subprocess.run(
        [sys.executable, "-I", "-c", environment_process._PREPARE, "30001", *steps],
        env={"MARKS": str(marks), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return done, [line.split() for line in marks.read_text().splitlines()], workspace


def test_the_three_steps_run_in_order_the_reap_in_the_one_process(tmp_path):
    done, marks, workspace = _prepare(tmp_path)

    assert done.returncode == 0, done.stderr
    assert [mark[0] for mark in marks] == ["reap", "home", "tools"]
    assert marks[0][1] == marks[1][1] == "30001"
    # The reap spares the process it runs in, so it runs in the exec's own;
    # the tool home is a child started after it.
    reap_pid, home_pid, tools_pid = marks[0][2], marks[1][2], marks[2][1]
    assert reap_pid == home_pid != tools_pid
    assert marks[2][2] == str(workspace.resolve())


@pytest.mark.parametrize(
    ("step", "failure", "status", "ran"),
    [
        ("reap", "import sys\nsys.exit(1)\n", 11, []),
        ("home", "import sys\nsys.exit(2)\n", 12, ["reap"]),
        ("home", "raise OSError('symlink')\n", 12, ["reap"]),
        ("tools", "exit 1", 13, ["reap", "home"]),
    ],
)
def test_a_failing_step_stops_the_rest_and_says_which(tmp_path, step, failure, status, ran):
    done, marks, _workspace = _prepare(tmp_path, **{step: failure})

    assert done.returncode == status
    assert [mark[0] for mark in marks] == ran


def test_the_tool_home_has_the_execs_environment_not_pythons(tmp_path):
    """Python's start sets LC_CTYPE for itself in a C locale (PEP 538); the shell never had it."""
    tools = 'printf "tools %s\\n" "${LC_CTYPE-unset}" >> "$MARKS"'

    done, marks, _workspace = _prepare(tmp_path, tools=tools)

    assert done.returncode == 0, done.stderr
    assert marks[2] == ["tools", "unset"]


def test_a_tool_home_that_cannot_start_is_its_own_failure(tmp_path):
    done, marks, _workspace = _prepare(tmp_path, workspace_exists=False)

    assert done.returncode == 13
    assert [mark[0] for mark in marks] == ["reap", "home"]


@pytest.mark.parametrize(
    ("step", "stall", "status"),
    [
        # A reap's own handler for OSError must not swallow the limit.
        ("reap", "import time\ntry: time.sleep(30)\nexcept OSError: pass\n", 11),
        ("home", "import time\ntime.sleep(30)\n", 12),
        ("tools", "exec sleep 30", 13),
    ],
)
def test_each_step_keeps_the_limit_it_had_alone(tmp_path, step, stall, status):
    started = time.monotonic()
    done, _marks, _workspace = _prepare(tmp_path, limit="0.3", **{step: stall})

    assert done.returncode == status
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (11, "cleanup could not be confirmed"),
        (12, "home could not be prepared"),
        (13, "tool directories could not be prepared"),
        (1, "operation could not be prepared"),
    ],
)
def test_each_step_keeps_its_own_error(status, message):
    calls = []

    def exec_capture(_spec, argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=status, stdout=b"")

    backend = SimpleNamespace(
        spec=None,
        manager=SimpleNamespace(exec_capture=exec_capture, backend=SimpleNamespace(gid=10001)),
    )

    with pytest.raises(SandboxError, match=message):
        prepare_operation(backend, 30001)  # type: ignore[arg-type]

    ((argv, kwargs),) = calls
    assert argv[:4] == ["python3", "-I", "-c", environment_process._PREPARE]
    assert argv[4:] == [
        "30001",
        "10",
        environment_process._REAP,
        environment_process._HOME,
        environment_process._TOOL_HOME,
        "/workspace",
    ]
    assert (kwargs["user_uid"], kwargs["user_gid"], kwargs["workdir"]) == (30001, 10001, "/")
    # Each step's own limit, and the runner's start beside them.
    assert kwargs["timeout_s"] == 40


def test_an_unreserved_identity_is_refused_before_anything_runs():
    with pytest.raises(SandboxError, match="Invalid environment operation identity"):
        prepare_operation(SimpleNamespace(), 1000)  # type: ignore[arg-type]
