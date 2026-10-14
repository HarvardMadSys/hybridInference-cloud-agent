"""A temporary workspace: one fixed empty commit, built on the host, never cloned.

The control plane records the commit's id as the base when it admits the
conversation, and the host builds the commit itself, so the two must agree to
the byte. These cases check that agreement against real Git, that the runner
builds the workspace instead of cloning one, and that a conversation without a
repository is never treated as a repository's: no project memory, no
repository check behind a goal.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from dataclasses import replace
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

from tests.unit.test_goal_controller import FakeGateway
from tests.unit.test_runner_host_settings import _JOB, _run_new_chat

from cloud_agent.api.worker import _claim_agent_memories
from cloud_agent.goals.controller import _check_access
from cloud_agent_host import runner as runner_mod
from cloud_agent_host.runner import prepare_temporary_worktree
from cloud_agent_shared.git.temporary_workspace import (
    EMPTY_TREE_SHA,
    TEMPORARY_WORKSPACE_BASE_SHA,
    TEMPORARY_WORKSPACE_COMMIT_MESSAGE,
    temporary_workspace_commit_env,
)

_TEMPORARY_JOB = replace(_JOB, repo=None, base_sha=TEMPORARY_WORKSPACE_BASE_SHA)


def _git(workdir: str, *args: str, env: dict[str, str] | None = None) -> str:
    """Git with no user or system configuration, as the runner runs it."""
    hermetic = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        **(env or {}),
    }
    return subprocess.run(
        ["git", *args], cwd=workdir, env=hermetic, capture_output=True, text=True, check=True
    ).stdout.strip()


def _refuse_clone(**_kwargs: Any) -> str:
    raise AssertionError("a temporary workspace is never cloned")


def test_the_recorded_base_is_the_commit_git_writes(tmp_path) -> None:
    """The id the control plane records is computed, so Git itself must agree with it."""
    _git(str(tmp_path), "init", "--quiet", str(tmp_path))

    assert _git(str(tmp_path), "hash-object", "-t", "tree", "--stdin") == EMPTY_TREE_SHA
    written = _git(
        str(tmp_path),
        "commit-tree",
        EMPTY_TREE_SHA,
        "-m",
        TEMPORARY_WORKSPACE_COMMIT_MESSAGE,
        env=temporary_workspace_commit_env(),
    )
    assert written == TEMPORARY_WORKSPACE_BASE_SHA


def test_the_host_starts_main_on_the_fixed_commit_every_time(tmp_path) -> None:
    """One empty commit on main, nothing to commit, no remote, and the same id twice."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    commit = prepare_temporary_worktree(workdir=str(first))

    assert commit == TEMPORARY_WORKSPACE_BASE_SHA
    assert prepare_temporary_worktree(workdir=str(second)) == commit
    assert _git(str(first), "symbolic-ref", "HEAD") == "refs/heads/main"
    assert _git(str(first), "rev-list", "--count", "HEAD") == "1"
    assert _git(str(first), "status", "--porcelain") == ""
    assert _git(str(first), "remote") == ""
    assert sorted(path.name for path in first.iterdir()) == [".git"]


def test_the_runner_builds_a_temporary_workspace_instead_of_cloning(monkeypatch) -> None:
    """No clone and no output branch: the import carries the empty repository on main."""
    monkeypatch.setattr(runner_mod, "prepare_worktree", _refuse_clone)
    monkeypatch.setattr(runner_mod, "checkout_output_branch", _refuse_clone)
    prefetch = runner_mod._prefetch_retained_source(job=_TEMPORARY_JOB, uid=1000, gid=1000)
    imported: list[bytes] = []
    manager = Mock()
    manager.import_archive.side_effect = lambda _spec, _path, archive: imported.append(
        archive.read()
    )

    assert prefetch.import_into(manager, Mock()) == TEMPORARY_WORKSPACE_BASE_SHA

    with tarfile.open(fileobj=io.BytesIO(imported[0])) as archive:
        head = next(member for member in archive.getmembers() if member.name.endswith(".git/HEAD"))
        reference = archive.extractfile(head)
        assert reference is not None
        assert reference.read().strip() == b"ref: refs/heads/main"
        tops = {
            next((part for part in PurePosixPath(name).parts if part != "."), "")
            for name in archive.getnames()
        }
        assert tops <= {"", ".git"}, "an empty workspace has nothing but its repository"


