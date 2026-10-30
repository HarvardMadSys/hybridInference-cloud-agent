"""Provider boundary adapters for historical native-session Fork.

Also how the sandbox image installs what they reach: the Agent SDK that
``claude-session-fork`` imports and the Kilo build the HTTP bridge drives.
"""

from __future__ import annotations

import ast
import json
import re
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy/sandbox/Dockerfile.agent-sandbox"
CLAUDE_FORK = REPO_ROOT / "deploy/sandbox/claude-session-fork"

#: A program the image build runs with ``-c``: the interpreter and flags, then the program.
_PROGRAM = re.compile(r"(\S+(?: +-[A-Za-z]+)*) +-c +(?:\\\n\s*)?'([^']*)'")
#: Names that load a module with no import statement for the build to run.
_DYNAMIC_IMPORT = frozenset({"importlib", "import_module", "__import__"})


def _script(path: str) -> dict:
    return runpy.run_path(str(REPO_ROOT / path), run_name=f"test_{Path(path).name}")


def test_opencode_and_kilo_fork_at_the_next_exclusive_user_boundary() -> None:
    payload = _script("deploy/sandbox/http-agent-live-bridge")["fork_payload"]
    messages = [
        {
            "info": {"id": "user-1", "role": "user"},
            "parts": [{"type": "text", "text": "first"}],
        },
        {"info": {"id": "assistant-1", "role": "assistant"}},
        {
            "info": {"id": "user-2", "role": "user"},
            "parts": [{"type": "text", "text": "second"}],
        },
        {"info": {"id": "assistant-2", "role": "assistant"}},
    ]

    assert payload(messages, 1) == {"messageID": "user-2"}
    assert payload(messages, 2) == {}
    with pytest.raises(RuntimeError, match=r"3 of 2"):
        payload(messages, 3)


def test_opencode_and_kilo_ignore_compaction_and_synthetic_continuations() -> None:
    payload = _script("deploy/sandbox/http-agent-live-bridge")["fork_payload"]
    messages = [
        {
            "info": {"id": "user-1", "role": "user"},
            "parts": [{"type": "text", "text": "first"}],
        },
        {"info": {"id": "assistant-tool", "role": "assistant"}},
        {
            "info": {"id": "compact", "role": "user"},
            "parts": [{"type": "compaction", "auto": True}],
        },
        {"info": {"id": "summary", "role": "assistant", "summary": True}},
        {
            "info": {"id": "continue", "role": "user"},
            "parts": [{"type": "text", "synthetic": True, "text": "continue"}],
        },
        {"info": {"id": "assistant-1", "role": "assistant"}},
        {
            "info": {"id": "user-2", "role": "user"},
            "parts": [{"type": "text", "text": "second"}],
        },
        {"info": {"id": "assistant-2", "role": "assistant"}},
    ]

    assert payload(messages, 1) == {"messageID": "user-2"}
    assert payload(messages, 2) == {}


def _claude_script(
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    sdk = ModuleType("claude_agent_sdk")
    sdk.fork_session = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    internal = ModuleType("claude_agent_sdk._internal")
    sessions = ModuleType("claude_agent_sdk._internal.sessions")
    sessions._resolve_session_file_path = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._internal", internal)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._internal.sessions", sessions)
    return _script("deploy/sandbox/claude-session-fork")


def test_claude_fork_ignores_unattributed_tool_results_and_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _claude_script(monkeypatch)["assistant_anchor"]

    def message(kind: str, uuid: str, content: object = "") -> dict:
        return {
            "type": kind,
            "uuid": uuid,
            "message": {"role": kind, "content": content},
        }

    messages = [
        message("user", "user-1"),
        message("assistant", "assistant-tool"),
        message("user", "tool-user", [{"type": "tool_result", "tool_use_id": "tool-1"}]),
        message("assistant", "assistant-1"),
        {
            **message("user", "compact-summary", "This session is being continued"),
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
        },
        message("user", "user-2"),
        {**message("assistant", "subagent"), "isSidechain": True},
        message("assistant", "assistant-2"),
    ]

    assert anchor(messages, 1) == "assistant-1"
    assert anchor(messages, 2) == "assistant-2"
    with pytest.raises(ValueError, match=r"3 of 2"):
        anchor(messages, 3)


