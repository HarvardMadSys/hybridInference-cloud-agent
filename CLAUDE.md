# CLAUDE.md

Read and follow [AGENTS.md](AGENTS.md) before changing this repository. It is
the canonical source for architecture, security boundaries, quality gates, and
pull-request workflow. Use [docs/development.md](docs/development.md) for the
detailed local-development commands.

## Before creating a pull request

1. Finish and commit the intended changes. The worktree must be clean before a
   pull request is created.
2. From the active worktree's repository root, run the required local gate in
   the same parallel mode used by CI:

   ```bash
   umask 022
   env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check
   ```

   The restrictive umask matches CI and keeps the Kata runtime security tests
   independent of a developer's shell defaults. `make check` runs the formatter
   and may change files. Review and commit any resulting changes, then run the
   gate again on the final commit.
3. Run every additional check required by the changed area. In particular,
   frontend changes need the checks in `apps/frontend`, and database changes
   need `make test-db` against the dedicated test database. See
   `docs/development.md#run-the-relevant-checks` for the full selection guide.
4. Do not create, request, or claim readiness for a pull request while a
   required check is failing or was not run. Record the commands and results in
   the pull-request description.

## Enforced Claude Code gate

The project `PreToolUse` hook in `.claude/settings.json` runs
`.claude/hooks/pre-pr-check.sh` before Claude Code creates a pull request with
`gh pr create` or an MCP `create_pull_request` tool. It blocks creation unless
the worktree is clean, the required local gate passes, and the gate leaves the
worktree clean. Fix the reported problem and retry; do not bypass or disable
the hook.

The hook enforces the repository-wide gate only. The area-specific checks in
step 3 remain required and must be run before invoking pull-request creation.
