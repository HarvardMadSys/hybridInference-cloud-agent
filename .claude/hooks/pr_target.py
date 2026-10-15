"""Find the checkout a pull-request creation will be made from.

`pre-pr-check.sh` runs this with the Claude Code PreToolUse event on stdin.

Claude Code starts a Bash hook whenever it cannot prove a command does *not*
match the hook's `gh pr create` pattern, which includes ordinary commands with
loops or heredocs. And the hook itself starts in the session's directory, while
the pull request may be created from another worktree (`cd <worktree> && gh pr
create ...`). So the gate first has to know whether this is a pull-request
creation at all, and if so which checkout holds the commit under review.

The gate belongs to this repository. A pull request for another repository
(a ``cd`` into its checkout, ``--repo``/``-R`` naming it, or an MCP call naming
its owner and name) follows that repository's own checks, not this one's.

Output on stdout, exit status 0:

- ``skip``: the tool call creates no pull request for this repository; allow
  it.
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

# The characters that separate shell commands. A newline ends a command just as
# `;` does, and shlex joins adjacent ones into one token, such as `;(` or `&&\n`.
_PUNCTUATION = ";&|()\n"
_GH_PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
# A backslash-newline continues a line; one escaped backslash before it does not.
_LINE_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
# The `gh pr create` options that take a value. A value is skipped, so a title
# such as `-Hotfix` or a body of `--repo` is never read as an option.
_VALUE_OPTIONS = frozenset(
    {
        *("-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file"),
        *("-H", "--head", "-l", "--label", "-m", "--milestone", "-p", "--project"),
        *("-R", "--repo", "-r", "--reviewer", "-T", "--template", "-t", "--title"),
        "--recover",
    }
)
_LONG_OPTION = {"-H": "--head", "-R": "--repo"}
# The OWNER/NAME of a GitHub remote URL or a `--repo` value, which may also be
# HOST/OWNER/NAME or a full URL.
_GITHUB_REPOSITORY = re.compile(r"(?:^|[:/])([^/:]+/[^/:]+?)(?:\.git)?/?$")


class TargetError(Exception):
    """The checkout under review cannot be determined."""


def _tokens(command: str) -> list[str]:
    # The shell drops a line continuation, so `gh pr create \` and the options
    # on the next line stay one command rather than splitting at the newline.
    command = _LINE_CONTINUATION.sub(r"\1", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_operator(token: str) -> bool:
    return bool(token) and set(token) <= set(_PUNCTUATION)


def _options(args: list[str]) -> dict[str, str]:
    """Read ``gh pr create``'s options in every form `gh` accepts; the last one wins."""
    values: dict[str, str] = {}
    position = 0
    while position < len(args):
        arg = args[position]
        position += 1
        if arg in _VALUE_OPTIONS:
            if position < len(args):
                values[_LONG_OPTION.get(arg, arg)] = args[position]
                position += 1
        elif arg.startswith("--") and "=" in arg:
            name, value = arg.split("=", 1)
            values[name] = value
        elif arg[:2] in _VALUE_OPTIONS and len(arg) > 2:
            # A short option's value may be attached: `-Htopic` or `-H=topic`.
            values[_LONG_OPTION.get(arg[:2], arg[:2])] = arg[2:].removeprefix("=")
    return values


def parse_command(command: str, cwd: str) -> tuple[str, str | None, str | None] | None:
    """Return ``(directory, head, repo)`` for a `gh pr create` command, else ``None``.

    ``directory`` is where the shell will be when `gh pr create` runs: the
    session directory, changed by any earlier ``cd``. ``head`` and ``repo`` are
    the values of ``--head``/``-H`` and ``--repo``/``-R`` when given.
    """
    if not _GH_PR_CREATE.search(command):
        return None
    try:
        tokens = _tokens(command)
    except ValueError as error:
        raise TargetError(f"cannot parse the pull-request command ({error})") from error

    directory = cwd
    # A `cd` inside `( ... )` runs in a subshell and ends with it.
    subshells: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_operator(token):
            for char in token:
                if char == "(":
                    subshells.append(directory)
                elif char == ")" and subshells:
                    directory = subshells.pop()
            index += 1
            continue
        if token == "cd" and index + 1 < len(tokens) and not _is_operator(tokens[index + 1]):
            directory = os.path.normpath(
                os.path.join(directory, os.path.expanduser(tokens[index + 1]))
            )
            index += 2
            continue
        if tokens[index : index + 3] == ["gh", "pr", "create"]:
            args = []
            for arg in tokens[index + 3 :]:
                if _is_operator(arg):
                    break
                args.append(arg)
            options = _options(args)
            return directory, options.get("--head"), options.get("--repo")
        index += 1
    # The text matched but no `gh pr create` invocation was found in it, for
    # example inside a quoted argument. Nothing will create a pull request.
    return None


def _git_output(directory: str, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", directory, *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _project_dir() -> str:
    """This repository's checkout: Claude Code's project directory, else the hook's own."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def _common_dir(directory: str) -> str | None:
    """The Git directory every worktree of ``directory``'s repository shares."""
    if not os.path.isdir(directory):
        return None
    common = _git_output(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(common) if common else None


def _github_repository(value: str) -> str | None:
    match = _GITHUB_REPOSITORY.search(value.strip())
    return match.group(1).lower() if match else None


def _own_repositories(project: str) -> set[str]:
    """The repositories this checkout's remotes name: ``origin``, and an upstream a fork tracks."""
    urls = _git_output(project, "config", "--get-regexp", r"^remote\..*\.url$") or ""
    return {
        own
        for line in urls.splitlines()
        if (own := _github_repository(line.split(None, 1)[-1])) is not None
    }


def is_other_repository(directory: str, repo: str | None) -> bool:
    """Whether the pull request belongs to a repository other than this one.

    ``repo`` (``--repo``, or an MCP call's owner and name) is compared with the
    repositories this checkout's remotes name, so a fork's pull request into
    the upstream it tracks is still this repository's; otherwise ``directory``
    is compared by the Git directory its worktrees share. Anything that cannot
    be told apart counts as this repository, so the gate still runs.
    """
    project = _project_dir()
    if repo is not None:
        own = _own_repositories(project)
        named = _github_repository(repo)
        if own and named is not None:
            return named not in own
    common = _common_dir(directory)
    return common is not None and common != _common_dir(project)


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
        directory, head, repo = parsed
        if is_other_repository(directory, repo):
            return None
        return resolve(directory, head)
    # An MCP create_pull_request tool names the repository and head branch.
    owner, name = tool_input.get("owner"), tool_input.get("repo")
    repo = f"{owner}/{name}" if owner and name else None
    if is_other_repository(cwd, repo):
        return None
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
