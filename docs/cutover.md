# Staging cutover runbook

Moving the cloud agent off the HybridInference gateway and onto this service.
Written for **staging** (task H2); production (H3) reuses it unchanged, with
Murphy's approval gate in front and a user-facing announcement instead of a
Slack one.

**There is no compatibility proxy and no dual-write.** The old stack stops
serving agents and the new one starts; nothing bridges the two. That is a
deliberate choice, and two facts make it the honest one:

- **Control tokens were re-keyed.** E3 replaced the gateway's `API_KEY_SECRET`
  derivation with `AGENT_CONTROL_TOKEN_SECRET`, so every worker token in flight
  becomes invalid the moment a runner talks to the new control plane. An
  attempt cannot survive the switch. Draining is therefore *required*, not a
  courtesy — MIGRATION.md's deferred register carries this row for exactly this
  step.
- **Terminal sessions are process memory.** `Sandbox Manager` keeps live
  sessions in a dict on the sandbox manager process
  (`cloud_agent_host/sandbox_manager.py:658`,
  `self._sessions`). They are not in any
  database, so there is nothing to migrate and no honest way to preserve them.
  They are closed deliberately, and the announcement says so.

Copy this file into the cutover ticket and tick the boxes there, with real
timestamps. The blank log at the bottom is what the acceptance criterion asks
for.

