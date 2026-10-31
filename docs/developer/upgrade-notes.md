# Upgrade notes

What an operator upgrading an existing deployment needs to know about recent
schema migrations. New installations start from an empty database and can skip
this page. The deployment order itself, and how to roll back, are in
[rolling deployment order](operations.md#rolling-deployment-order) and
[deployment reliability](deployment-reliability.md).

## Before starting this release

Before starting this release, run `alembic upgrade head` through
`0108_local_account_parity`. The supported deployment scripts run migrations
before replacing the API; every migration listed here is additive and safe
under the running previous release unless it says otherwise. The few
transitions that need stopped writers are listed in
[applying migrations that are not backwards-compatible](operations.md#applying-migrations-that-are-not-backwards-compatible).

After migrating, start the main control-plane listener with its existing
complete environment: it imports newly registered settings once, as described
in [Admin Settings](operations.md#admin-settings).

## Independent accounts cutover

This release removes HybridInference Gateway sign-in, admission and inference.
Cloud Agent keeps its own accounts, and every task runs on a model connection
its owner adds with their own provider key. A database from an earlier
release is keyed by Gateway owner IDs that no account holds, so it is not
upgraded, converted or mapped: the release starts on a **new, empty
database**, and the old one is kept untouched as the archive and rollback
source. People register again, apply through the waiting list and add their
model connections. Nothing migrates old tasks, connections or identities.

1. **Archive the old deployment.** Drain it, stop every writer, and take a
   custom-format `pg_dump` of the old Agent database with its checksum, the
   old configuration and root keys. Keep the old database itself; do not
   delete, downgrade or rebind it. Optionally confirm it is what this release
   refuses, read-only:

   ```bash
   AGENT_DATABASE_URL='<old database>' python -m cloud_agent.identity_namespace --check
   ```

   It exits non-zero, names this runbook, and changes nothing.

2. **Create a new, empty Agent database** and point `AGENT_DATABASE_URL` at
   it. The same check must now report an empty database that will be bound on
   first start. `deploy/control-plane/deploy.sh` runs it read-only before it
   migrates, and stops if it refuses.

3. **Give the deployment its own secrets**, none of them shared with the
   Gateway or with another environment: `AGENT_LOCAL_SESSION_SECRET`,
   `AGENT_SETTINGS_ENCRYPTION_KEY`, `AGENT_CONTROL_TOKEN_SECRET`,
   `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` (each at least 32 random bytes,
   `openssl rand -hex 32`) and a separate `ADMIN_TOKEN`. Hosts enroll again
   with new enrollment tokens.

4. **Remove every Gateway variable.** The control plane and `deploy.sh` refuse
   `AGENT_AUTH_MODE`, `GATEWAY_*` (issuer, JWKS, authorize, base URL, grant
   path, binding and dispatch token), `IDENTITY_*`,
   `AGENT_SESSION_REDIRECT_URI`, `AGENT_GATEWAY_URL`,
   `AGENT_GATEWAY_SSH_HOST`, `AGENT_WEB_GATEWAY_URL`, `AGENT_WEB_FEATURE_RAG`
   and the on-call dispatch variables, and a host refuses `AGENT_GATEWAY_URL`,
   `AGENT_GATEWAY_HOSTNAME` and the inference tunnel's `AGENT_TUNNEL_*`. Set
   `AGENT_WEB_BASE_URL` (the base of every emailed link) and
   `AGENT_PUBLIC_API_URL` (this API's public address, which
   `AGENT_SESSION_REDIRECT_URI` used to give).

5. **Mail.** An existing SMTP transport, such as the one HybridInference sends
   with, may be reused: set `AGENT_SMTP_HOST`, `AGENT_SMTP_PORT` (587),
   `AGENT_SMTP_SECURITY=starttls`, `AGENT_SMTP_USERNAME`,
   `AGENT_SMTP_PASSWORD` and `AGENT_SMTP_FROM` from the operator's secret
   store, never from the repository. Only the transport is shared: every link
   in a message is built from `AGENT_WEB_BASE_URL` and signed with this
   deployment's own `AGENT_LOCAL_SESSION_SECRET`, and the account data is
   this database's.

6. **Initial administrators.** List the verified addresses of the people who
   administer the deployment in `AGENT_LOCAL_ADMIN_EMAILS`
   (`admin-one@example.org,admin-two@example.org` are placeholders; production
   names exactly its two administrators, staging its own list). A listed
   account is approved and made an administrator only once its address is
   proven, by a link sent to it. There is no grant to the first person who
   signs up. Alternatively, or for the very first one, exchange the setup code
   the control plane logs at startup (`agent_local_setup_pending`) at
   `/setup/administrator`, or use `python -m cloud_agent.accounts.bootstrap_admin`
   ([first administrator](local-accounts.md#the-first-administrator)).

7. **Deploy and verify.** Run the usual deployment. Then: sign-up sends a
   verification link at `AGENT_WEB_BASE_URL`; an administrator approves an
   application; the applicant adds a model connection and runs a task. Turn on
   `AGENT_MODEL_CONNECTIONS_ENABLED` once every host and control-plane replica
   runs this release; nothing runs before it.

**Rollback.** Before the new deployment has taken writes that matter, stop it
and redeploy the previous release against the archived database and
configuration, which were never changed. After new writes, keep the new
database: it holds accounts the old release cannot read, and the two
databases are never merged.

## Conversation history read contract

This release changes `GET /v1/agent/jobs/{job_id}/thread` to `history_version: 2`.
Deploy the frontend with the API: the existing message/job arrays and their
chronological order remain, but they are a bounded page rather than the whole
conversation. Small histories remain complete. The default page contains at
most 50 turn/message entries, the maximum `limit` is 100, and the serialized
response is at most 512 KiB. `current_job_id` identifies the newest turn
independently of the page; `anchor=true` starts at the selected job instead.

Pass `next_cursor` as `cursor` to load older history explicitly. For refreshes,
retain older pages and pass `sync_cursor` as `after`; follow each returned
`next_cursor` as `after` until it is null. This rereads the two mutable tail
turns so an in-flight write cannot disappear behind a newly allocated ID.
Merge messages by ID and jobs by ID/revision, preserving turn order and placing
each opening prompt before its replies and mid-turn messages. Cursors are
scoped to the owned, undeleted conversation on every request.

Messages longer than 4,096 Unicode characters carry `content_truncated` and
`next_offset`. Read more through
`GET /v1/agent/jobs/{job_id}/thread/messages/{message_id}?offset=...`; each
request returns at most 16,384 characters and offsets count Unicode characters,
not UTF-8 bytes or JavaScript UTF-16 units. `thread_preview` marks a job whose
wide fields need the existing single-job detail read. A false
`pending_messages_complete` means read the unchanged queue endpoint,
`GET /v1/agent/threads/{thread_id}/messages`; an omitted queue snapshot is not
an empty queue. This changes the owner-facing read API only, never runtime
session recovery or transcript replay.

## Recent migrations

Newest first.

| Migration | What it changes |
|---|---|
| `0107_local_accounts` | Creates the tables accounts use: accounts, password sign-in, waiting-list admissions, server-side sessions, emailed tokens, abuse counters, access terminations and the account identity marker. It changes no existing row and locks no existing table. A downgrade drops them, and with them every account. |
| `0108_local_account_parity` | Local accounts only. Splits `agent_local_password_logins` into sign-in identities (email, login name, phone) and optional password credentials, carrying every account over with its verification time and hash (a verified email is recorded as verified by its link, the only way `0107_local_accounts` had); adds the access view every SQL gate reads, the `deleted` account state with the account's name, note, language and sign-in and deletion times, phone codes, browser sessions, sign-in events, the signup domain list, first-run setup and administrator mail to accounts. It touches only the account tables. A downgrade restores the email-and-password table and drops the new tables, with the sign-in events and mail history in them; it refuses while any account signs in some other way or is deleted, rather than dropping that account. |
| `0106_user_model_connections` | Adds `agent_model_connections`, `agent_model_connection_checks` and `agent_model_connection_scan_keys` tables for owners' own inference endpoints (Chat Completions, Responses or Anthropic Messages), nullable source columns `agent_jobs.model_connection_id` and `agent_pending_messages.model_connection_id`, and a separate `agent_model_connection_residencies` table binding a resident member's process to one connection revision for one residency generation (a separate table, so a running previous release's prepared statements over the membership table keep working). Nothing is rewritten. The feature stays off until `AGENT_MODEL_CONNECTIONS_ENABLED` is set, and nothing runs before it. A downgrade refuses while any connection exists, or any turn, waiting message or resident member names one, finished conversations included ([owner model connections](operations.md#owner-model-connections)). |
| `0105_job_event_usage_summary` | Adds a nullable `usage_summary` column to `agent_job_events`. Adding it rewrites no rows and holds its lock only for the catalog change. New events store the projection of their payload that usage reads need, and those reads take it from there; events stored before the column existed are projected when read, as before. A downgrade drops the column, and usage reads project every payload again. |
| `0104_notification_change_keyset` | Builds a `(user_id, kind, change_xid, entity_id)` index concurrently before dropping the preceding owner/XID index, so notification delta continuation pages seek directly instead of scanning the whole snapshot interval. It changes no row and does not block foreground writes. Existing v1 continuations finish in their previous kind/identity order before advancing to v2. The previous API remains functionally compatible during migration, although its delta reads may use a less selective index until it is replaced. A downgrade restores the preceding index concurrently. |
| `0103_terminal_resume_keyset` | Rebuilds the partial terminal-resume index on `(updated_at, id)`, matching the replay keyset so every bounded continuation seeks directly instead of sorting equal-time rows again. The online build does not block foreground writes and changes no row. A downgrade restores the preceding one-column index. |
| `0102_admin_sandbox_marker_order` | Replaces revision 0097's membership trigger so that it locks a membership's sandbox group before marking the group changed, the order every write to the group already takes. Before it, settling, abandoning, ending, releasing or reaping a turn could deadlock with the admin terminal check or a workspace start on the same group, and Postgres aborted one of the two. Only the trigger function changes: no table is locked or rewritten, and the previous release's statements run unchanged under it. A downgrade restores the 0097 trigger. |
| `0101_notification_state_deltas` | Adds coalesced per-owner job/thread change markers, an active-job index, and a partial recent-terminal marker index. Notification clients bootstrap active and recent terminal work in bounded keyset pages from a request-time database clock baseline, then request bounded snapshot deltas; no historical jobs are backfilled or replayed. Markers age out lazily after 24 hours and stale cursors explicitly rebuild. The previous push stream remains available. A downgrade drops only derived state and restores push-only trigger behavior. |
| `0100_memory_archive_pages` | Builds partial owner/scope indexes for active memories and archive cursor pages concurrently. Existing memory rows are unchanged. Online builds avoid blocking foreground writes; interrupted builds can be retried. Offline SQL uses ordinary index builds. Downgrade drops only these indexes. |
| `0099_thread_stats_rollups` | Adds a derived, versioned per-attempt cache for owner thread statistics. Missing or stale attempts reduce their compact event projections in bounded 1,000-row pages; unchanged reads compare the newest indexed sequence and reuse the cache without transferring historical event rows. Event storage and the authoritative billing ledger are unchanged. A downgrade drops only this rebuildable cache. |
| `0098_measurement_counts` | Backfills exact profiling counts into at most 64 counter shards per run and installs transactional accounting triggers. The historical scan holds a write-conflicting lock on measurements until migration commit; schedule this one-time write pause for the size of retained history. Raw samples and whole-run summaries are unchanged. Downgrade removes only counters and triggers. |
| `0097_admin_sandbox_deltas` | Adds coalesced metadata-change markers and short-lived, content-free admin inventory snapshot pages. Admin refreshes request only changed identities after their initial snapshot; expired cursors explicitly resynchronize. Triggers cover sandboxes, membership counts, origin titles, and host heartbeats without a fleet-wide write lock. Deploy the migration before enabling the new UI; older list clients remain supported. A downgrade discards only derived inventory state and its triggers. |
| `0096_snapshot_title_context` | Adds a bounded JSON title context to immutable sandbox snapshots, so a publisher claim never rebuilds or transfers the sandbox's unbounded transcript. Each capture continues from the newest earlier snapshot of its group that has a context, adding the turns of every snapshot since, including one whose capture failed. Existing snapshots start with an empty context; a snapshot still holding it when a publisher claims it, captured before this migration or by the previous release during the rollout, has its context built and stored at that claim, so its draft is titled from the conversation rather than from its changed files. These reads look back over at most 24 earlier snapshots and read the newest 24 turns; the older turns of a longer gap are left out of the title. A downgrade drops only this derived context. |
| `0095_memory_retention` | Builds partial archived-time and non-null superseded-by indexes concurrently, without changing memory rows or the 90-day retention policy. Online builds avoid blocking foreground writes; interrupted builds can be retried. Offline SQL uses ordinary index builds. Downgrade drops only these indexes. |
| `0094_thread_message_pages` | Builds a concurrent index on each job's messages in the existing prompt-first order, so history pages seek a bounded set of message IDs before reading bodies. Changes no row. An interrupted online build is dropped and retried; offline conversion builds the index plainly inside its transaction. A downgrade drops the index concurrently. |
| `0093_recent_requests` | Adds an owner/time-indexed projection of eligible recent requests and message/thread maintenance triggers. Backfills existing eligible messages once; trigger installation holds write-conflicting locks while the backfill runs, so schedule for a low-write period on a large history. Source messages are unchanged. Downgrade removes only the projection and triggers. |
| `0092_targeted_work_wakes` | Replaces the `agent_work_available` trigger function so that its notification names the one host that can take a retained conversation's turn, and only that host's waiting runner is woken. It changes no table or row. A control plane from the previous release ignores the name and wakes every host, as before. A downgrade restores the empty notification. |
| `0091_env_queued_runs_index` | Builds a partial index on `agent_environment_runs` concurrently, holding only the runs still queued for a Host to claim. Idle environment-capable Hosts claim every few seconds and the prebuild maintenance pass repeats its scan; without the index every claim read the table's whole test and prebuild history, which no partial index covered since `0078`. It reads the run history without blocking the running API's inserts, and an interrupted build leaves an invalid index that the next run drops and rebuilds. Offline SQL builds it plainly instead, because a conversion transaction cannot run a concurrent build. Changes no row. A downgrade drops the index concurrently; runs already claimed stay claimed. |
| `0090_user_model_preferences` | Adds an empty table holding each owner's pinned default model for new tasks. It changes no existing row; owners without a pin keep following the model their last task used, and the administrator's default model is a setting rather than a column. A downgrade drops the pins only. |
| `0089_superseded_removals` | Adds an empty table recording each authorization to remove a container that a moved sandbox group left behind on a Host being released, and when the Host confirmed the removal. It changes no existing rows. A downgrade drops the records; containers already removed stay removed. |
| `0088_sandbox_retry_budget` | Adds the automatic deletion retry window and attempt count. Existing pending requests begin their 24-hour window at their request time; retries stop after eight claims with bounded backoff. An administrator resumes an archived terminal request under its existing request ID and verified receipt. A terminal prearchive request is atomically replaced through the authorized deletion path, giving the host a fresh archive identity after an acknowledged abort. |
| `0087_sandbox_deletion_attempts` | Adds the admin-only, secret-free attempt history for sandbox deletion. |
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
A database from before the independent-accounts release, on either chain, is
not upgraded; see [the cutover](#independent-accounts-cutover).

## When you add a migration

Add a row to the table above in the same pull request, saying what an operator
upgrading a live deployment needs to know: what existing rows it changes, how
long it can lock, and whether its downgrade refuses. Then update the head
revision named under *Before starting this release* and in
[ENVIRONMENT.md](../../ENVIRONMENT.md); a test checks that both name the
current head.
