# Superseded: session-resident harness threads

- **Status:** historical proposal; not executable
- **Date:** 2026-08-17
- **Superseded by:**
  [`2026-08-31-native-session-fork-shared-workspace-design.md`](2026-08-31-native-session-fork-shared-workspace-design.md)

This proposal explored platform checkpoint/export, cold restore, transcript
replay, and short-lived per-turn sandboxes. Those mechanisms are not part of
the current executor.

The implemented design keeps native Agent CLI state in a retained Docker
container, keeps a live process per active chat member, uses native resume when
that process must be replaced, and performs immediate runtime-native Fork in a
shared container/workspace. Portable storage and cross-host restore remain a
separate future design; they must not be inferred from this historical file.