def test_claude_raw_chain_follows_logical_parent_across_compaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _claude_script(monkeypatch)
    transcript = tmp_path / "session.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "message": {"role": "user", "content": "one"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "parentUuid": "user-1",
            "message": {"role": "assistant", "content": "done"},
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "parentUuid": None,
            "logicalParentUuid": "assistant-1",
        },
        {
            "type": "user",
            "uuid": "summary",
            "parentUuid": "boundary",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary"},
        },
        {
            "type": "user",
            "uuid": "user-2",
            "parentUuid": "summary",
            "message": {"role": "user", "content": "two"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "parentUuid": "user-2",
            "message": {"role": "assistant", "content": "done"},
        },
    ]
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    script["_session_entries"].__globals__["_resolve_session_file_path"] = (
        lambda *_args, **_kwargs: transcript
    )

    loaded = script["_session_entries"]("session", "/workspace")
    anchor = script["assistant_anchor"]

    assert [entry["uuid"] for entry in loaded] == [
        "user-1",
        "assistant-1",
        "boundary",
        "summary",
        "user-2",
        "assistant-2",
    ]
    assert anchor(loaded, 1) == "assistant-1"
    assert anchor(loaded, 2) == "assistant-2"


def test_claude_fork_calls_sdk_with_the_inclusive_assistant_anchor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _claude_script(monkeypatch)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {
                    "type": "user",
                    "uuid": "11111111-1111-4111-8111-111111111111",
                    "parentUuid": None,
                    "message": {"role": "user", "content": "one"},
                },
                {
                    "type": "assistant",
                    "uuid": "22222222-2222-4222-8222-222222222222",
                    "parentUuid": "11111111-1111-4111-8111-111111111111",
                    "message": {"role": "assistant", "content": "done"},
                },
            ]
        )
        + "\n"
    )
    script["_session_entries"].__globals__["_resolve_session_file_path"] = (
        lambda *_args, **_kwargs: transcript
    )
    call: dict[str, object] = {}

    def fork_session(source: str, **kwargs: object) -> object:
        call["source"] = source
        call.update(kwargs)
        return type(
            "ForkResult",
            (),
            {"session_id": "33333333-3333-4333-8333-333333333333"},
        )()

    script["main"].__globals__["fork_session"] = fork_session
    monkeypatch.setattr(
        sys,
        "argv",
        ["claude-session-fork", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "1"],
    )

    script["main"]()

    assert call == {
        "source": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "directory": "/workspace",
        "up_to_message_id": "22222222-2222-4222-8222-222222222222",
    }
    assert json.loads(capsys.readouterr().out) == {
        "source_session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "session_id": "33333333-3333-4333-8333-333333333333",
    }


def _instructions() -> list[str]:
    """The sandbox image's instructions, each with its continuation lines."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    return re.findall(r"^[A-Z]+ (?:.*\\\n)*.*$", dockerfile, re.MULTILINE)


def _run(marker: str) -> str:
    """The one RUN instruction in the sandbox image that contains ``marker``."""
    runs = [step for step in _instructions() if step.startswith("RUN ") and marker in step]
    assert len(runs) == 1, f"{len(runs)} RUN instructions contain {marker!r}, not one"
    return runs[0]


def _shebang(path: Path) -> str:
    """The interpreter and flags ``path`` names on its first line."""
    line = path.read_text(encoding="utf-8").split("\n", 1)[0]
    assert line.startswith("#!"), f"{path.name} names no interpreter"
    return " ".join(line.removeprefix("#!").split())


def _sdk_statements(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """The import statements in ``tree`` that import from the Agent SDK."""
    statements: list[ast.Import | ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        else:
            continue
        if any(module.split(".")[0] == "claude_agent_sdk" for module in modules):
            statements.append(node)
    return statements


def _sdk_imports(source: str) -> list[str]:
    """Every statement in ``source`` that imports from the Agent SDK, normalized."""
    return sorted(ast.unparse(node) for node in _sdk_statements(ast.parse(source)))


def _words(node: ast.AST) -> set[str]:
    """The identifier or string text ``node`` itself spells, without its children's."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.alias):
        return {node.name.split(".")[0]}
    if isinstance(node, ast.ImportFrom):
        return {(node.module or "").split(".")[0]}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    return set()


