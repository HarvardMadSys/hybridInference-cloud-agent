#!/usr/bin/env bash

set -uo pipefail

deny() {
  printf 'Pull request creation blocked: %s\n' "$1" >&2
  exit 2
}

command -v git >/dev/null 2>&1 || deny "git is not available."
command -v make >/dev/null 2>&1 || deny "make is not available."
command -v python3 >/dev/null 2>&1 || deny "python3 is not available."

# Claude Code passes the PreToolUse event as JSON on stdin. Read it in full
# before starting test processes so none of them can inherit unread hook input.
# pr_target.py decides whether the call creates a pull request at all (this
# hook also starts for commands Claude Code cannot classify) and which checkout
# holds the commit under review: the hook starts in the session directory, but
# the pull request may be created from another worktree.
target="$(python3 "$(dirname "${BASH_SOURCE[0]}")/pr_target.py")" || \
  deny "cannot determine the checkout under review."
[[ "$target" == "skip" ]] && exit 0

repo_root="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || \
  deny "$target is not inside a Git worktree."
cd "$repo_root" || deny "cannot enter the active worktree root: $repo_root"

worktree_status="$(git status --porcelain --untracked-files=all)" || \
  deny "cannot inspect the active worktree."
if [[ -n "$worktree_status" ]]; then
  printf '%s\n' \
    'Pull request creation blocked: the worktree is not clean.' \
    'Commit or stash the changes, then retry so the gate validates the exact commit to be reviewed.' \
    '' \
    "$worktree_status" >&2
  exit 2
fi

printf '%s\n' \
  "Running the local pull-request gate in $repo_root:" \
  "  umask 022" \
  "  env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check" >&2

umask 022
if ! env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check >&2; then
  deny "the required local gate failed. Fix the failure and run pull-request creation again."
fi

worktree_status="$(git status --porcelain --untracked-files=all)" || \
  deny "cannot inspect the worktree after the local gate."
if [[ -n "$worktree_status" ]]; then
  printf '%s\n' \
    'Pull request creation blocked: make check changed the worktree.' \
    'Review and commit the generated changes, then retry so the final commit is tested.' \
    '' \
    "$worktree_status" >&2
  exit 2
fi

printf 'Local pull-request gate passed.\n' >&2
exit 0