> **Status, 2026-08-09: the cutover this runbook was written for has
> happened.** Staging served its first end-to-end job on 2026-08-06,
> production followed on 2026-08-07 (gateway network fix
> HarvardMadSys/hybridInference#1229 was the last piece), and H4
> (HarvardMadSys/hybridInference#1235) removed the gateway's agent code on
> 2026-08-09 — deployed to both environments the same day, old routes answer
> 404 on both. DR5 was then **amended by its owner: the old job history was
> deleted, not archived** (single-user payload; final `pg_dump` kept on each
> host as the undo net — hybridInference#1235 has the record). The run did not
> follow this file step by step — the log at the bottom records what actually
> happened, including the steps that were skipped and why. The runbook stays
> as the template for any future stack-to-stack move. The migration tooling it
> drove (`deploy/migrate/`, step 5) was **deleted on 2026-08-31** — never run,
> and its source tables no longer exist. A future move that wants the template
> recovers it from history: `git show 36767ea:deploy/migrate/migrate_connections.py`.

---

## 0. Preconditions

Do not start until every one of these is true.

- [ ] The new control plane is deployed and reachable, with its own database
      (`AGENT_DATABASE_URL`) — a database this service owns outright, not the
      gateway's.
- [ ] `ENVIRONMENT.md`'s control-plane variables are provisioned, including
      `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` (**generated once, kept for good** —
      regenerating it after the migration orphans every credential this runbook
      re-encrypts).
- [ ] The E11 smoke checklist ([smoke.md](smoke.md)) passes against the new
      stack with a *fresh* connection — i.e. before any migrated row is
      involved.
- [ ] The gateway's key material is available as a **file** on whatever machine
      runs step 5, and the machine has been decided (the decision was recorded
      in `deploy/migrate/README.md` § Where it runs, which was deleted with the
      tooling on 2026-08-31; see MIGRATION.md's H1 row).
- [ ] Backups of both databases are taken, and a restore has been rehearsed at
      least once. `pg_dump` of the gateway database and of the new one.
- [ ] Step 5 has been rehearsed end to end on a copy, with `--apply` and a
      second no-op run. Cutover day is not the first time this script writes.
- [ ] Rollback (§ Rollback below) has been walked once on staging: flip back,
      confirm the old stack serves, flip forward again.

### Check the gateway before scheduling anything

    python deploy/preflight/check_gateway.py https://staging.freeinference.org

Read-only — it fetches JWKS and reads a catalog, mints nothing. Exports
`GATEWAY_GRANT_DISPATCH_TOKEN` first and it exercises the authorized path too;
the token is read from the environment and never from `argv`, which is
readable by every process on the host. Exit code is 0 only when everything
passes, so it can gate a deploy step.

It exists because all four preconditions failed as a 404 when this runbook was
written (they pass on both environments as of 2026-08-07), and reading the
status code alone is how the routing gap was first misdiagnosed here.

### Blockers to check before scheduling

Each will make the cutover fail in a specific, non-obvious way if it has not
been dealt with. Struck-through rows have landed; they are kept because a
blocker that vanishes without a record reads as one that was never real.

| Blocker | Where | Why it stops this runbook |
|---|---|---|
| ~~**Identity is not configured on production**~~ | ~~The gateway's own environment~~ | ✅ **resolved on both sides.** Staging was configured 2026-08-05 (RSA key generated on the host into `IDENTITY_JWT_PRIVATE_KEY`, `IDENTITY_ALLOWED_REDIRECTS` matched exactly); production followed with the 2026-08-07 deploy — `GET /v1/identity/jwks` answers a key on both, re-verified 2026-08-09. Each host holds its **own** key; staging's was never copied. |
| ~~**`GATEWAY_GRANT_DISPATCH_TOKEN` is not set on production**~~ | ~~Both sides' environments~~ | ✅ **resolved on both sides, and both are proven by traffic rather than inference.** Staging: a real job minted, renewed ×4 and revoked a grant on 2026-08-06. Production: `/internal/agent-grants` answers 401 JSON from outside (route live, auth enforced) and its gateway has minted a real grant since the 2026-08-07 cutover. The values differ between the two environments, as required. |
| ~~**`AGENT_MCP_CONFIG` has no file yet.**~~ | ~~The control-plane host~~ | ✅ **no longer a blocker — the deployment-wide registry was retired** (#48). Jobs run without MCP tools by design until per-user connections land (#50 is the first half), so there is no file to copy and nothing to configure before the cutover. Job creation refuses any server a caller names rather than silently running without it. |
| ~~The runner sends control-plane calls and the sandbox's inference calls to one base URL~~ | ~~`runner.py` `TODO(E9)`~~ | ✅ **resolved** — the sandbox now carries two addresses and two credentials (`AGENT_GATEWAY_URL` + `agr`, `AGENT_SANDBOX_MCP_URL` + an `mcp`-scoped token), and a runtime asked for MCP with no relay address refuses rather than resolving it against the inference host. |
| ~~Inference grants (C5–C8) are not on the gateway~~ | ~~unmerged #1180~~ | ✅ **resolved** — #1180 through #1183, #1191 and #1195 are all on the gateway's `dev`. |
| ~~`/internal/model-catalog`, `/internal/users/{id}/status`, `/internal/mcp-registry` do not exist upstream~~ | ~~`gateway.py`~~ | ✅ **resolved for two of the three, and the third was removed by design.** The catalog and user-status routes exist and carry an alias table; `/internal/mcp-registry` is gone — MCP belongs to this service now, so there is nothing to ask the gateway about. |
| ~~Host enrollment exists on the control plane but **the runner does not use it**~~ | ~~`grep -rn AGENT_ENROLL_TOKEN host/` returns nothing~~ | ✅ **resolved** — `cloud_agent_host/enrollment.py` redeems the token and `runner.py` calls `ensure_enrolled` at startup. Staging's runner host carries a stored credential and heartbeats as `online`, so step 6 is enrolment, not environment variables. |
| **The pool's sandbox manager token must be the *same value* on the control plane and on every host** — and nothing checks that it is. Discovered 2026-08-06: with the reverse tunnel up and the address advertised correctly, every sandbox manager call still failed, because the two machines had been configured with two different 64-character secrets. | Each host's `.env` and the control plane's | `sandbox_manager_routing.py` states the design — one shared `AGENT_SANDBOX_MANAGER_TOKEN` for the whole pool — but a mismatch is not refused anywhere, it is a **401 on every file listing, terminal and git status** for jobs on that host. It reads like the tunnel is broken, and it is not; nothing distinguishes the two from the control plane's logs. Compare fingerprints (`sha256` of the value, never the value) on both machines before step 6, and re-check after any host is re-provisioned. |

---

## 1. Announce — T−24h

- [ ] Post to the team channel: date, window, and that **task history does not
      move**. Users keep their GitHub/GitLab connections and lose their job,
      thread and event history (decision DR5 — later amended to outright
      deletion, 2026-08-09).
- [ ] Say that open terminal sessions will be closed and any unfinished job
      will not be resumed on the other side.
- [ ] Ask that no long job be started inside the six hours before the window —
      the default agent timeout is 3600 s
      (`cloud_agent_host/runner.py:100`, `DEFAULT_AGENT_TIMEOUT_S`), and step 3 waits for
      whatever is running.

## 2. Freeze new-job creation on the old stack — T+0

**There is no feature flag for this.** Verified at the freeze commit: job
creation is gated only by the store being configured (503), the model
resolving, and repository entitlement. Emptying `AGENT_REPO_ALLOWLIST` does
*not* stop it, because a user with their own GitHub App installation is
entitled through `repos_for_user` regardless of the allowlist.

So the freeze is applied at the edge, in front of the gateway, on three routes:

```text
POST /v1/agent/jobs                      # create
POST /v1/agent/jobs/{job_id}/follow-ups  # next turn in a thread
POST /v1/agent/jobs/{job_id}/fork        # immediate same-container native-session fork
```

**And the source-control routes, which is easy to miss** — the freeze is about
what step 5 copies, not only about jobs. These write the two tables the
migration moves, so a connection made or removed after the snapshot is taken is
a change that lands nowhere: the old row is exported, the new one is not, and
the user arrives on the new stack with a connection they deleted or without one
they just made.

```text
POST   /v1/agent/integrations/github/connect
DELETE /v1/agent/integrations/github/connections/{installation_id}
POST   /v1/agent/integrations/gitlab/connect
DELETE /v1/agent/integrations/gitlab/connections/{connection_id}

# The older aliases, which write the same table. Freezing only the four
# above leaves a second door into `agent_repo_grants` open — and the UI is
# not the only caller: anything holding a gateway session can post here.
POST   /v1/agent/github/connect
DELETE /v1/agent/github/connect/{installation_id}
```

Verify the list against the deployment rather than against this file. It was
written from one reading of the router and missed the aliases above:

```bash
curl -s https://<gateway>/openapi.json \
  | python3 -c "import json,sys; [print(p) for p in sorted(json.load(sys.stdin)['paths']) if 'connect' in p]"
```

- [ ] Return `503` with a message naming the new domain. Leave every `GET` and
      the `/v1/agent/worker/*` routes open — the drain in step 3 needs them.
- [ ] Confirm with a real request that a create is refused and a job list still
      answers.
- [ ] Confirm a connect is refused too, and that Integrations still *lists*
      what a user already has — the freeze stops writes, not reading.
- [ ] Record the time. Everything after this is inside the window.

> If the deployment has no edge it can rule on, the fallback is to remove
> `app.include_router(agent_jobs.router)` from the gateway's
> `apps/backend/serving/servers/app.py:107` and redeploy — a code change, so it
> is slower to apply and slower to roll back, and it takes the read routes down
> with it. Prefer the edge rule.

## 3. Drain the old runner

The runner's only drain is the signal `docker stop` already sends.
`run_forever` (`cloud_agent_host/runner.py:2136`) loops on claims and exits in
three ways: a SIGTERM sets a stop flag, so it finishes the job in hand and then
returns; SIGINT (`KeyboardInterrupt`) still stops immediately; and a rejected
host credential stops it before the next claim. The SIGTERM half is what a
`docker compose up -d` replacement relies on — before it existed only
`KeyboardInterrupt` was caught and every replacement killed the job in flight.

What has not changed is that killing the process mid-job does not
finish the job: the lease expires after `AGENT_LEASE_TTL` (default 120 s), the
reaper supersedes the attempt within 30 s of that, and the job is requeued into
a queue nothing will ever serve again. Draining still means *waiting*.

- [ ] Watch the queue until it is empty and no attempt is running:

      ```sql
      SELECT state, count(*) FROM agent_jobs
       WHERE state IN ('queued','waiting','running','publishing') GROUP BY state;
      SELECT id, job_id, status, lease_expires_at FROM agent_attempts
       WHERE status = 'running';
      ```

- [ ] When both are empty, block `POST /v1/agent/worker/claim` at the edge too,
      so nothing can be taken after this point.
- [ ] Stop the old runner replicas and the sandbox manager on the runner host.
- [ ] Confirm the old publisher has nothing left: no job in `publishing`, and
      `published_pr_url IS NULL` for nothing that finished successfully.

      ```sql
      SELECT id, state, published_pr_url FROM agent_jobs
       WHERE state = 'succeeded' AND published_pr_url IS NULL;
      ```

- [ ] Record the drain duration. It is the number that decides whether the
      production window in H3 is realistic.

## 4. Back up

- [ ] `pg_dump` the gateway database (at minimum `agent_repo_grants`,
      `agent_gitlab_connections`; take the whole agent schema if it is cheap).
- [ ] `pg_dump` the new database, even though it is nearly empty — a rollback
      that has to *undo* a partial import wants a before-image.
- [ ] Note both file paths in the ticket.

## 5. Migrate the connections

This is `deploy/migrate/migrate_connections.py`.
It moves `agent_repo_grants` and `agent_gitlab_connections` and nothing else,
**decrypting with the gateway's key and re-encrypting with this service's**.
A copy would produce connections that list correctly and fail at first use.

- [ ] Dry run first. It writes nothing and refuses nothing that a real run
      would accept.

      ```bash
      export AGENT_MIGRATION_SOURCE_URL='postgresql://…/gateway'
      export AGENT_DATABASE_URL='postgresql://…/cloud_agent'
      export AGENT_SOURCE_CONTROL_ENCRYPTION_KEY='…'
      uv run python deploy/migrate/migrate_connections.py --old-key-file /run/gateway-key
      ```

- [ ] Read the report. `conflict` must be `0`; a non-zero count means a user
      connected on both stacks and the two rows must be reconciled by hand
      before proceeding.
- [ ] Apply.

      ```bash
      uv run python deploy/migrate/migrate_connections.py \
          --old-key-file /run/gateway-key --apply
      ```

      It runs `alembic upgrade head` against the target first. That is
      required: the store's `initialize()` is a no-op now and the baseline is
      the only thing that creates these tables.

- [ ] Both checksums read `MATCH`. They are computed over *decrypted* content,
      so a match is evidence the re-key round-tripped — the ciphertext cannot
      be byte-identical and is not compared.
- [ ] Run it once more, unchanged. `insert 0`, `refresh 0`, everything `same`,
      checksums unchanged. That is the idempotency acceptance.
- [ ] Delete the old key file and shred whatever shell history holds its path.
- [ ] Paste the report into the ticket.

> If a rehearsal import ran days earlier and a user reconnected on the old
> stack since, the script reports those rows as divergent and **refuses**.
> Re-run with `--refresh` once you have confirmed the source is the newer of
> the two.

## 6. Point the runner host at the new control plane

The control plane enrols hosts (`POST /v1/agent/host/register`, then
`/heartbeat`). A one-time `AGENT_ENROLL_TOKEN` is exchanged for a persisted
credential bound to the host name; claims and heartbeats authenticate with
that credential. Request-body host names are not identity.

- [ ] Set on the runner host:
      - `AGENT_CONTROL_PLANE_URL` → the new control plane's claim/register/
        heartbeat endpoint.
      - `AGENT_GATEWAY_URL` → the inference gateway reached by sandboxes.
      - `AGENT_ENROLL_TOKEN` → a new single-use token bound to this host; remove
        it after the persisted credential is written.
      - `AGENT_RUNNER_HOST` → the enrolled machine name.
      - `AGENT_SANDBOX_MANAGER_URL` and
        `AGENT_SANDBOX_MANAGER_TOKEN` → the advertised sandbox manager and the same
        environment secret configured on the control plane.
- [ ] Start the runner and the sandbox_manager. Confirm the startup line names the new
      host and the sandbox backend, and that preflight passed.
- [ ] Confirm on the new control plane that the enrolled host appears online
      with a fresh heartbeat and usable sandbox manager (`GET /admin/agent/runner-hosts`).
- [ ] Leave the pool **unpinned**. The new database's `agent_runner_policy`
      singleton has `active_host = NULL`, which means claiming is unrestricted;
      pinning to a host that has not polled yet is refused, and pinning at all
      turns every other machine off.

## 7. Bring the new stack into service

- [ ] Point the agent domain at the new web app and API (DR4).
- [ ] Confirm the identity round trip: sign in on the new domain, land back
      with a session.

## 8. Smoke test

Run the E11 checklist ([smoke.md](smoke.md)). At minimum, and in this order —
each step is the precondition for the next:

- [ ] **A migrated connection still works.** Open Integrations as a user whose
      GitLab connection was migrated and list their projects. This is the
      assertion the whole of step 5 exists for: a copied ciphertext passes
      every earlier check and fails *here*.
- [ ] A migrated GitHub grant still offers the same repositories in the
      composer.
- [ ] Create a job. It reaches `queued`.
- [ ] The runner claims it; events stream to the browser over SSE.
- [ ] The job reaches a terminal state and, if it produced a patch, a draft PR
      appears on the right branch.
- [ ] Open a terminal on the finished job's workspace; files list.
- [ ] Cancel a second job mid-run and confirm it settles.
- [ ] A *new* GitLab connection made on the new stack works — proving the new
      key is being used for writes as well as reads.

## 9. Banner on the old UI

- [ ] `/agents` on the old frontend shows a banner naming the new domain and
      saying history did not move.
- [ ] The three frozen routes keep returning `503` with the same message. Do
      not un-freeze them "just in case" — a job created on the old stack after
      this point runs nowhere and confuses whoever created it.

## 10. Close out

- [ ] Fill in the log below and attach both migration reports.
- [ ] Update MIGRATION.md's "Landed here" table: H1 ✅, H2 ✅.
- [ ] Start the soak. H3 needs at least one week of real dogfood use on this.
- [ ] Do **not** delete anything in the gateway yet. Removal is
      [docs/gateway-removal.md](gateway-removal.md) (H4), and it happens after
      *production* cutover, not this one.

---

## Rollback

> **The rollback window closed on 2026-08-09.** H4 deleted the gateway's agent
> routes and the amended DR5 dropped the old tables, so there is no old stack
> left to flip back to. The section stands as written for the next migration
> that needs one.

Rollback is available for as long as the old stack's data is untouched, and it
stays untouched: step 5 opens the gateway database with
`default_transaction_read_only = on` and never writes to it. There is nothing
to undo on that side.

What rollback costs: every job created on the new stack since step 7, and any
GitLab connection re-made there. Those rows stay in the new database and are
not copied back.

1. [ ] Stop the runner on the runner host.
2. [ ] Restore the runner's old control-plane/gateway endpoints and its old
       enrolled-host credential as one unit.
3. [ ] Point the agent domain back at the old frontend, or remove the redirect.
4. [ ] Remove the edge rules from steps 2 and 3, in that order: unblock
       `/worker/claim` **before** unblocking creation, or the first job created
       sits in a queue nothing is polling.
5. [ ] Start the old runner and sandbox_manager. Confirm a create → claim → finish
       round trip.
6. [ ] Remove the banner.
7. [ ] Leave the new database as it is. Re-running step 5 later with
       `--refresh` reconciles whatever moved in the meantime; wiping it loses
       the rehearsal evidence for nothing.

**What rollback does not recover:** in-flight attempts, on either side. They
were drained in step 3 and there is nothing to resume. Terminal sessions are
gone in both directions for the same reason they were gone forward — they only
ever existed in a process.

**Test this section before the real run.** The acceptance criterion for H2 is a
rollback that has actually been executed once: flip back, confirm the old stack
serves a job end to end, then flip forward again and re-run step 5 (it is
idempotent, so the second forward pass is a no-op on the data).

---

## Log

Filled retroactively on 2026-08-09. The cutover ran ahead of this runbook, so
the honest record is what happened, what was skipped, and why — a blank row
would read as "not yet" when the truth is "never, deliberately".

| Step | Planned | Actual (UTC) | By | Notes |
|---|---|---|---|---|
| 1. Announce | T−24h | skipped | — | single-user deployment; the only user ran the cutover |
| 2. Freeze creation | T+0 | never applied; closed 2026-08-09 by deletion | Murphy + Claude | no edge rule was ever put up — the old routes stayed answerable (401) until H4 removed them; production's tables held zero rows, so nothing leaked through the gap |
| 3. Drain complete | | n/a | — | no live attempts existed on the old stack; the co-located staging runner had been failing preflight for weeks and rtx6000b was already enrolled with the new control plane |
| 4. Backups | | 2026-08-09 | Claude | final `pg_dump` of the 11 tables on both gateways before the DR5-amended deletion; kept in the operator's home directory on each host |
| 5. Migration dry run | | skipped | — | payload was 1 GitHub connection + 0 GitLab rows |
| 5. Migration applied | | skipped — fallback taken, then deletion | Murphy | the one GitHub connection was re-made by hand on 2026-08-06 (DR5's reconnect fallback); DR5 was amended 2026-08-09 and the source tables dropped |
| 5. Idempotency re-run | | n/a | — | nothing was migrated |
| 6. Runner re-pointed | | 2026-08-06 | Murphy + Claude | by enrolment, not env edit: rtx6000b redeemed an enrol token, heartbeats `online`; sandbox manager reachability needed #41 plus aligning the two hosts' `AGENT_SANDBOX_MANAGER_TOKEN` values |
| 7. Domain switched | | staging 2026-08-06, production 2026-08-07 16:59 | Murphy | same-origin `/agents` rewrites through the console (no separate domain — DR4 as deployed); production needed hybridInference#1229 |
| 8. Smoke passed | | 2026-08-06, partial | Claude | `ajob_8b0be5f9e7ff9ad5`: create → remote claim → sandbox manager workspace → tool-calling run → succeeded, with grant mint / renew ×4 / revoke. **Still unproven: publish, mid-run cancel, the fail-closed trio (smoke.md § 6).** |
| 9. Banner up | | n/a | — | the rewrite made the old UI unreachable the moment it deployed, and H4 then deleted it; no banner ever shipped |
| Rollback rehearsed | before the run | never — window closed 2026-08-09 | — | see the note at the top of the Rollback section |
