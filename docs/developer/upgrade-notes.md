# Upgrade notes

What an operator upgrading an existing deployment needs to know about recent
schema migrations. New installations start from an empty database and can skip
this page. The deployment order itself, and how to roll back, are in
[rolling deployment order](operations.md#rolling-deployment-order) and
[deployment reliability](deployment-reliability.md).

## Before starting this release

Before starting this release, run `alembic upgrade head` through
`0086_conversation_read_receipts`. The supported deployment scripts run migrations
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
| `0086_conversation_read_receipts` | Adds an empty table for each owner's latest read completion per conversation, with revision and job/attempt indexes for incremental polling and foreign-key cleanup. It changes no existing rows and takes brief foreign-key definition locks on the referenced tables. Existing browser receipts import when that browser next opens the updated UI; a downgrade drops cloud receipts but leaves browser caches intact. |
| `0085_terminal_resume_index` | Builds a partial index on `agent_jobs` concurrently, holding only the turns whose terminals still wait to resume. It reads the whole job history without blocking the running API; an interrupted run can simply be repeated. Changes no row. |
| `0084_host_removal_move_out` | Adds `agent_runner_hosts.removal_moves_out` and `removal_leaves_unmoved`, false for every existing host, so a removal already pending keeps its earlier meaning: revoke once its running turns end. |
| `0083_host_move_out` | Adds the `move_out` purpose to the admission-hold check constraint. Changes no existing row. Its downgrade deletes any move-out holds; groups already moved keep their checkpoints. |
| `0082_team_environments` | Adds nullable team columns to environments and their bindings, and lets a repository default name any environment. Changes no existing row. Its downgrade refuses while a team environment exists, archived ones included. |
| `0081_snapshot_pushed_commit` | Adds `agent_sandbox_snapshots.pushed_commit_sha` and copies into it the commit of every snapshot that was never published. `published_commit_sha` is left as it was, so the API it replaces keeps working, and no notice fires. |
| `0080_user_ssh_servers` | Adds the owner SSH server tables; see [SSH servers](operations.md#ssh-servers). Changes no existing row. |
| `0079_teams` | Adds teams: members, single-use invitations, join requests and the team audit log. Nothing else refers to a team yet, so it changes no existing row and nothing anyone can run or read. |
| `0078_environment_snapshots` | Adds environment installation snapshots and the setup fingerprint on environment bindings that decides which snapshot a workspace may start from; see [environment installation snapshots](operations.md#environment-installation-snapshots). |
| `0077_sandbox_usage_days` | Adds the per-day container usage table and an owner index on `agent_sandboxes`, built without `CONCURRENTLY`. That table holds one row per retained container, so the brief write lock is short. |
| `0076_mcp_residency_fence` | Adds a nullable residency generation to MCP approval records; existing approvals keep it empty. |
| `0075_runtime_upgrades` | Adds two empty JSON columns for agent updates from the admin Settings page. |
| `0074_host_removal_pending` | Adds `agent_runner_hosts.removal_requested_at`, empty for every existing host. A downgrade drops it and leaves a host that was waiting to be removed draining. |
| `0073_sandbox_restore` | Adds the `cold` and `restoring` sandbox states and the start fence that binds a checkpoint to the run it ends; see [restoring a removed host's sandboxes](operations.md#restoring-a-removed-hosts-sandboxes). Existing checkpoints carry no fence and never move a group. Its downgrade refuses while any group is cold or restoring. |
| `0072_drop_old_memory_tables` | Drops the retired memory tables. A rollback to before `0070` finds them empty, without the earlier memory's portraits and preferences. |
| `0071_temporary_workspaces` | Drops `NOT NULL` from `agent_threads.repo` and `agent_environment_bindings.repo`, so conversations can start without a repository. Its downgrade refuses while any such conversation exists. |
| `0070_memory_cutover` | Moves the memory switches and saved preferences onto the durable-memory table. |
| `0069_agent_memories` | Adds the durable-memory table. |
| `0067_failed_snapshot_wake` | Only replaces a trigger function. |
| `0066_admin_job_listing_index` | Builds an index on `agent_jobs` concurrently. On a long job history it takes a while without blocking the running API; an interrupted run can simply be repeated. |
| `0051_host_drain_fence_epoch` | Backfills a non-null drain-fence epoch for every host. The database rotates that epoch whenever drain-relevant lifecycle, credential, placement or ownership state changes. Once the V2 control plane is activated, this revision and the `AGENT_DEPLOY_DRAIN_FENCE_V2` recipe marker are the application and schema rollback floor; see [rollback compatibility](operations.md#rollback-compatibility). |
| `0049_host_drain_fence` | Starts every existing host without a deployment drain owner or release receipt. The V1 marker it introduced remains only so an older orchestrator can perform the bridge upgrade; it is not a rollback target after V2 activation. |
| `0048_sandbox_plugins` | Adds `agent_threads.plugins`; existing chats default to no plugins. Rebuild the sandbox image so the updated CLI wrappers load selected plugins. |
| `0046_job_claim_retry` | Adds `agent_jobs.claim_retry_at`, which the worker claim query requires. Existing queued jobs remain immediately eligible. |

Older revisions that need a maintenance window are listed in
[applying migrations that are not backwards-compatible](operations.md#applying-migrations-that-are-not-backwards-compatible).
A database still stamped with the retired pre-2026-09-10 chain cannot be
upgraded directly; see
[preserving an old-chain database](operations.md#preserving-an-old-chain-database).

## When you add a migration

Add a row to the table above in the same pull request, saying what an operator
upgrading a live deployment needs to know: what existing rows it changes, how
long it can lock, and whether its downgrade refuses. Then update the head
revision named under *Before starting this release* and in
[ENVIRONMENT.md](../../ENVIRONMENT.md); a test checks that both name the
current head.
