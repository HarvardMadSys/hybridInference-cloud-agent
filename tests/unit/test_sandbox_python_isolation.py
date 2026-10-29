"""Nothing a member leaves behind loads into the platform's own code in a sandbox.

A member's home outlives the member that last used its slot: the next member
of that slot runs as the same UID, with the same home. Python started without
``-I`` loads that home's user site-packages, so a ``.pth`` file or a
``usercustomize`` module left there would run inside the platform's scripts,
the member-process reaper among them, before the reap that is meant to end
whatever the last member left running. And pi compiles its extensions with
jiti, whose default file cache is one directory under ``/tmp``, which every
member's UID shares.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
from tests.unit.test_member_preparation import (
    _SPEC as _MEMBER_SPEC,
    _Guest,
    _launch as _member_launch,
    _manager,
)
from tests.unit.test_shared_session_supervisor import _launch, _Manager, _spec

from cloud_agent_host import resident_transport
from cloud_agent_host.resident_transport import ResidentTransportFactory

REPO = pathlib.Path(__file__).resolve().parents[2]
HOST = REPO / "apps/backend/cloud_agent_host"
SANDBOX = REPO / "deploy/sandbox"
REAPER = SANDBOX / "member-process-reaper"
PI_WRAPPER = SANDBOX / "pi-freeinference"

#: Every ``python3`` in snippet text that is not positively followed by an
#: isolated interpreter flag. Written as "must look like this", not "must not
#: look like that": the spellings that run Python are open-ended (``-c``, a
#: heredoc's ``-``, ``-u``, ``-m``, a script path, a pipe, a redirect, a
#: different minor version, a quoted name, a variable holding the flags), and a
#: guard that enumerates the bad ones is always one spelling behind. A false
#: positive here costs a comment rewording; a false negative costs the rule.
_UNISOLATED_INTERPRETER = re.compile(
    r"""(?:(?<![\w./-])python3(?:\.\d+)?|["']python3(?:\.\d+)?["'])(?!\s+-I)"""
)

#: A probe runs a bare ``python3`` only to ask whether it is there, never to
#: hand it a script: ``command -v python3``, ``which python3``. It matches the
#: text that *ends* just before the word, so it exempts the probe and nothing
#: else in the same snippet.
_EXISTENCE_PROBE = re.compile(r"(?:command\s+-v|which)\s+$")

#: Two strings name an interpreter without running one, and the argv test above
#: already checks how it is invoked: a literal that *is* the token (an element
#: of a command list), and prose that happens to mention it ("neither curl nor
#: python3, so ..."). Neither is a snippet, so neither is this test's subject.
_INTERPRETER_TOKEN = re.compile(r"""^["']?python3(?:\.\d+)?["']?$""")
_PROSE_AFTER = re.compile(r"[,.;:)]")


def _interpreter_argv() -> list[tuple[str, list[object]]]:
    """Every list or tuple literal in the host's code whose first element is a Python."""
    found = []
    for path in sorted(HOST.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                continue
            first = node.elts[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            if first.value.rsplit("/", 1)[-1].startswith("python"):
                literal = [getattr(element, "value", None) for element in node.elts]
                found.append((f"{path.relative_to(REPO)}:{node.lineno}", literal))
    return found


def test_every_python_the_host_starts_in_a_sandbox_is_isolated():
    found = _interpreter_argv()
    assert len(found) > 20, "the scan no longer finds the host's interpreter commands"
    assert [where for where, argv in found if argv[1] != "-I"] == []


def test_the_reaper_is_started_by_an_isolated_interpreter():
    """Its path names no interpreter, so each command that runs it is checked."""
    runs = []
    for path in sorted(HOST.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            for index, element in enumerate(node.elts[:-1]):
                mode = node.elts[index + 1]
                if (
                    isinstance(element, ast.Name)
                    and element.id == "MEMBER_REAPER_TARGET"
                    and isinstance(mode, ast.Constant)
                    and mode.value in {"capture", "reap"}
                ):
                    prefix = [getattr(item, "value", None) for item in node.elts[:index]]
                    runs.append((f"{path.relative_to(REPO)}:{node.lineno}", prefix))
    assert len(runs) >= 3
    assert [where for where, prefix in runs if prefix != ["python3", "-I"]] == []


def test_no_shell_snippet_starts_python_unisolated():
    """Every ``python3`` a shell snippet runs carries ``-I``, whatever the form.

    This is a search over snippet text, not a lookup of known spellings: a
    snippet that runs Python from a heredoc (``python3 - "$x"``) is exactly as
    unisolated as one that runs ``-c``, and a guard that lists the spellings it
    knows lets the next one through.
    """
    strings = [
        (path.relative_to(REPO).as_posix(), node.lineno, node.value)
        for path in sorted(HOST.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(), str(path)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert any("python3 -I -c" in value for _where, _line, value in strings)

    unisolated = []
    for where, line, value in strings:
        if _INTERPRETER_TOKEN.match(value):
            continue
        for match in _UNISOLATED_INTERPRETER.finditer(value):
            # A probe is not a run, and prose is not a snippet; anything else
            # that runs Python must isolate it.
            if _EXISTENCE_PROBE.search(value[: match.start()]):
                continue
            if _PROSE_AFTER.match(value[match.end() :]):
                continue
            unisolated.append(f"{where}:{line}: {match.group(0)!r} in {value[:60]!r}")
    assert unisolated == []


def test_every_python_script_the_sandbox_ships_names_an_isolated_interpreter():
    """A shebang cannot use ``env`` to pass a flag, so each names its interpreter."""
    shebangs = {
        path.name: path.read_text(errors="replace").split("\n", 1)[0]
        for path in sorted(SANDBOX.iterdir())
        if path.is_file() and path.read_bytes().startswith(b"#!")
    }
    python = {name: line for name, line in shebangs.items() if "python" in line}
    assert {"member-process-reaper", "pi-freeinference", "claude-session-fork"} <= python.keys()
    assert {
        name: line
        for name, line in python.items()
        if not (line.startswith("#!/") and line.endswith(" -I") and "/env " not in line)
    } == {}


def _user_site(python: str, home: pathlib.Path) -> pathlib.Path:
    """Where ``python`` looks for a user's site-packages when ``home`` is HOME."""
    answer = subprocess.run(
        [python, "-c", "import site; print(site.getusersitepackages())"],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=True,
    )
    return pathlib.Path(answer.stdout.strip())


def _plant(site: pathlib.Path, marker: pathlib.Path) -> None:
    """What a member could leave in its home: a ``.pth`` line and a ``usercustomize``."""
    site.mkdir(parents=True, exist_ok=True)
    (site / "left-behind.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker) + '.pth'!r}).touch()\n"
    )
    (site / "usercustomize.py").write_text(
        f"import pathlib\npathlib.Path({str(marker) + '.usercustomize'!r}).touch()\n"
    )


def _ran(marker: pathlib.Path) -> list[str]:
    return sorted(path.suffix for path in marker.parent.glob(marker.name + ".*"))


def test_what_a_member_leaves_in_its_home_never_loads_into_the_reaper(tmp_path):
    """Run by the interpreter its first line names, the one the sandbox has.

    A virtual environment never loads a user's site-packages, so the test's own
    interpreter could not show the difference.
    """
    interpreter = REAPER.read_text().split("\n", 1)[0][2:].split()[0]
    if not pathlib.Path(interpreter).is_file():
        pytest.skip(f"{interpreter} is not on this host")
    home = tmp_path / "home"
    marker = tmp_path / "marker"
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    _plant(_user_site(interpreter, home), marker)
    uid = str(os.getuid())

    # The plant works: without -I, both run before the script does.
    subprocess.run([interpreter, str(REAPER), "capture", uid], env=env, check=False)
    assert _ran(marker) == [".pth", ".usercustomize"]
    for path in marker.parent.glob(marker.name + ".*"):
        path.unlink()

    # As the host starts it, and as its own first line does.
    subprocess.run([interpreter, "-I", str(REAPER), "capture", uid], env=env, check=False)
    assert _ran(marker) == []
    executable = tmp_path / "member-process-reaper"
    shutil.copyfile(REAPER, executable)
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    subprocess.run([str(executable), "capture", uid], env=env, check=False)
    assert _ran(marker) == []


def test_the_claude_fork_helper_loads_nothing_from_the_member_home(monkeypatch):
    """Retained containers keep an older helper, so the flag comes with the exec."""
    manager = _Manager()
    calls: list[dict[str, object]] = []

    def capture(_spec, _argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            [], 0, stdout=b'{"source_session_id":"source","session_id":"child"}', stderr=b""
        )

    monkeypatch.setattr(manager, "exec_capture", capture, raising=False)
    factory = ResidentTransportFactory(manager)  # type: ignore[arg-type]
    launch = _launch("athr_source", native_session_id="source", slot=1)

    assert factory._fork_claude(_spec(), "source", launch, 2) == "child"
    assert calls[0]["env"]["PYTHONNOUSERSITE"] == "1"  # type: ignore[index]


def test_every_pi_launch_brings_this_hosts_wrapper_into_a_retained_container(monkeypatch):
    """With MCP servers or without: pi always runs through its wrapper and the bridge.

    A container created from an older image keeps the wrapper that image
    shipped until the host copies in its own, which it does by digest.
    """
    guest = _Guest()
    manager = _manager(guest)
    refreshed: list[str] = []
    manager.ensure_mcp_helpers = lambda _spec, runtime: refreshed.append(runtime)

    def launch(context, *_args, **_kwargs):
        return SimpleNamespace(
            ensure_started=lambda **_kw: context.spawn_live(["agent"], dict(context.base_env)),
            process_alive=lambda: True,
            close=lambda: None,
        )

    driver = SimpleNamespace(launch_live=launch, launch_resident_live=launch)
    monkeypatch.setattr(resident_transport, "get_driver", lambda _runtime: driver)

    ResidentTransportFactory(manager).open(_MEMBER_SPEC, _member_launch("pi"))

    assert refreshed == ["pi"]
    assert ("spawn", None, None) in guest.events


def test_pi_compiles_its_extensions_with_no_cache_another_member_could_write(tmp_path):
    """However pi is started, the wrapper turns its compile caches in /tmp off."""
    stub = tmp_path / "bin" / "pi"
    stub.parent.mkdir()
    record = tmp_path / "caches"
    stub.write_text(
        f"#!{sys.executable}\nimport os\n"
        f"open({str(record)!r}, 'w').write("
        "os.environ.get('JITI_FS_CACHE', 'unset') + ' ' "
        "+ os.environ.get('NODE_DISABLE_COMPILE_CACHE', 'unset'))\n"
    )
    stub.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(PI_WRAPPER), "--mode", "rpc"],
        env={
            "PATH": f"{stub.parent}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://backend:8080/v1",
            "OPENAI_API_KEY": "ajt.attempt.key",
            "PI_GATEWAY_MODEL": "glm-5.1",
            "PI_CODING_AGENT_DIR": str(tmp_path / "activation"),
            "JITI_FS_CACHE": "true",
            "NODE_DISABLE_COMPILE_CACHE": "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert record.read_text() == "false 1"
