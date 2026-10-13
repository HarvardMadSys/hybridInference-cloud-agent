"""Find the checkout a pull-request creation will be made from.

`pre-pr-check.sh` runs this with the Claude Code PreToolUse event on stdin.

Claude Code starts a Bash hook whenever it cannot prove a command does *not*
match the hook's `gh pr create` pattern, which includes ordinary commands with
loops or heredocs. And the hook itself starts in the session's directory, while
the pull request may be created from another worktree (`cd <worktree> && gh pr
create ...`). So the gate first has to know whether this is a pull-request
creation at all, and if so which checkout holds the commit under review.

Output on stdout, exit status 0:

- ``skip``: the tool call does not create a pull request; allow it.
- an absolute directory: run the gate in that checkout.

Exit status 2 with a reason on stderr when the checkout cannot be determined.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

_OPERATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
_GH_PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")


class TargetError(Exception):
    """The checkout under review cannot be determined."""


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def parse_command(command: str, cwd: str) -> tuple[str, str | None] | None:
    """Return ``(directory, head)`` for a `gh pr create` command, else ``None``.

    ``directory`` is where the shell will be when `gh pr create` runs: the
    session directory, changed by any earlier ``cd``. ``head`` is the value of
    ``--head``/``-H`` when given.
    """
    if not _GH_PR_CREATE.search(command):
        return None
    try:
        tokens = _tokens(command)
    except ValueError as error:
        raise TargetError(f"cannot parse the pull-request command ({error})") from error

    directory = cwd
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "cd" and index + 1 < len(tokens) and tokens[index + 1] not in _OPERATORS:
            directory = os.path.normpath(
                os.path.join(directory, os.path.expanduser(tokens[index + 1]))
            )
            index += 2
            continue
        if tokens[index : index + 3] == ["gh", "pr", "create"]:
            head = None
            args = []
            for arg in tokens[index + 3 :]:
                if arg in _OPERATORS:
                    break
                args.append(arg)
            for position, arg in enumerate(args):
                if arg in ("--head", "-H") and position + 1 < len(args):
                    head = args[position + 1]
                elif arg.startswith("--head="):
                    head = arg.split("=", 1)[1]
            return directory, head
        index += 1
    # The text matched but no `gh pr create` invocation was found in it, for
    # example inside a quoted argument. Nothing will create a pull request.
    return None


def _worktree_branches(directory: str) -> dict[str, str]:
    """Map each checked-out branch to its worktree path."""
    listing = subprocess.run(
        ["git", "-C", directory, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        raise TargetError(f"{directory} is not inside a Git worktree")
    branches: dict[str, str] = {}
    path = None
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/") and path is not None:
            branches[line[len("branch refs/heads/") :]] = path
    return branches


def _current_branch(directory: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", directory, "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def resolve(directory: str, head: str | None) -> str:
    """Return the checkout whose commit the pull request will propose.

    Without a head branch, `gh` proposes the branch checked out in
    ``directory``. With one, the gate has to run where that branch is checked
    out; a branch that is not checked out anywhere cannot be checked.
    """
    if not os.path.isdir(directory):
        raise TargetError(f"{directory} does not exist")
    if head is None:
        return directory
    branch = head.split(":", 1)[-1]  # `gh` also accepts <owner>:<branch>
    if _current_branch(directory) == branch:
        return directory
    worktree = _worktree_branches(directory).get(branch)
    if worktree is None:
        raise TargetError(
            f"the head branch {branch!r} is not checked out in any worktree of this "
            "repository; check it out so the gate can test the exact commit"
        )
    return worktree


def target(event: dict) -> str | None:
    """Return the checkout to gate for one PreToolUse event, or ``None`` to allow."""
    cwd = event.get("cwd") or os.getcwd()
    tool_input = event.get("tool_input") or {}
    if event.get("tool_name") == "Bash":
        parsed = parse_command(tool_input.get("command", ""), cwd)
        if parsed is None:
            return None
        return resolve(*parsed)
    # An MCP create_pull_request tool names the head branch directly.
    return resolve(cwd, tool_input.get("head"))


def main() -> int:
    """Read the event from stdin and print the decision."""
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        print(f"cannot read the hook event ({error})", file=sys.stderr)
        return 2
    try:
        checkout = target(event)
    except TargetError as error:
        print(error, file=sys.stderr)
        return 2
    print("skip" if checkout is None else checkout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