def _sdk_reached_without_import(source: str) -> list[str]:
    """Each place ``source`` may reach the SDK other than by an SDK import statement."""
    tree = ast.parse(source)
    statements = _sdk_statements(tree)
    pinned = {id(node) for statement in statements for node in ast.walk(statement)}
    bound = {
        (alias.asname or alias.name).split(".")[0]
        for statement in statements
        for alias in statement.names
    }
    reached = []
    for node in ast.walk(tree):
        words = set() if id(node) in pinned else _words(node)
        read_off_an_sdk_import = (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in bound
        )
        if (
            read_off_an_sdk_import
            or words & _DYNAMIC_IMPORT
            or any("claude_agent_sdk" in word for word in words)
        ):
            reached.append(ast.unparse(node))
    return reached


def test_the_image_build_imports_exactly_what_the_claude_fork_helper_imports() -> None:
    """The build runs the helper's SDK import lines, under the helper's own interpreter.

    The image deletes the SDK's bundled CLI on the grounds that the helper's
    imports never start it. An import the helper adds and the build does not
    run, or one the build runs with another interpreter or other flags than the
    helper's shebang names, would build green and fail at a user's first Fork.
    """
    programs = _PROGRAM.findall(_run('"claude-agent-sdk=='))
    assert programs, "the image no longer runs the helper's SDK imports"

    assert {" ".join(command.split()) for command, _ in programs} == {_shebang(CLAUDE_FORK)}
    built = sorted(ast.unparse(ast.parse(program)) for _, program in programs)
    assert built == _sdk_imports(CLAUDE_FORK.read_text(encoding="utf-8"))


def test_the_claude_fork_helper_reaches_the_sdk_only_through_its_import_statements() -> None:
    """Whatever the helper takes from the SDK, the build's import check has tried.

    The check runs the helper's SDK import statements and nothing else. A module
    loaded through ``importlib`` or ``__import__``, the SDK's name used as a
    value, or an attribute read off a name an SDK import bound would each reach
    something the check never tried.
    """
    assert _sdk_reached_without_import(CLAUDE_FORK.read_text(encoding="utf-8")) == []


def test_the_sdk_cli_is_deleted_by_its_install_and_before_the_import_check() -> None:
    """Any later RUN leaves the 214 MB in a layer; a check before proves nothing."""
    run = _run('"claude-agent-sdk==')
    deletion = re.search(r"&& rm \S+/claude_agent_sdk/_bundled/claude \\\n", run)
    first_program = _PROGRAM.search(run)
    assert deletion, "the SDK's bundled CLI is no longer deleted where the SDK is installed"
    assert first_program, "the image no longer runs the helper's SDK imports"

    assert run.index('"claude-agent-sdk==') < deletion.start() < first_program.start()


def _mount_options(spec: str) -> dict[str, str]:
    """A ``--mount`` value as options: ``type=bind,rw`` is ``{"type": "bind", "rw": ""}``."""
    return dict(option.partition("=")[::2] for option in spec.split(","))


def test_the_kilo_build_reaches_the_image_only_through_a_read_only_mount() -> None:
    """A COPY of the stage is a layer every host pulls, however soon it is deleted.

    The runtime install mounts the stage instead, read-only, so nothing that
    runs earlier in that RUN can change the reviewed build before
    install-runtimes.sh copies it.
    """
    copies = [
        step for step in _instructions() if step.startswith("COPY ") and "--from=kilo-build" in step
    ]
    assert copies == [], "the Kilo build is copied into a layer of its own"

    install = _run("install-runtimes.sh kilo ")
    mounts = [_mount_options(spec) for spec in re.findall(r"--mount=(\S+)", install)]
    kilo = [mount for mount in mounts if mount.get("from") == "kilo-build"]
    assert len(kilo) == 1, "the runtime install no longer mounts the Kilo build"
    assert kilo[0].get("type") == "bind"
    assert not {"rw", "readwrite"} & kilo[0].keys(), "the Kilo build is mounted writable"
