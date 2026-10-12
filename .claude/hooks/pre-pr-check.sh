#!/usr/bin/env bash

# Claude Code passes the PreToolUse event as JSON on stdin. Drain it before
# starting test processes so none of them can inherit unread hook input.
cat >/dev/null

set -uo pipefail

deny() {
  printf 'Pull request creation blocked: %s\n' "$1" >&2
  exit 2
}

command -v git >/dev/null 2>&1 || deny "git is not available."
command -v make >/dev/null 2>&1 || deny "make is not available."

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || \
  deny "the hook is not running inside a Git worktree."
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
