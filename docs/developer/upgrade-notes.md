# Upgrade notes

## Before starting this release

Before starting this release, run `alembic upgrade head` through
`0086_sandbox_retry_budget`. The supported deployment scripts run migrations
before replacing the API; every migration listed here is additive and safe
under the running previous release unless it says otherwise. The few
transitions that need stopped writers are listed in
[applying migrations that are not backwards-compatible](operations.md#applying-migrations-that-are-not-backwards-compatible).

After migrating, start the main control-plane listener with its existing
complete environment: it imports newly registered settings once, as described
in [Admin Settings](operations.md#admin-settings).

## Recent migrations

Newest first.

| Migration | What it changes |
|---|---|
| `0086_sandbox_retry_budget` | Adds the automatic deletion retry window and attempt count. Existing pending requests begin their 24-hour window at their request time; retries stop after eight claims with bounded backoff. An administrator resumes an archived terminal request under its existing request ID and verified receipt. A terminal prearchive request is atomically replaced through the authorized deletion path, giving the host a fresh archive identity after an acknowledged abort. |
| `0085_sandbox_deletion_attempts` | Adds the admin-only, secret-free attempt history for sandbox deletion. |
| `0084_host_removal_move_out` | Adds `agent_runner_hosts.removal_moves_out` and `removal_leaves_unmoved`, false for every existing host, so a removal already pending keeps its earlier meaning: revoke once its running turns end. |