def test_a_missing_temporary_base_is_rebuilt_not_cloned(monkeypatch) -> None:
    """A workspace that lost its base gets the same empty commit back, as a bundle."""
    monkeypatch.setattr(runner_mod, "prepare_worktree", _refuse_clone)
    bundles: list[dict[str, Any]] = []
    manager = Mock()
    manager.import_base_bundle.side_effect = lambda _spec, bundle, **kwargs: bundles.append(
        {"bytes": bundle, **kwargs}
    )

    imported = runner_mod._import_retained_base(
        job=_TEMPORARY_JOB, manager=manager, spec=Mock(), backend=Mock()
    )

    assert imported == TEMPORARY_WORKSPACE_BASE_SHA
    assert bundles[0]["commit"] == TEMPORARY_WORKSPACE_BASE_SHA
    assert bundles[0]["bytes"].startswith(b"# v2 git bundle")


def _checkout_event(control: Mock) -> dict[str, Any]:
    return next(
        call.args[0].payload
        for call in control.append_event.call_args_list
        if call.args[0].event_type == "lifecycle"
        and call.args[0].payload.get("phase") == "checked_out"
    )


def test_the_first_turn_says_nothing_was_cloned(monkeypatch) -> None:
    """The checkout event marks the tree as the empty commit, so no one reads "repository ready"."""
    temporary = _run_new_chat(monkeypatch, None, _TEMPORARY_JOB)
    repository = _run_new_chat(monkeypatch, None, _JOB)

    assert temporary.result == repository.result == 0
    assert _checkout_event(temporary.control)["temporary"] is True
    assert "temporary" not in _checkout_event(repository.control)


async def test_a_temporary_workspace_feeds_only_user_memory() -> None:
    """A claim reads the user scope with no repository and marks only its memories."""
    user_memory = {"id": "user-rule", "content": "Always label the axes in charts."}
    repository = SimpleNamespace(
        task_memories=AsyncMock(return_value={"repo": None, "project": [], "user": [user_memory]}),
        mark_memories_used=AsyncMock(),
    )

    memories = await _claim_agent_memories(repository, "owner", None)

    repository.task_memories.assert_awaited_once_with("owner", None)
    repository.mark_memories_used.assert_awaited_once_with("owner", ["user-rule"])
    assert memories == {"repo": None, "project": [], "user": [user_memory]}


async def test_a_goal_in_a_temporary_workspace_checks_no_repository(monkeypatch) -> None:
    """The owner's Agent access is still asked; there is no repository to ask about."""

    async def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a temporary workspace has no repository to check")

    monkeypatch.setattr("cloud_agent.goals.controller.require_entitled_repo", refuse)
    gateway = FakeGateway()
    turn = {
        "id": "ajob_1",
        "thread_id": "athr_1",
        "user_id": "owner",
        "repo": None,
        "archived_at": None,
        "thread_sandbox": None,
    }

    action = await _check_access(
        SimpleNamespace(source_control=None), turn, app_credentials=object(), gateway=gateway
    )

    assert action is None
    assert gateway.asked == ["owner"]


async def test_a_goal_in_a_temporary_workspace_still_needs_agent_access() -> None:
    """Withdrawn access pauses the goal before any repository question arises."""
    gateway = FakeGateway()
    gateway.allowed = False
    turn = {
        "id": "ajob_1",
        "thread_id": "athr_1",
        "user_id": "owner",
        "repo": None,
        "archived_at": None,
        "thread_sandbox": None,
    }

    action = await _check_access(SimpleNamespace(source_control=None), turn, gateway=gateway)

    assert action is not None
    assert action.kind == "pause"
    assert action.reason == "access_denied"
