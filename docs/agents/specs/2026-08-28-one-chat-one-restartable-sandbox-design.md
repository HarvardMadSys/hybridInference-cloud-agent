# Superseded: one-chat restartable sandbox proposal

- **Status:** historical proposal; not executable
- **Date:** 2026-08-28
- **Superseded by:**
  [`2026-08-31-native-session-fork-shared-workspace-design.md`](2026-08-31-native-session-fork-shared-workspace-design.md)

This proposal introduced Docker-owned retained chat containers, idle
`docker stop`, and native Agent CLI resume. Those pieces evolved into the
current sandbox-group design.

Its separate one-to-one executor, one-process-per-turn lifecycle, rollout
flags, host worktree compatibility path, checkpoint/replay fallback, and
Docker-clone Fork were removed. They must not be used as an implementation or
operations reference.

The current design represents an ordinary chat as a single-member sandbox
group. Fork immediately creates another native Agent CLI session in that same
container and workspace, and sibling chats may interact concurrently. See the
superseding document for the complete contract.
