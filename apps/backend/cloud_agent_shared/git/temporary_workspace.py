"""The fixed first commit of a temporary workspace.

A temporary workspace is a conversation without a repository: its sandbox
starts from an empty directory instead of a clone, and nothing it does is
published. Git still needs a base. The Git panel, each turn's list of changed
files and Fork all compare the workspace against one, so the workspace starts
from a single empty commit.

That commit is the same bytes everywhere: Git's empty tree, a fixed identity,
a fixed time and a fixed message. The control plane therefore knows its id
when it admits the conversation and records it as the base, and the host that
builds the workspace proves it built exactly that commit. A commit stamped
with the current time would differ on every build, so a first turn retried on
a rebuilt workspace would leave the recorded base naming a commit the
workspace does not have.
"""

from __future__ import annotations

import hashlib

#: Git's empty tree. The same object in every SHA-1 repository, which is the
#: only object format a workspace is created with.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: The branch the workspace starts on: the name a new GitHub repository uses,
#: so ``git push -u origin main`` works when the owner later publishes it.
TEMPORARY_WORKSPACE_BRANCH = "main"

TEMPORARY_WORKSPACE_COMMIT_MESSAGE = "Start temporary workspace"

_NAME = "Cloud Agent"
_EMAIL = "cloud-agent@localhost"
# 2026-10-14T00:00:00Z. Any fixed instant would do; this one reads as a date
# in `git log` rather than as the Unix epoch.
_DATE = "1791936000 +0000"


def temporary_workspace_commit_env() -> dict[str, str]:
    """The environment under which ``git commit-tree`` writes exactly this commit."""
    return {
        "GIT_AUTHOR_NAME": _NAME,
        "GIT_AUTHOR_EMAIL": _EMAIL,
        "GIT_AUTHOR_DATE": _DATE,
        "GIT_COMMITTER_NAME": _NAME,
        "GIT_COMMITTER_EMAIL": _EMAIL,
        "GIT_COMMITTER_DATE": _DATE,
    }


def _commit_object() -> bytes:
    """The commit's content as Git stores it; ``commit-tree -m`` ends the message with a newline."""
    signature = f"{_NAME} <{_EMAIL}> {_DATE}"
    return (
        f"tree {EMPTY_TREE_SHA}\n"
        f"author {signature}\n"
        f"committer {signature}\n"
        "\n"
        f"{TEMPORARY_WORKSPACE_COMMIT_MESSAGE}\n"
    ).encode()


def _object_id(body: bytes) -> str:
    return hashlib.sha1(b"commit %d\0" % len(body) + body, usedforsecurity=False).hexdigest()


#: The id of the commit every temporary workspace starts from.
TEMPORARY_WORKSPACE_BASE_SHA = _object_id(_commit_object())
