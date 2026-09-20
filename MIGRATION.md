# Migration record

> **Status: the extraction is complete.** Production cut over on 2026-08-07 and
> the gateway's own agent code was deleted on 2026-08-09. This document is the
> **historical record** of how the move was run, plus the register of follow-ups
> it deliberately left open — see "Deferred behaviour register" and "Open
> decisions". It is no longer a live task list.
>
> The rules that governed the move are retired; [AGENTS.md](AGENTS.md) § 5 says
> which of them are permanent. The "Landed here" table at the bottom is the
> answer to "what is actually done" — the plan describes intent, this describes
> reality.

This repository is a **clean snapshot** of the cloud agent feature extracted
from `HarvardMadSys/hybridInference`. It does not import that repository's git
history.

## Licence and provenance

This repository is **MIT**, carrying the source repository's licence text and its
copyright line — `Copyright (c) 2026 Harvard SEAS` — unchanged.

It was briefly created as Apache-2.0. That was wrong twice over. Apache-2.0 can
*contain* MIT code, but only while retaining the MIT notice, which a bare licence
swap drops. More importantly the copyright holder is Harvard SEAS, so
relicensing is not a decision this project can make on its own. Keeping MIT
avoids the question entirely and is the least surprising outcome for anyone who
already holds a copy of the original.

If a future relicence is genuinely wanted, it needs the copyright holder's
agreement first, not a change to this file.

## Why the history was not carried over

The source repository is private and its history is not a publication artifact —
it contains deployment details, internal hostnames, and credential-rotation
churn from a live service. Filtering that history well enough to publish is
strictly harder than starting clean, and a filter that is only *mostly* correct
publishes exactly the commits it failed to catch. Provenance is preserved in
this file and in commit messages instead.

## Source

| | |
|---|---|
| Source repository | `HarvardMadSys/hybridInference` (private) |
| Freeze commit | `764a6f97477504deb4542e1c28c3128ff9601c94` (2026-08-03) |
| Manifest | `docs/agents/plans/cloud-agent-split-manifest.md` in the source repo |
| Plan | `docs/agents/plans/2026-08-03-cloud-agent-repo-split.md` in the source repo |

Every migration task copies from this commit. Fetch a file with
`git show 764a6f97:<path>` rather than from the source repo's `dev`, which will
have moved on.

The freeze waited on five pull requests — #1147 (egress allowlist proxy), #1148
(MCP via the gateway), #1167 (runner-host lock order), #1168 (Kata
provisioning), #1170 (remote runner relay) — because anything unmerged at freeze
time would have to be re-ported by hand. All five landed on 2026-08-03, adding
13 files to the manifest.

One of them changed the shape of the split. The MCP proxy authenticates tool
calls with the same capability token and store fence as model calls, so it stays
on the gateway side and inference grants must describe MCP access as well as
models. See the manifest's coordination notes.

## Post-freeze drift register

The freeze promise is "copy from `764a6f97` and you copy what runs on dev".
Two files have drifted since — both formatting-only, verified by
`git diff 764a6f97..dev`:

| File | Drift | Consumer task |
|---|---|---|
| ~~`apps/frontend/src/components/features/agents/IntegrationsView.tsx`~~ | ~~Prettier prose re-wrap, 2 lines~~ ✅ reconciled in F2 — copied from the freeze, formatted with this repo's Prettier, and the result is byte-identical to `origin/dev`. Formatting-only confirmed. | — |
| ~~`tests/servers/test_agent_jobs_api.py`~~ | ~~two blank lines restored between test sections (the ruff fix that rode in with upstream #1177's merge)~~ ✅ reconciled in E10 — the freeze copy run through this repo's `ruff format` (0.15.12) is byte-identical to `origin/dev`'s copy. Formatting-only confirmed. | — |

Rule for those tasks: copy from the freeze commit as usual, then run this
repo's formatter and confirm the result matches dev's copy — the diff against
dev must stay formatting-only. Any *new* drift discovered during a task gets a
row here before the task proceeds; **semantic** drift additionally needs
Murphy's sign-off, because it means the freeze was violated upstream.

## Deferred behaviour register

Differences against the source repo that are deliberate now and owed a fix by
a named task. Each entry lists the tripwire that keeps it from being
forgotten.

| Deferred | Owner | Tripwire |
|---|---|---|
| ~~Host logging is unconfigured until an entrypoint configures it.~~ | ~~**D6**~~ ✅ closed — `configure_process_logging()` is called from the runner's `main()` (the one sanctioned diff beyond the import rewrite), and `test_configure_process_logging_makes_info_reach_a_handler` is the required positive test. Import-time behaviour is unchanged and still pinned. | — |
| ~~`workspace_snapshot.py` moved in D3 without its test file.~~ | ~~**D6**~~ ✅ closed — `test_agent_workspace_snapshot.py` moved with the runner, unmodified. | — |
| ~~`workspace_paths.py` moved in D3 without dedicated coverage: its tests live inside `test_agent_workspace_broker.py` (D5).~~ | ~~**D5**~~ ✅ closed — the broker test file arrived in D5 and covers it. | — |
| The store's `initialize()` is a no-op. It used to create the schema **and run five data backfills** (threads and thread messages for jobs predating them, the singleton runner-policy row, `thread_id`/`turn_no` on legacy jobs) on every boot. All forty statements moved verbatim to `migrations/versions/0001_baseline.py`. | **E6** must not restore startup DDL when it moves the bootstrap call site; **H1** must run `alembic upgrade head` against the imported database. ✅ it does — `migrate_connections.py` runs it before the import, because the store no longer creates these tables and nothing else would. **Correction to this row's reasoning:** the five backfills write `agent_threads`, `agent_thread_messages`, `agent_jobs` and `agent_runner_policy`, and H1 migrates *none* of those (DR5 leaves history behind). They are no-ops on the imported data. The upgrade is required for the schema, not for the backfills. **Two renames since:** the revision became `0001_schema_baseline` in the 2026-09-10 rewrite, and the tripwire moved to `tests/unit/test_schema_baseline.py` (`test_the_store_executes_no_ddl`). | `tests/unit/test_schema_baseline.py` pins the table inventory and that the store executes no DDL. |
| The `model` token scope still exists in `tokens.py`. It no longer has a **minter** — E9 landed, and the claim asks the gateway for a grant instead — but it is still *used*: `parse_worker_token` accepts it and `_worker_claims` turns it into the 403 `insufficient_scope` that keeps any narrower credential off the worker surface. | ~~**E9**~~ — E9 deliberately did **not** remove it. The register row's condition was "if nothing uses it any more", and something does. Removing it also changes three ported test files' outcomes (`test_agent_job_tokens.py` mints both scopes; `test_agent_jobs_api.py` mints one by hand in `test_sandbox_token_stays_out_of_owner_routes`), which is a parity break migration rule 3 forbids a task from taking on its own. **Whoever prunes the token module post-cutover**, together with those assertions, deliberately. | This row; `tests/unit/test_agent_job_tokens.py` still exercises both scopes, so deleting one fails loudly. |
| Re-keying control tokens invalidates every token in flight. An attempt holding one signed by the gateway's root is an attempt from before the migration. | ~~**H2**~~ ✅ written into the runbook and executed with no old attempt in flight (2026-08-06). The current runner now handles SIGTERM as a drain: it stops polling, finishes the admitted turn within the Compose grace period, and exits before the broker is replaced. | This row; `docs/cutover.md` § 3 and its log, plus the retained host deployment tests. |
| The obsolete `test_agent_jobs_lifecycle.py::test_full_lifecycle_create_to_published_pr` xfail targeted worker publish routes removed by upstream PR #1098. | **Resolved.** The nonexistent route test is gone rather than advertising an unresolved failure; publication remains owned by `publish_worker`. | `test_agent_jobs_api.py::test_a_worker_cannot_write_the_publish_record` pins the security property, and the local end-to-end smoke covers the supported lifecycle. |
| **One broker token for the whole pool.** G5 routes each request to the broker on the host that ran the attempt, but host rows carry a URL and no credential — every broker still authenticates the single `AGENT_WORKSPACE_BROKER_TOKEN`. So a compromised host's broker token opens every other host's broker. Doing half of it here would have looked like the problem was solved. | **Whoever adds per-host secrets** — it is a distribution problem (issue at enrollment, rotate, deliver), not a routing one. | `test_every_broker_shares_the_one_token_today` in `tests/unit/test_broker_routing.py` asserts the current state, so the day it changes it changes visibly. |
| Source-control credentials are encrypted with `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`, not the gateway-derived key they were written under. | ~~**H1**~~ ✅ **tooling written** — `deploy/migrate/migrate_connections.py` decrypts with the old material and re-encrypts with the new one. ~~Still owed: *running* it (H2 step 5), and deciding which machine holds both keys for that one run.~~ ✅ closed without running (2026-08-09): the payload was one GitHub row and zero GitLab rows, the connection was re-made by hand instead, and the amended DR5 then deleted the source tables — there is nothing left to migrate, ever. **Precision correction:** the rows being re-keyed were the connection tables — `agent_gitlab_connections.access_token_ciphertext` / `refresh_token_ciphertext` (both `NOT NULL`) and the `agent_mcp_connections` pair; `agent_repo_grants` has no encrypted column at all and is a plain id-preserving copy. The schema's remaining ciphertext column, `agent_oauth_states.code_verifier_ciphertext`, is correctly excluded — ten-minute TTL, single-use. | This row; `ENV_ENCRYPTION_KEY`'s docstring in `source_control.py`; `tests/unit/test_migrate_connections.py::test_copying_the_ciphertext_would_produce_a_broken_connection`, which fails if anyone turns the re-key back into a copy (deleted with the tooling, 2026-08-31 — see the H1 row). |
| ~~**The web app calls three session routes that do not exist yet.**~~ | ~~**E8**~~ ✅ closed — `cloud_agent/api/session.py`, mounted in `app.py`. **Corrected against the service as it now ships, because the original text described a flow that no longer exists:** there are four routes, not three — `GET /login` only parks a PKCE flow, the code exchange happens at `GET /callback`, `POST /logout` ends the session, and `GET /me` reads it. Login no longer takes an `{identity_token}` body and no longer asks `user_status` (that gateway route is retired and this service no longer calls it); the account check is the Gateway's `agent-access` decision. `me` returns `{id, agent_access}`, **not** `{id, email, role, user_name}` — the session carries subject and issuer and no profile, and roles deliberately confer nothing. **One thing E8 did not add, still true: CORS.** The web app sends `credentials: 'include'`, so a deployment serving it from a different origin than the API needs `Access-Control-Allow-Origin` (exact, not `*`) and `Allow-Credentials` in front of this service; the shipped topology is same-origin behind one reverse proxy, so this stays a deployment constraint rather than an open bug. | `tests/servers/test_session_api.py` drives the routes against a real RS256 identity token, and the browser side is pinned by `apps/frontend/src/app/login/page.test.tsx` and `ProtectedRoute.test.tsx` against a mocked BFF, so a mismatched server shape shows up as a red integration rather than a silent one. |
| **`sessionStorage` is still used in `apps/frontend/`, and F2's acceptance criterion cannot be met literally.** The criterion is `rg sessionStorage apps/frontend/` → no hits, standing in for "the access token is gone". The token *is* gone — `client.ts` was rewritten around the cookie and holds no credential. | **Whoever signs off F2.** Either accept the criterion as "no *credential* in web storage", which is satisfied, or schedule the remaining uses as their own post-cutover PR with their tests updated deliberately — migration rules 1 and 3 forbid doing it inside a move. | This row. An enumeration rather than "exactly": `JobDetail.tsx` carries a **draft prompt** across the navigation a rewind/fork performs (and `JobDetail.test.tsx` asserts on it); `apps/frontend/src/lib/api/agents.ts` keeps per-scope **idempotency request ids**; `ProtectedRoute.test.tsx`, `Markdown.untrusted.test.tsx` and `vitest.config.ts` use it for unrelated test/env purposes. No credential among any of them, which is what the criterion actually stands for. |
| ~~**`budget_usd` is now inert.** The create API still accepts it, the store still writes it, and job records still return it — but **nothing in this repository checks it**.~~ | ~~**Whoever owns the API surface**, before this service takes real traffic.~~ ✅ **closed — the field was dropped, and this row outlived it by two days.** The first of the two ways out was taken across three commits: `c9acfcd` (store + schema), `3172bf5` (public API) and `3ba97d5` (the last of it). `git grep budget_usd -- cloud_agent/ migrations/ apps/frontend/src/lib/` now returns nothing, and the rewritten `0001_schema_baseline` never carried the column. **The row is struck rather than deleted because it did real harm while stale**: read on 2026-08-06 it was taken as current, and it describes an unenforced spend cap — the one shape of stale note that gets quoted as a security bound. A closed row costs a line; a wrong one costs whoever believes it. | `tests/unit/test_schema_baseline.py` pins the table inventory, so re-adding a budget column fails the suite rather than quietly restoring the trap. (The cited `test_migration_baseline.py` and its `DELIBERATE_OMISSIONS` set went with the old chain on 2026-09-10.) |
| ~~**The router's API-key branch has no store behind it, on this side.** `verify_api_key` resolves a key through `get_operational_store`, which reads `app.state.operational_store` — and **nothing in this repository sets it**, because the gateway kept `users` and `api_keys`. So a programmatic caller presenting `hyi-*` gets a 503 in every real deployment.~~ | ~~**Whoever owns the API surface.**~~ ✅ **decided 2026-08-04: the branch is deleted**, which was the first of the two ways out this row listed. The reasoning is the row's own: the branch has no producer and never will. `GatewayClient` answers "may this user sign in", never "who owns this key" (the route it asked then, `/internal/users/{id}/status`, is itself retired in favour of `/agent-access`), so nothing in this repository can populate the store the branch reads — a dependency that cannot succeed in any deployment is dead code that looks alive, and leaving it invites a later reader to "fix" it by reaching back into the gateway's database, which is the coupling this split removes. Deleted with it: `verify_api_key`, `hash_api_key`, `get_operational_store`, the `X-API-Key` header parameter, and `_looks_like_browser_jwt` (with one authenticator left there is no namespace left to select). `verify_admin_access` and `verify_dispatcher_access` lost the `op_store` parameter they accepted and ignored. **The session is now the only owner credential**, and restoring a programmatic one means the row's *other* way out — a gateway contract in `gateway_contracts/` that resolves a key to a user — not resurrecting this. The ported case that covered the branch is in the discarded register below. | ~~This row~~ `tests/unit/test_owner_auth_dispatch.py::test_an_api_key_authenticates_nobody` asserts **401**, and a reinstated branch with no store answers **503**, so it fails the day the branch comes back without the contract. `grep -rn "operational_store\|verify_api_key" cloud_agent/` returns two lines, both inside `deps.py`'s module docstring recording the deletion, and nothing else. |
| **`/v1/session/*` has no CORS.** The web app calls it with `credentials: 'include'`; a browser will refuse that cross-origin without `Access-Control-Allow-Origin` naming the exact origin plus `Access-Control-Allow-Credentials`. The middleware was not added because which origins are allowed is a deployment fact, and a wrong default here is either an outage or an open door. | **Whoever changes the deployment topology.** The shipped Compose topology is same-origin — the console reverse-proxies the control plane and the web tier — so nothing needs configuring today. Fronting them on two origins means naming the exact origin here. | `deploy/reverse-proxy/agents-under-gateway.conf` is the shipped same-origin answer, and no `CORSMiddleware` or `allow_origins` appears in `cloud_agent/`. |

**Closed and removed from the register: the host agent enrols itself.** The
"host agent cannot enrol itself" row is gone because it is no longer true —
`cloud_agent_host/enrollment.py` redeems `AGENT_ENROLL_TOKEN` and
`runner.py` calls `ensure_enrolled` at startup, writing the returned credential
under `AGENT_HOST_STATE_ROOT` with mode 0600. `docs/cutover.md` records the
same resolution, and staging's runner host heartbeats as `online` off a stored
credential. Its tripwire (`grep AGENT_ENROLL_TOKEN host/` returning nothing)
was the thing that rotted first, which is the failure mode this register's
tripwires exist to catch.

## Discarded tests

Migration moves test files whole. When an artifact is *rewritten* instead of
moved, its tests cannot come along unchanged — so they are recorded here case
by case rather than quietly dropped, and each row says where the property went.

### `tests/servers/test_agent_jobs_api.py` — the API-key case (2026-08-04)

One ported case, deleted with the branch it covered. The ruling is in the
deferred register above: `authenticate_agent_owner`'s API-key branch had no
producer in this repository and never will, so it was removed rather than left
looking implemented. This is the sanctioned path for a removed artifact — the
same one E4's `visible_models` rewrite used — not a silent deletion.

| Case | What it asserted | Why it cannot hold here | Where the property lives now |
|---|---|---|---|
| `test_normal_api_key_still_authenticates_agent_owner` | That a bearer that is neither a session JWT nor an `ajt.*` worker token — the case minted `hyi-programmatic-client` — reaches the API-key authenticator, returns **200** on `GET /v1/agent/jobs`, and that `update_key_last_used` was called with the resolved key's id. | Both halves depend on an operational store that resolves a key hash to a user. The gateway kept `users` and `api_keys`; nothing in this repository sets `app.state.operational_store`, and `GatewayClient` has no method that answers "who owns this key". The case passed only because it *injected* `FakeOwnerAuthStore` — production reached 503 on the same request. With the branch deleted the request is a 401, and there is no store shape left to fake. | **The gateway**, which still authenticates API keys for its own surfaces — including `update_key_last_used` against the `api_keys` table that never moved. Nothing about key authentication changed there. On this side the replacement is `tests/unit/test_owner_auth_dispatch.py` (6 cases), which pins what this service can now get wrong: an `hyi-*` key authenticates **nobody** and says so as 401 rather than 503 (the tripwire against reinstating the branch), `X-API-Key` is not read at all, the session authenticates by cookie *and* by bearer, and the worker-token 403 `insufficient_scope` that `verify_api_key` used to own survived the function. |

Two things that are **not** discards, recorded so a reviewer diffing the ported
file against the freeze has the whole list:

- `test_sandbox_token_stays_out_of_owner_routes`, `test_browser_jwt_can_list_agent_jobs`
  and `test_expired_browser_jwt_is_not_reinterpreted_as_api_key` each dropped a
  `get_operational_store` override. **No assertion changed** and all three still
  pass: they drove the session path and the sandbox refusal, and installed the
  store only because the dispatch resolved it before choosing a branch.
- `test_dedicated_dispatcher_token_opens_exactly_one_door` lost its `_NoSuchKey`
  store subclass, which existed so the dispatcher token would read as an
  *unknown* API key rather than being validated by the permissive base fake. Its
  assertion — the dispatcher token is 401/403 on the owner surface — is
  unchanged and still passes; the refusal now comes from the session verifier,
  the only owner authenticator left. `FakeOwnerAuthStore` itself is deleted: it
  is a fixture, not coverage, and nothing else used it.

### `tests/unit/test_agent_visible_models.py` (E4)

The source walked the gateway's route registry. That registry does not exist in
this service, so all four cases test behaviour that stayed behind:

| Case | Where the property lives now |
|---|---|
| `test_role_gates_match_the_inference_path` | Gateway. Covered by `test_the_catalog_is_not_the_anonymous_one` in the source repo's `test_internal_lookups.py`, which pins that a `free` user sees strictly fewer models than a `pro` one. |
| `test_aliases_resolve_but_are_not_listed_twice` | Gateway. **Resolved since:** the behaviour change this cell used to record has been undone — `/internal/model-catalog` returns an `aliases` table as part of the declared contract, `gateway.model_catalog_with_aliases()` reads it, and `agent_model_resolvable` resolves a name through it, so an alias works again. What stayed behind in the gateway is only the *scope* of the original case: whether the catalog lists an alias twice. |
| `test_unpublished_disabled_and_embedding_models_are_excluded` | Gateway — `agent_visible_models` there still applies published/role/disabled/chat-type filters before answering. |
| `test_visibility_resolver_override_wins_both_ways` | Gateway — the resolver is a gateway service and the catalog endpoint passes it. |

Replaced by `tests/unit/test_agent_visible_models.py` and
`tests/unit/test_gateway_client.py`, which test what this service can now get
wrong: faithfully reporting what the gateway said, and **failing closed** when
it cannot answer.

### `apps/frontend/src/lib/api/__tests__/client.test.ts` (F2)

`lib/api/client.ts` is the one file F2 rewrites rather than moves — F2's own
acceptance criterion forces it, because the `sessionStorage` access token lives
there and not in `agents.ts`. Its 260 lines of token/refresh machinery are gone,
so its test file cannot come along: every case drives `setAccessToken`,
`getAccessToken`, expiry-peeking, or the refresh-and-replay a cookie session has
no equivalent for.

Replaced by `apps/frontend/src/lib/api/client.test.ts` (11 cases at the last count), which pins what the
rewrite is now responsible for: the request carries the cookie and **no**
`Authorization` header, a 401 announces the expired session **once** and is
**not** retried, an ordinary 5xx says nothing about the session, and
`jsonOrThrow` — carried over verbatim — still classifies errors the same way.

`jsonOrThrow`'s gateway-login branches (password reset, email verification,
duplicate signup) are unreachable here. They were kept rather than pruned: a
migration that edits logic is a migration that cannot be diffed, and the whole
point of moving the function untouched is that error classification is
demonstrably unchanged for every call `agents.ts` makes. Pruning is a
post-cutover PR.

### `mock.test.ts::never spends past the budget cap` (F2)

One case, deleted with the field it asserted on. `expect(job.spentUsd)
.toBeLessThanOrEqual(job.budgetUsd)` cannot survive `budgetUsd` being removed
from `types.ts`, and there is nothing left for it to mean: no per-task budget
exists to be exceeded. The sibling case in `adapt.test.ts` lost its budget
assertion and kept its PR-label one, and was renamed to match.

Actual spend is **not** affected and is still displayed and still tested —
`JobDetail`'s Spend / Input tokens / Output tokens / Model calls panel is
untouched, including the `hasLedger === false` case that says "Ledger
unavailable" rather than implying a job was free.

### `budget_usd`: every moved test the removal touched (F2)

The paragraph above named one case and mentioned a second in passing. That was
not enough: the removal reached **six** moved test files, and a reviewer
diffing them against the freeze had no list to check them off against. Here it
is, whole.

The two rows marked **discarded** were restored verbatim from the freeze and
re-run before being recorded, so the claim "cannot hold" is an observation and
not an assumption. `npx tsc --noEmit` refuses both — `error TS2339: Property
'budgetUsd' does not exist on type 'AgentJob'` — and with the type error
ignored the runtime failures are `expected undefined to be 5` and
`expected value must be number or bigint, received "undefined"`.

| File | Case | What changed | What it asserted, and why it cannot hold |
|---|---|---|---|
| `mock.test.ts` | `never spends past the budget cap` | **discarded** — whole case | `job.spentUsd <= job.budgetUsd` over every mock fixture. `budgetUsd` is gone from `DisplayJob`, so there is no cap to compare against and no per-task budget for one to mean. Nothing survives to re-point it at. |
| `adapt.test.ts` | `carries the budget and the PR label through` → `carries the PR label through` | **discarded** — one assertion of two, plus the rename that follows from it | `expect(job.budgetUsd).toBe(5)` — that `toDisplayJob` copies `budget_usd` from the record onto the display job. `AgentJobRecord.budget_usd` is gone, so `toDisplayJob` has nothing to read and nothing to write. The other half, `expect(job.prLabel).toBe('draft PR 44')`, still applies and is **kept**; the title was narrowed to match what the case now proves. |
| `AgentArchiveViews.test.tsx` | — | fixture key `budgetUsd: 1` removed | **Not a discard.** No assertion changed. Forced by the type: leaving the key produces `error TS2353: Object literal may only specify known properties`. Verified by putting it back. |
| `AgentsSidebar.test.ts` | — | fixture key `budgetUsd: 1` removed | as above |
| `JobDetail.test.tsx` | — | fixture keys `default_budget_usd: 2` and `budgetUsd: 2` removed | as above |
| `TaskComposer.test.tsx` | — | fixture key `default_budget_usd: 2` removed | as above |

Two consequences worth stating rather than leaving to be discovered:

- **`mock.ts` still narrates a budget it no longer has.** Two moved fixture
  strings — `stateNote: 'budget exceeded · $2.00 cap'` and a lifecycle line
  reading `Job stopped: budget cap reached ($2.00)` — describe a cap that no
  longer exists as a field. Editing them would be a second logic edit to a
  moved file, so they stay. The deleted case is what used to keep that mock
  dataset honest about itself.
- **The removal's production half is 5 files** (`lib/api/agents.ts`,
  `types.ts`, `adapt.ts`, `mock.ts`, `JobDetail.tsx`) and cascades from one
  decision: dropping `budget_usd` from `AgentJobRecord`, which the manifest
  asks F2 to do. Everything downstream follows by the compiler — `adapt.ts`
  has nothing to read, so `DisplayJob.budgetUsd` cannot be populated, so the
  field cannot stay required, so the fixtures and the `JobDetail` stat go too.
  Reinstating the field in `types.ts` or `mock.ts` on its own does **not**
  typecheck; only reinstating the record field first makes the rest possible.

### `components/providers/SiteConfigProvider.test.tsx` (F2)

Found by the sweep that fixed the two dropped test files above, and dropped in
the same way — silently. Unlike those two it is **not** a port: its subject is
one of the files F2 recreated, so the test could not have come along unchanged.
It is recorded here rather than ported.

| Case | What it asserted | Why it cannot hold |
|---|---|---|
| `loads and publishes the runtime document from the configured API base` | That the provider `fetch`es `/site-config` from the build-time API base with `cache: 'no-store'`, and publishes `branding.appName`, `branding.contactEmail`, `features.publicSignup` and `features.rag` from the response. | `/site-config` is a gateway endpoint and is in neither contract this service consumes, so the recreated provider makes **no network call at all** — the document is assembled from build-time environment. Two of the four fields it probes, `branding.contactEmail` and `features.publicSignup`, do not exist in the recreated `RuntimeSiteConfig`; the other two exist but are read from `process.env`, never from a response. There is no request to assert on and no async state to await. |

**Nothing replaced it**, and that should be said plainly rather than implied:
`SiteConfigProvider.tsx` and `buildTimeSiteConfig` have **no test in `apps/frontend/`**.
`layout.test.tsx` `vi.mock`s `useSiteConfig`, so it pins the moved layout's
*consumption* of the shape, not the provider that produces it. The shape itself
is held by the compiler — `layout.tsx` destructures `RuntimeSiteConfig`, so
`npm run type-check` fails on a shape change — but which environment variable
lands in which field is currently unasserted. Writing that test is a small
post-cutover PR, not a migration task, which is why this row exists.

### `test_agent_jobs_api.py` — two cases E9 replaced in place (E7/E8/E9)

**Ratified 2026-08-04.** Both replacements stand: each asserts a strictly
stronger property than the case it replaces, and both still prove the thing the
originals existed to prove. This section is the whole record — the freeze text
of both originals is quoted below, so a reviewer does not have to open the diff
to check the claim.

Both originals asserted on **the credential the claim used to mint for the
sandbox**: a `SCOPE_MODEL` worker token, signed with what is now
`AGENT_CONTROL_TOKEN_SECRET`. E9's whole point is that the gateway does not know
that root, so the token bought nothing — the sandbox's first model call was
going to be refused whatever happened at the claim. That credential no longer
exists, so preserving the originals' assertions would have meant preserving a
credential the split deliberately removed. They were **replaced in place**, not
dropped: each replacement drives the same endpoints with the same intent, and a
comment at the old site points here.

#### Case 1 — `test_claim_returns_a_separate_model_scoped_token`

| | |
|---|---|
| **Replaced by** | `test_the_claim_hands_over_a_gateway_grant_not_a_minted_token` |
| **The original asserted** | that the claim response carries a `sandbox_token`; that it differs from `worker_token`; that `parse_worker_token(worker_token)["scope"] == SCOPE_FULL`; and — the case's name and point — that `parse_worker_token(sandbox_token)["scope"] == SCOPE_MODEL`. |
| **The replacement asserts** | the first three **verbatim**, then `pytest.raises(InvalidAgentToken)` on `parse_worker_token(sandbox_token)`, and additionally `store.attempt_grants == {100: "agr_1"}`. |
| **Why it is stronger** | "Parses, with a narrow scope" says the sandbox credential is one of ours and is held off the worker surface by a scope *comparison*. "Does not parse" says it is not one of ours at all: it is refused by the signature check, before any scope logic runs, which is the same statement as *this service cannot mint it*. Three of four original assertions survive untouched; the fourth is replaced by one that implies everything the fourth was protecting and more. The original would still pass if this service went back to minting the sandbox's credential itself — the replacement fails the moment it does. The `attempt_grants` assertion has no counterpart in the original: it pins the attempt↔grant link that renewal and revocation are both looked up through. |

#### Case 2 — `test_sandbox_token_cannot_write_job_state`

| | |
|---|---|
| **Replaced by** | `test_the_sandbox_grant_cannot_write_job_state` |
| **The original asserted** | that the sandbox credential is refused on all four worker write endpoints — `POST …/events`, `…/artifacts`, `…/finish`, `…/heartbeat` — with `403` and `detail.error.type == "insufficient_scope"`; and, as a positive control, that the runner's own `worker_token` still writes an event (`201`). |
| **The replacement asserts** | the identical loop over the identical four endpoints, and the identical positive control, with `401` and `detail.error.type == "unauthorized"`. Nothing else differs — the two bodies are line-for-line the same otherwise. |
| **Why it is stronger** | The refusal moved one step earlier in the pipeline. `403 insufficient_scope` was `_worker_claims` **accepting** the credential's signature and then rejecting its scope; `401 unauthorized` is `parse_worker_token` raising `InvalidAgentToken` before the scope branch is reached at all. The replacement therefore depends on strictly less code being correct — a bug in the scope comparison cannot open it — while proving the same guarantee: a credential leaked out of the sandbox cannot write job state. Coverage is identical (same four endpoints, same positive control); only the identity of the credential being refused changed, which is the change E9 made. |

**One thing case 2's replacement no longer reaches, and nothing else does.**
`_worker_claims`' `403 insufficient_scope` branch — the "anything that is not
`SCOPE_FULL`" refusal on the *worker* surface — was exercised only by case 2's
original. Nothing drives a narrower-scope worker token at a worker endpoint
now: `test_sandbox_token_stays_out_of_owner_routes` mints a `SCOPE_MODEL` token
by hand but drives `/v1/agent/jobs`, whose 403 comes from
`routes.authenticate_agent_owner` (it came from `deps.verify_api_key` until that
function was deleted on 2026-08-04; the refusal moved into the dispatch rather
than going with it), a different function; `tests/unit/test_agent_job_tokens.py`
mints and parses both scopes but touches no route. The branch is live code and
the drift register's "the `model` token scope still exists" row rests on it, so
it wants either a one-case test or deliberate deletion **together with the
scope**, by whoever prunes the token module post-cutover. This is not a
weakness in the replacement — the replacement is about a different credential —
but it is a consequence of the credential change, and it is written down here
so it is found by a reader rather than by an incident.

**Not** discarded, and worth saying so: `test_sandbox_token_stays_out_of_owner_routes`
mints its `SCOPE_MODEL` token by hand rather than getting it from a claim, so it
is untouched and still passing — as is all of `tests/unit/test_agent_job_tokens.py`.

## The web app: what moved, what was recreated (F1–F3)

The plan sizes F2/F3 as a move of 39 files. It is, for the agent trees
themselves — but those files import eight things from outside them, and the
support surface had to come along or be re-authored. Recording the split
because "moved" and "rewritten" carry very different review obligations.

**Moved verbatim** (only route paths and the `budget_usd` removal differ; every
other byte matches `764a6f97`, and `IntegrationsView.tsx` additionally matches
`origin/dev`):

| From | To | Files |
|---|---|---|
| `apps/frontend/src/components/features/agents/` | `apps/frontend/src/components/features/agents/` | 30 |
| `apps/frontend/src/app/agents/` | `apps/frontend/src/app/(agents)/` | 8 |
| `apps/frontend/src/lib/api/agents.ts` + its test | `apps/frontend/src/lib/api/` | 2 |
| `apps/frontend/src/lib/utils/errors.ts` + its test | `apps/frontend/src/lib/utils/errors.{ts,test.ts}` | 2 |
| `apps/frontend/src/components/ui/Markdown.tsx` + its test | same paths | 2 |
| `apps/frontend/src/components/providers/ToastProvider.tsx` | same path | 1 |
| `apps/frontend/src/styles/globals.css` | same path | 1 |
| the frontend's `.eslintrc.json`, `.eslintignore`, `.prettierrc`, `.prettierignore`, `postcss.config.js`, `tailwind.config.ts`, `tsconfig.json`, `vitest.config.ts`, `vitest.setup.ts` | `apps/frontend/` | 9 |

**Recreated**, because moving would have dragged in gateway concerns this
service does not have:

| File | Why not a move |
|---|---|
| `lib/api/client.ts` | The `sessionStorage` access token and its refresh loop are exactly what F2 removes. `jsonOrThrow` and `safeFetch` are carried over verbatim inside it. See the discarded-test record. |
| `config/env.ts` | The source read `NEXT_PUBLIC_API_BASE` and a deploy-target table for the gateway. Replaced by `NEXT_PUBLIC_AGENT_API_URL` plus the authorize URL, and the gateway's token endpoint is derived from the latter. |
| `components/providers/AuthProvider.tsx` | The source drove email/password login and token refresh. Sign-in happens at the gateway now; this asks `/v1/session/me` and nothing else. `hasRole` is carried over unchanged — the layout's internal-only gate depends on its exact ordering. |
| `components/providers/SiteConfigProvider.tsx` | The source fetched `/site-config` from the gateway. Build-time environment instead; the shape (`branding` + `features`) is unchanged because the moved layout destructures it. |
| `components/providers/index.tsx` | Same stack minus `QueryProvider` — no moved file uses TanStack Query, so shipping it would be a dependency with no caller. |
| `components/features/auth/ProtectedRoute.tsx` | The source pushed at its own `/login`. This service has its own login page now (`apps/frontend/src/app/login/page.tsx`), so `ProtectedRoute` navigates there with the destination and the reason; the page's sign-in button is what sends the visitor on to the gateway's `/authorize` with a PKCE challenge. |
| `app/layout.tsx`, `app/callback/page.tsx`, `lib/auth/{pkce,signin}.ts`, `lib/api/session.ts` | New. The document shell, the return leg of sign-in, and the PKCE machinery. |
| `next.config.js` | The source proxied a dozen gateway paths through Next rewrites. Nothing to rewrite here. |

**Not brought across:** `config/branding.ts` (137 lines of gateway branding —
the four values the layout actually reads became environment variables),
`lib/api/{auth,user}.ts`, `config/site-config.ts`, `QueryProvider`, and
`components/ui/{Header,SiteFooter,ErrorBoundary,Button,Card}`.

**Route re-rooting.** `/agents/*` became `/*`, as F3 requires. The moved pages
live in a `(agents)` route group so the moved `layout.tsx` stays a *nested*
layout with the shape its own test renders, while `app/layout.tsx` supplies the
`<html>`/`<body>` document Next requires at the root. Route groups do not appear
in URLs: the built routes are `/`, `/[jobId]`, `/archived`, `/connected`,
`/integrations`, plus `/callback` deliberately **outside** the group so it is
not behind `ProtectedRoute`.

The 22 route-string edits are a path change, not an assertion change — six test
expectations moved with the routes they name (`'/agents/ajob_1'` →
`'/ajob_1'`). The one place a *destination* genuinely changed is the header's
Docs Assistant and Dashboard links: they are gateway pages, so they became
absolute links to `branding.gatewayUrl` and disappear on a deployment that has
not been told where the gateway is. Leaving them as `<Link href="/dashboard">`
would have shipped two 404s.

## Cross-repo readiness

The control plane calls three gateway contract families:
`/internal/agent-grants` (mint, renew, revoke, usage),
`/internal/model-catalog`, and `/internal/users/{id}/agent-access`.
`/internal/users/{id}/status` was the third one and is **retired**: admission
is a permission decision, not an account-profile read, so the account check
moved to `agent-access` and nothing here calls the status route any more.
`/internal/mcp-registry` was removed deliberately: this service owns its
reviewed MCP catalogue and per-user connections, and no smoke/deployment path
requires the old gateway registry.

A review of the wiring flagged this as "endpoints that do not exist", which
was true before the gateway contracts landed. Recording the current boundary
here so the next reader does not re-diagnose it: a missing surface fails closed
— nobody signs in and no job is claimed, rather than either happening
unauthorized.

The paths were verified to match the gateway PRs exactly, not merely by name.

**Those endpoints are now written down** — `gateway_contracts/`, task C8: three OpenAPI
documents covering identity (C1–C3), inference grants (C5–C7) and the internal
lookups (C9), hand-written from the gateway routers rather than generated from
a running gateway, because the value of a contract is that it says what was
*agreed*, so a gateway that stops honouring it produces a visible disagreement
rather than a client that quietly breaks.

~~Each document names the branch and files it was written from in `info.x-source`,
because none of them had merged when they were written. **When those branches
land, re-verify each document against the merge commit** — a review can change a
contract, and these describe the version proposed rather than the version
accepted.~~ ✅ **Landed.** Re-checked 2026-09-10 by reading the route decorators
of the three routers at the gateway's `origin/main`: all three are served there.
That was a route check, not a line-by-line re-review of the schemas, and it is a
snapshot — `main` is a fast-forward mirror of `dev`
(`.github/workflows/sync-main.yml`), so which branch a router is on moves under
the claim. `info.x-source` is now a historical "written from" marker; the current
status, and what to do when the gateway changes a router, is in
[`gateway_contracts/README.md`](gateway_contracts/README.md).

`tests/unit/test_contracts.py` goes past C8's stated acceptance ("YAMLs lint").
It also checks every cited consumer test exists — two plausible-looking test
paths were cited while writing these and neither existed, and an unfollowable
citation reads as verification that did not happen — and that **every
`/internal/` or `/v1/` path literal in `gateway.py` and `grants.py` appears in
some document**, read out of the clients' own source rather than a hand-kept
list. That last one catches the quiet failure: a gateway call added later with
no contract, so the dependency exists in code and nowhere in the record. Every
call the service makes today is covered, and
`/internal/agent-grants/{id}/usage` is among them: `ledger.py` calls
`grants.usage()` to snapshot each attempt's attributed spend, which is the
caller the contract describes. (This paragraph used to say the endpoint was
"called by nothing here" — true when the contract was written, and false since
the ledger landed.)

## Review findings

Successive adversarial reviews of E7-E10 and F/G/H raised these. They are
recorded so they are decisions rather than discoveries, and struck through as
they are resolved — each row says how, and a row resolved by a decision rather
than by a code change says which decision. Severities are the reviewer's.

**All 18 are resolved.** The heading is kept because the register is the point:
a finding that was fixed and then forgotten is a finding that comes back, and
two rows here exist only to correct claims made by earlier rows.

| Sev | Where | Finding |
|---|---|---|
| ~~P1~~ | `storage/store.py` | ~~A drained host silently returned to service: the reaper moved `draining → offline` and a heartbeat moved `offline → online`, so drain undid itself whenever the machine went quiet and came back.~~ ✅ **fixed** — `draining` is no longer a reaper candidate, pinned by `test_a_drained_host_stays_drained_when_it_goes_quiet_and_returns`. |
| ~~P3~~ | `app.py` | ~~Host reaping shared a `try` with lease reaping, so a host-reap failure skipped attempt recovery and stalled publishes for that pass.~~ ✅ **fixed** — separate `try`, since the shared one turned a reporting failure into a strictly worse outage. |
| ~~P2~~ | `storage/store.py` | ~~Registering under an existing host's name revokes the incumbent's credential and resets it to `enrolling`, taking a live busy host out of the pool. The enrol token names no host, so the name is the registering machine's to choose. Needs a decision: refuse a name in use, or make the token name its host.~~ ✅ **fixed — the token names its host.** `POST /admin/agent/runner-hosts/enroll-token` now requires `host`; it is stored on the token row (in the rewritten baseline's `agent_host_enroll_tokens.host`) and is part of `consume_enroll_token`'s UPDATE predicate, so a machine cannot register under a name an admin did not choose for it and a mismatch does not even *spend* the token — a typo is retried, not re-issued. **Refusing a name already in use was the other candidate and was rejected:** it breaks the case the upsert exists for (a rebuilt machine re-enrolling under its own name — `test_a_superseded_credential_stops_working` would have had to be weakened, which rule 3 forbids), and it would have left the naming decision on the unauthenticated side anyway. Registration already required a freshly minted token, so binding the name costs the operator nothing and moves the decision to the party the admin endpoint has authenticated. Pinned by `test_a_typo_cannot_evict_a_live_host` and `test_a_token_admits_only_the_name_it_was_issued_for` (unit), `test_a_typo_cannot_revoke_a_live_hosts_credential` and `test_a_mismatched_name_does_not_spend_the_token` (`dbtest`), with `test_a_rebuilt_machine_re_enrols_under_its_own_name` holding the door open for the legitimate case. Token rows written before the binding have a NULL `host` and match nothing: `NULL = $2` is never true, so they fail closed rather than staying redeemable under any name. |
| ~~P2~~ | `apps/frontend/` (F2) | ~~**Two moved files' test files were not brought across — 14 cases silently dropped**, and not recorded.~~ ✅ **fixed** — `apps/frontend/src/lib/utils/errors.test.ts` (12 cases) and `apps/frontend/src/components/ui/Markdown.test.tsx` (2 cases) were copied from the freeze **byte-identical** and pass with no change at all, not even an import: both use relative imports (`./errors`, `./Markdown`) that survive the move, and `react-markdown`, `remark-gfm`, `@testing-library/*` and `jsdom` were already in `apps/frontend/package.json`. The same sweep over every `.ts`/`.tsx` in `apps/frontend/src` found one further dropped file, `SiteConfigProvider.test.tsx` — its subject was *rewritten*, not moved, so it is a recorded discard rather than a port. See "Discarded tests". |
| ~~P2~~ | `apps/frontend/` (F2) | ~~The `budget_usd` removal is a **logic edit to 11 moved files** that also **deleted a test case and an assertion**.~~ ✅ **recorded, not repaired** — the case and the assertion were put back and re-run to establish whether they could hold. They cannot: `tsc` rejects `budgetUsd` on `AgentJob` (TS2339) and, with types ignored, both assertions fail on `undefined`. They are therefore discarded **with reasons, case by case**, in "Discarded tests" above, together with the four sibling files that lost fixture keys only. The production half of the edit (5 files) stands on the manifest's F2 instruction; the test half is now accounted for rather than silent. |
| ~~P2~~ | `schemas.py` | ~~`HostRegisterRequest.broker_url` documents an env fallback that `broker_for_attempt` does not perform once a host row exists. Doc and code disagree; the doc is wrong.~~ ✅ **fixed — the description, not the code.** `broker_routing.py` refuses the fallback deliberately and says why in its own docstring: routing host B's request to host A's broker is not an error, it is a plausible answer about the wrong machine's disk. `test_a_host_without_a_broker_url_does_not_fall_back` pins it, so "fix the code instead" would have meant deleting a test that exists for a silent-corruption bug. The field now says what actually happens: supply it on every enrolled host that runs jobs, because once an attempt records this host the env var is not consulted, and omitting it makes workspace requests answer 409. The env var is still the answer for attempts that record no host at all — E11's single-host shape, pinned by `test_an_attempt_with_no_host_falls_back_to_the_single_host_env`. |
| ~~P3~~ | `deploy/migrate/` | ~~A `--apply` run that is then refused has already run `alembic upgrade head`, but reports "nothing was written". Schema *was* written.~~ ✅ **fixed — the report tells the truth; the upgrade stays where it is.** Moving it after the refusal checks was the alternative and is not available: those checks read the target's tables, which the upgrade is what creates. So a refused `--apply` now prints `mode: refused — no rows were written` plus a note that the schema *was* written and which revision the target now sits at, and `--skip-migrations` is documented as the way to make a refusal leave the target byte-for-byte unchanged. Pinned by `test_a_refused_apply_admits_the_schema_was_written` and `test_a_refusal_that_upgraded_nothing_does_not_claim_it_did`. **The two related P3s went with it.** The report's `target` column was `refresh + unchanged` — source rows found in the target, not the target's row count — while the checksum printed under it covers every target row; it is now `len(target_rows)`, so the two numbers describe the same set (`test_the_target_column_is_the_targets_row_count`). And the "`API_KEY_SECRET` is ignored" notice was emitted *after* the configuration checks, which made it unreachable for the one operator it is written for: the one who set that variable expecting it to be used and therefore did not pass `--old-key-file`. It is now the first thing the run does, and `test_the_ignored_gateway_secret_is_announced_before_anything_can_refuse` holds it there. |
| ~~P3~~ | `docs/gateway-removal.md` | ~~Two accuracy breaks: a stated command result that is false in the repo the document is about, and a §1.4 path that does not exist at the freeze commit despite the header claiming every path was read there.~~ ✅ **fixed, and every other path, line number and count in the document re-verified against `764a6f97` on 2026-08-04.** `git grep agent_grants origin/dev` does *not* return nothing — it hits the split plan, which specifies the table rather than implementing it — so the claim is now the scoped `git grep agent_grants origin/dev -- apps/ tests/`, which genuinely returns nothing, with the unscoped result called out so nobody reads the plan's own text as evidence the work is done. §1.4's identity paths landed *after* the freeze (`identity.py` with #1177, hours later); they are now marked as `origin/dev` paths — the document's one exception, stated in the header and again in the section — with the router prefix and the full frontend paths named, because line numbers on a moving branch are the part that will rot. Two further line references were off and were corrected: `bootstrap.py:796-890` → `796-892` (the block ends at its own `logger.info`'s closing paren) and `completions.py:598-601` → `597-601`. Everything else checked out: the 20-file `agent_jobs/` deletion list, the eleven tables, 2,550 lines, 29 manifest paths under `tests/`, 8 + 30 frontend files, `agent_name_from_prompt`'s single consumer, and every `§2.5` destination in this repository. **Two of that sweep's own claims were wrong and one was ambiguous, all corrected below** — `agents.test.ts` is 27 cases and not 25, §1.4's callout says four files over a section naming seven, and "314 lines/30 cases" counted test functions where the file runs 60; see the last row of this table. |
| ~~P3~~ | `apps/frontend/src/lib/auth/pkce.ts` | ~~The PKCE verifier uses one fixed `localStorage` key, which breaks concurrent sign-ins. The file's own comment says the key was chosen to dodge a `rg sessionStorage` grep — a guard worked around rather than satisfied.~~ ✅ **fixed, and then superseded: the file no longer exists.** The fix as it stood keyed each pending flow under `cloud-agent:pkce:<state>` so two tabs could not overwrite each other's verifier, and pinned it with `apps/frontend/src/lib/auth/pkce.test.ts` (8 cases). PKCE then moved **server-side** — the control plane assembles the whole round trip and `apps/frontend/src/lib/auth/` holds only `signin.ts` — so the browser has no verifier to store, no `localStorage` key to collide, and no test file. The row is kept because the failure it records (a guard evaded rather than satisfied) is the one a later reader would otherwise reintroduce. F2's `rg sessionStorage apps/frontend/` criterion is still literally unmet by `JobDetail.tsx`'s draft prompt and by the idempotency request ids in `lib/api/agents.ts`; both are recorded in the deferred register. |
| ~~P3~~ | `apps/frontend/package.json` | ~~The "pinned" Prettier is `^3.8.3`, a caret range. Only the lockfile holds 3.8.3, and 3.9.6 reformats a moved file, so a lockfile regeneration turns web CI red.~~ ✅ **fixed** — `"prettier": "3.8.3"`, exact, in `package.json` *and* in the lockfile's own dependency spec (`npm ci` refuses a lock that disagrees with the manifest, so both had to move). Same reason as `ruff==0.15.12` in `pyproject.toml`, stated there in a comment: "moved files pass unchanged" is only a meaningful acceptance criterion against a fixed formatter, and the bump is its own pull request rather than a side effect of a lockfile regeneration. `npm ci --dry-run` and `npm run format:check` both pass on the pinned pair. Unlike the ruff pin, the reason cannot live next to the version — `package.json` admits no comments — so it lives here. |
| ~~P2~~ | `app.py`, `grants.py` | ~~**Renewal cadence and grant TTL are unrelated and nothing asserts they are compatible.** Renewal runs on the reaper's 30 s pass; mint requested no `ttl_seconds`, so the gateway's default applied — a number this service does not hold. A TTL shorter than the cadence loses a live attempt its capability mid-run, and the only symptom is a refused model call inside a sandbox.~~ ✅ **fixed — the relationship is now one number and six assertions.** `grants.RENEWAL_INTERVAL_S` is the cadence and `app.REAP_INTERVAL_S` **is** that constant rather than a second copy; `GRANT_TTL_S = RENEWAL_INTERVAL_S * RENEWAL_HEADROOM_PASSES` (30 × 4 = 120 s) is derived, never typed, and is `mint`'s default so the claim path cannot forget it. The gateway's own clamps are mirrored beside it (`MAX_GRANT_TTL_S` 900, renewal step 300, read from C5 at `71cc77a9`) because both would shorten our TTL *silently*: asking for more than the max is clamped down, and every renewal re-times the grant to the gateway's step whatever we asked at mint — so it is that 300 s, not our 120 s, that the cadence has to stay inside once a job is running. Pinned by six cases in `test_grant_lifecycle.py` (cadence is the reaper's pass; TTL ≥ 3 passes and equals its own derivation; TTL ≤ the gateway max; cadence × headroom ≤ the gateway's step; give-up lands before either expiry; `mint`'s default is the derived value) plus `test_a_lifetime_is_always_asked_for` on the wire. Changing either constant alone now fails a test rather than a sandbox. |
| ~~P2~~ | `api/session.py`, `api/deps.py` | ~~**The two session routes disagreed.** `/login` returned the gateway's `user_name`; `/me` returned `null` for it always, because `get_current_user`'s dict has no such key. One contract, two answers, for seven days per cookie.~~ ✅ **fixed, and then made moot by the minimal-identity change.** The disagreement was resolved the hard way — make the cookie the single answer — by having `mint_session` accept a display name and login return the same value it minted: the plumbing has to be right *before* the field exists, or it arrives and one route reports it. Then the profile was dropped from the identity token altogether, and the question disappeared with it: `mint_session(*, user_id)` now mints `sub`/`gateway_issuer`/`iat`/`exp` and nothing else, `/me` returns `{id, agent_access}`, and there is no display name in the session to disagree about. The tests that exist assert the **opposite** of the old plumbing — `test_a_display_name_does_not_survive_into_me` and `test_identity_profile_claims_are_not_copied_into_the_session` in `tests/servers/test_session_api.py` — so the two rows this cell used to cite (`test_login_and_me_answer_the_same_thing`, `test_a_display_name_survives_into_me`) are gone deliberately, not lost. |
| ~~P2~~ | `api/routes.py` | ~~**A grant that could not be linked was handed over anyway.** `_mint_sandbox_grant` returned the token when the gateway sent no `grant_id`, and when the fenced UPDATE matched nothing. An unlinked grant is one nothing renews and nothing revokes: live to its TTL, owned by no attempt, findable from neither side.~~ ✅ **fixed — link or withdraw.** The link is now the condition: a grant that is not recorded against its attempt is revoked immediately (when there is an id to revoke), the claim is released so no attempt is spent, and the claim answers 503 `credential_unavailable` — the same shape the unmintable case already answered, because a runner has no different useful behaviour for the two. The id-less case cannot be revoked at all, which is exactly why it must not be used. Pinned by `test_a_grant_that_cannot_be_linked_is_revoked_not_handed_over` and `test_a_grant_with_no_id_is_refused_rather_than_used`. |
| ~~P2~~ | `app.py` | ~~**Renewal retried a dead grant forever.** `_renew_live_grants` had no give-up, so a grant the gateway has permanently refused was asked about every pass for the life of the attempt — and the attempt ran on toward the moment its capability expired with nothing saying so. Revocation detaches as it hands over; renewal had no equivalent.~~ ✅ **fixed — it gives up, and giving up ends the attempt.** After `GRANT_RENEWAL_GIVE_UP_AFTER` (3) *consecutive* refusals of the same grant — consecutive, so a gateway with a bad minute every hour cannot accumulate a job to death — the attempt is abandoned via `release_claim`: aborted, the worker's next heartbeat says stop, and the job returns to the queue **without spending a retry**, where the next claim asks for a fresh grant rather than nursing a dead one. Nothing revokes inline; the release makes the attempt non-current, which is precisely what `take_revocable_grants` selects, so the revocation pass takes it exactly once. Three passes is 90 s, inside both the gateway's 300 s renewal step and our own 120 s TTL, so the give-up lands while the capability is still alive. The counter is owned by the reaper loop (not the module), so a restart forgets — the right way round: a fresh process should ask again before abandoning anyone's attempt. `list_renewable_grants` now carries `lease_generation` so the release is fenced; a `dbtest` pins that the fence it hands out is the one `release_claim` accepts. Pinned by five unit cases and `test_a_renewable_row_carries_the_fence_that_can_end_it`. **This row's "consecutive refusals" was too loose and the next row corrects it:** every failure counted, including ones the gateway never made. |
| ~~P3~~ | `api/deps.py` | ~~**One misconfiguration, two diagnoses.** An unset `AGENT_SESSION_SECRET` made `get_current_user` answer 503 and `verify_admin_access` answer 401, because the latter caught the base `IdentityError`. The 401 sends an operator hunting for a credential problem that does not exist.~~ ✅ **fixed — both answer 503.** `verify_admin_access` catches `IdentityNotConfigured` before `IdentityError` and reports it as the deployment problem it is. The `ADMIN_TOKEN` door is compared first and is unaffected, so a deployment that authenticates machines only keeps working with no session secret at all. Pinned by `test_the_admin_door_diagnoses_a_missing_secret_the_same_way` and `test_the_admin_token_still_opens_without_a_session_secret`. |
| ~~P3~~ | docs (four count errors) | ~~Four counted claims a reader would trust, each wrong: `agents.test.ts` as "526 lines, 25 cases"; §1.4's callout saying `git cat-file` "fails for all four files below" over a section naming seven paths; `docs/cutover.md`'s `hosts.py:105,151`; and this table's PKCE row claiming "abandoned entries are swept when a new flow starts".~~ ✅ **fixed, each counted at the freeze rather than re-copied.** (1) `agents.test.ts` is **526 lines, 27 cases** — 526 confirmed, but the file holds 26 `it` blocks of which one is an `it.each` of two, and `vitest` runs 27; corrected in `docs/gateway-removal.md` §2.4 and in the manifest-corrections table below. The neighbouring `test_agent_name_from_prompt.py` count was ambiguous the same way and is now "30 test functions, 60 executed cases" rather than "30 cases". (2) §1.4 names **seven** paths, not four; all seven fail `git cat-file -e 764a6f97:<path>` and all seven exist on `origin/dev` (re-checked 2026-08-04), and the section now lists them so the number is countable. (3) `hosts.py:105,151` had rotted to 113 and 168; replaced with `::register_host` and `::heartbeat_host`, which cannot rot — and the stale "`grants.py` has no caller" in the row below it went with it, since E9 gave it one. (4) **The claim was made to match the code, not the other way round.** `purgeExpired()` sweeps by *redeemability* — unreadable, or past `MAX_AGE_MS` — never by abandonment, and it must: an entry younger than `MAX_AGE_MS` is indistinguishable from a sign-in running in another tab, which is the case the per-`state` keying exists for. The row above, `pkce.ts`'s header, `purgeExpired`'s docstring and the test's own name now all say that. |
| ~~P2~~ | `app.py`, `gateway.py`, `grants.py` | ~~**The give-up could not tell a refused grant from an unreachable gateway**, so ~90 s of gateway downtime abandoned every running attempt in the fleet and discarded work that was nearly done.~~ ✅ **fixed — a give-up now requires the gateway to have named the grant.** `_renew_live_grants` caught bare `Exception` and `GatewayError` carried no status, so `ConnectTimeout` and "that grant is revoked" were the same object; three passes of either destroyed the attempt. `GatewayError` now carries `status_code` and `error_type`, `definitive` distinguishes *an answer* from *no answer* (4xx excluding 408/429), and only `DEAD_GRANT_REFUSALS` — `unknown_grant`, `grant_not_live`, `subject_unavailable` — advances the counter. **The status code alone was not enough and that is the sharp part:** the gateway answers 404 for both `unknown_grant` and `internal_api_not_configured`, and 401 for a rotated dispatch token, so a reader of status classes destroys the fleet on one missing environment variable exactly as readily as on real downtime. A non-verdict now leaves the count *untouched* — neither advancing (the abort) nor resetting (which would let a grant refused every other pass be renewed forever) — so only a successful renewal clears it. An unrecognised type is not definitive, and that direction is deliberate: not giving up costs a few doomed renewals against a capability the TTL retires anyway, while giving up wrongly costs a user's work. Pinned by ten cases — a fleet-wide outage releasing nothing, six parametrised non-verdicts (401, the 404 that is *not* a dead grant, 500, 502, 429, 408), an outage that neither advances nor forgives, `subject_unavailable` still giving up, and the `DEAD_GRANT_REFUSALS` set pinned literally because adding to it is a decision to destroy work — plus four on the client, that a refusal carries its type and an unreachable gateway carries none. **Verified by mutation:** restoring `definitive = True` fails eight of them. |
| ~~P3~~ | `api/deps.py` | ~~`log_admin_action`'s docstring credits migration `0004` for the audit table; `0004_enroll_token_host` is the enrol-token binding and the table arrives in `0005_grants_and_audit`.~~ ✅ **fixed once, and the fix then rotted too — which is the point of the row.** The first repair replaced a bare ordinal with the full revision name (`0005_grants_and_audit`) on the reasoning that a name is harder to misread than a number. The 2026-09-10 baseline rewrite deleted that revision: `agent_admin_audit_log` is now created by `0001_schema_baseline`. The docstring and this row both name the baseline, and the durable lesson is the one the original repair missed — *any* reference to a specific revision can rot, so a docstring that has to cite one should say what the table is for and let the migration be found. |

## Open decisions

Things a migration task surfaced that are **design calls, not mechanical
moves**, and so were deliberately not decided by the task that found them.

### Two host registries, or one (found in G1)

The plan's G1 says to create `agent_hosts(host_id ulid pk, name unique, …)`
beside the `agent_runner_hosts` table upstream #1158 already shipped. It never
says what becomes of the old one, and G4's slot scheduler supersedes a claim
gate — the pin — whose behaviour E10 pins with ten ported tests.

Taken literally that produces two answers to "which hosts exist": two admin
surfaces, two lists an operator has to know to reconcile, and a claim gate
reading one while somebody edits the other. It also breaks the ten tests, which
under rule 3 is a parity break rather than a design change.

**Decided (2026-08-04): one registry.** `agent_runner_hosts` gains the pool
columns — status, labels, capabilities, slots, version, broker URL, heartbeat,
credential hash — and keeps its primary key. `agent_attempts.host_id` therefore
references `agent_runner_hosts(host)`.

What that buys, and what it costs:

- #1158's pin, its three endpoints and its ten tests keep meaning what they
  meant. The pin and the pool **compose**: the pin decides *which* host may
  claim, the pool decides *whether* that host is in a state to. Both gates run,
  both fail closed, and neither can silently undo the other.
- A host that joined by polling defaults to `online` with a null `slots_total`
  (unmetered) and empty capabilities, which is exactly the pre-G behaviour. No
  existing deployment has to enrol anything to keep working.
- The cost is one table holding two kinds of row — a *sighting*, whose name is
  a scheduling label anything with the dispatcher token can invent, and an
  *enrollment*, whose name is a credentialed identity. They are told apart by
  the `enrolled` flag, and `admin_hosts.py`'s docstring says which guarantee
  applies to which. A reviewer who dislikes that is disliking the right thing;
  the alternative was two tables that mean the same thing.
- The name `host_id` on `agent_attempts` is the plan's word for a column that
  holds a host *name*. Kept because the plan names it, and because renaming a
  foreign key later is cheaper than reconciling two registries.

If a ulid identity is genuinely wanted later, it is an added column plus a
backfill, not a second table.

### The co-located runner overlay (found in D7)

`deploy/docker/docker-compose.agent-runner.yml` and `ops/deploy/agent_runner.sh`
did **not** move. They are not standalone: the compose declares a `backend:`
service and is composed *on top of* the gateway's own `docker-compose.yml`
(`-f docker-compose.yml -f docker-compose.agent-runner.yml`). The remote-runner
file says why in its own header — pointing the overlay at a machine with no
gateway does not fail, it *starts a gateway there*.

This repository has no `docker-compose.yml` base and no `backend` service, so
moving the pair as-is would land files that cannot run, and adapting them into
a standalone stack is a redesign that a migration task must not perform.

The two options, for whoever decides:

1. **Drop them.** A co-located runner becomes "the standalone remote-runner
   compose, with the tunnel pointing at localhost" — a configuration, not a
   second file. Cheapest, and it leaves one deployment shape to maintain.
2. **Keep a co-located topology as a first-class file here**, written against
   this repository's own services rather than the gateway's.

Their tests stay with them: `tests/unit/test_agent_deploy_config.py` asserts on
that compose, and `tests/unit/ops/test_agent_runner_preflight.py` executes
`agent_runner.sh`. Both are still in the source repo. The behaviours they pin
(internal egress network, read-only docker socket, non-root sandbox, kata
preflight) are **not currently covered here** — closing that gap is part of
whichever option is chosen. `tests/unit/test_deploy_paths.py` covers the moved
files' paths only, and says so.

**Decided (2026-08-04): option 1.** The overlay is dropped; the standalone
remote-runner compose is the only topology, and a co-located runner is that
compose with the tunnel pointing at localhost. H4 deletes all four source files.

Its guarantees are kept by porting the source test file **whole** rather than
by picking out the properties that seemed important:

- `tests/unit/test_deploy_config.py` — all 50 source assertions, re-pointed at
  the standalone compose. 48 pass unchanged; 2 were adapted where they keyed
  on the gateway's `backend` service, keeping the half that still exists.
- `tests/unit/test_deploy_security.py` — only what the port could not carry.
- `tests/unit/test_setup_kata_runtime.py` — the Kata preflight, driving the
  real script. D7 had missed this file entirely.

**Rule this produced: port test files whole, then adapt what fails.** A first
attempt hand-picked "the four properties that matter" and produced 13
assertions against the source's 50 — a 74% silent loss, in a file whose whole
job is to catch silent failures. Reading the source list is not the same as
porting it, and the compiler does not check prose. When a task drops an
artifact, its tests move first and get deleted individually with reasons, never
summarised.

**Functional gap found with the decision; resolved 2026-09-02.** The Trusted egress tier —
the Squid allowlist proxy and the `agent-egress-trusted` / `agent-egress-uplink`
networks — was declared **only** in the overlay. At the time of the move, the
standalone compose left `AGENT_EGRESS_NETWORK_TRUSTED/CUSTOM/FULL` empty and
selected `platform_only` for both phases, so it did not support dependency
registry access. The retained-only deployment follow-up closes that gap:
the offline validating renderer writes a Docker-owned config volume, the
proxy alone straddles an internal Trusted network and a routable uplink, and
setup defaults to that tier while agent turns stay `platform_only`. The three
previously skipped source assertions now run normally. This is infrastructure
state only; no chat workspace, provider state, or Agent CLI session is mounted.

**Isolation gap found during the retained-deployment audit; resolved
2026-09-02.** A Docker network marked `internal` has no off-host route, but its
containers can still address one another. Putting every chat directly on the
same `agent-egress` bridge therefore made the documentation's two-fixed-relay
claim false and exposed unrelated sandboxes as network peers. The compose
networks are now discovery backplanes only. Lifecycle code creates a labelled,
deterministic internal bridge per sandbox group, attaches only that group's
container and the phase's fixed relay/proxy endpoints, and migrates a stopped
pre-upgrade container off the old shared bridge before restart. Fork siblings
remain in one container/network by design; unrelated groups do not.

### Which machine runs the connection migration (found in H1)

`deploy/migrate/migrate_connections.py` must hold **both** keys at once for the
length of one run: the gateway's, to decrypt, and this service's, to
re-encrypt. That is not an implementation choice — nothing else can read the
old rows — and it is the one place this repository brushes against AGENTS.md
rule 4.

What the code decides, and does not leave to cutover day:

- the old key is read from a **file** (or stdin), never from the environment
  and never from `argv`;
- `API_KEY_SECRET` in the script's own environment is **ignored**, with a note
  on stderr saying so, so it cannot become configuration by accident;
- nothing is persisted: no cache, no state table, no log line carrying key
  material or plaintext, and the source database is opened
  `default_transaction_read_only`.

What is **not decided**, and is an operator call rather than a coding one:

1. **On the gateway host**, with `AGENT_DATABASE_URL` pointing at the new
   database over the network. The old secret never leaves the machine that
   already holds it; the new key travels there for one run.
2. **On an operator workstation** with a route to both databases, both keys as
   files on a `tmpfs`, deleted afterwards.

Either satisfies rule 4's actual property — the gateway's secret must not end
up in the agent host's persistent configuration. Deciding it under a runbook
clock is how it ends up in an `.env`, so it is written down here now. The
runbook it was recorded in, `deploy/migrate/README.md` § Where it runs, was
deleted with the tooling on 2026-08-31; whoever schedules H2 puts the answer in
the cutover ticket.

(2026-08-31: the tooling this section governs was deleted without ever having
run — see the H1 row in the task table for the record and the recovery pointer.
The decision above stands as written for any future re-key.)

## Manifest corrections

Classification errors found while executing tasks. Fold these into the source
repo's manifest when it is next edited.

| File | Manifest says | Actually | Why |
|---|---|---|---|
| `apps/backend/serving/agent_jobs/terminal_coordination.py` | `move:host`, D5 | `move:control`, E6 | Its docstring says "Gateway-owned recovery"; its consumers at the freeze are `servers/bootstrap.py` and `servers/routers/agent_jobs.py` — control-plane code. It imports the broker *client* (`workspace_broker_client`, the caller side), not the broker. The route cases move with `test_agent_jobs_api.py` in E10; E6 ports the settled-terminal reconciliation case from `test_bootstrap.py`. |
| `apps/backend/serving/agent_jobs/patch_gate.py` | `move:host`, D4 | **shared** — host *and* control plane | At the freeze it is imported by `runner.py` and `workspace_snapshot.py` (host) **and** by `publisher.py`, `publish_worker.py` and the API router (control plane). E5 imports it from `cloud_agent_host`, which works in one repository but means a control-plane image must ship the host package. A shared third package is the eventual right answer; that is a deployment-layout decision, not something a migration task should make. Whoever writes `deploy/control-plane/` needs to know either way. |
| `apps/backend/serving/servers/routers/agent_jobs.py` | "2,257 lines" | **2,550 lines** at the freeze commit | The count predates the five PRs that landed 2026-08-03. Anyone sizing E6 from the manifest would have under-estimated it by 13%. |
| `apps/backend/serving/schemas_admin.py` | absent from the manifest | `move:control`, E6 | `admin_hosts.py` cannot import without `AgentRunnerHost`, `ListAgentRunnerHostsResponse` and `SetActiveAgentRunnerHostRequest`. The three classes were copied verbatim into `cloud_agent/schemas.py`. Same shape of omission as D7's sandbox assets: the manifest listed a module but not what it imports. |
| `deploy/docker/pi-freeinference`, `opencode-freeinference`, `kilo-freeinference`, `codex-requirements.toml` | absent from the manifest | `move:deploy`, D7 (moved to `deploy/sandbox/`) | `Dockerfile.agent-sandbox` `COPY`s all four. The manifest listed the Dockerfile but not its assets, so a literal reading of D7 would have produced an image that fails to build. |
| `deploy/docker/docker-compose.agent-runner.yml`, `ops/deploy/agent_runner.sh` (+ their two test files) | `move:deploy`, D7 | **dropped by decision** — see “The co-located runner overlay” | They were an overlay on the gateway's compose stack, not a standalone deployment; the standalone host stack and its ported tests replace that topology. |
| `ops/setup/setup_kata_runtime.sh` | absent from the manifest | `move:deploy`, D7 (here as `deploy/host/setup_kata_runtime.sh`) | Found while writing H4's deletion list. It carries no "agent" in its name, so the A3 grep never matched it and **no regeneration of the manifest ever will** — same for its test, `tests/unit/ops/test_setup_kata_runtime.py`, which MIGRATION.md already caught by hand. Both must be deleted from the source repo at H4 off this row rather than off the manifest. |
| `ops/release/public_export.py:36` **and** `tests/unit/release/test_public_export_audit.py:161` | the manifest's coordination note names only the first | both hard-reference `apps/backend/serving/agent_jobs/patch_gate.py` | The audit test loads the gate by path to assert the export tool reuses its credential shapes. H4 breaks both, not one. Recorded in `docs/gateway-removal.md` § 3. |
| `apps/frontend/src/lib/api/__tests__/agents.test.ts` | absent from the manifest and from the plan's Appendix inventory | `move:web`, F2 | Same shape of omission as `schemas_admin.py` and D7's sandbox assets: the manifest lists `agents.ts` but not its test. 526 lines, 27 cases (26 `it` blocks, one of them an `it.each` of two), and the **only** coverage of the SSE stream reader — frame parsing across chunk boundaries, cursor tracking, reconnect-without-a-gap. Ported whole, **byte-identical to the freeze** — re-verified by `diff`. A literal reading of F2 would have shipped the streaming client uncovered. |
| `apps/frontend/src/lib/utils/errors.test.ts`, `apps/frontend/src/components/ui/Markdown.test.tsx`, `apps/frontend/src/components/providers/SiteConfigProvider.test.tsx` | absent from the manifest — as are the three files they test, which F2 had to pull in as support surface | `move:web`, F2 (the first two); recorded discard (the third) | The same omission again, and it did land: the first two were dropped, 14 cases with them, and were not noticed until an adversarial review. **The manifest names no web support file and therefore no web support test**, so a regeneration will not catch this class — the only reliable check is the one this repair used: for every `.ts`/`.tsx` in `apps/frontend/src`, ask whether a sibling test existed at the freeze. That sweep has now been run over all 39 non-test `.ts`/`.tsx` files in `apps/frontend/src` and is complete: these three are the entire remainder, alongside `client.test.ts`, which F2 had already recorded. Every other freeze test file whose subject is present in `apps/frontend/` came across. |

## Upstream provenance

The cloud agent was built in the source repository between **2026-07-28** and
**2026-08-03** as issue #1041 and its follow-ups: 51 commits across 52 pull
requests.

<details>
<summary>Pull requests that produced the migrated code</summary>

#1041 (issue), #1054, #1057, #1061, #1064, #1073, #1089, #1090, #1096, #1098,
#1111, #1112, #1113, #1114, #1115, #1116, #1117, #1118, #1121, #1123, #1124,
#1125, #1126, #1129, #1131, #1133, #1134, #1136, #1138, #1139, #1141, #1142,
#1143, #1145, #1146, #1149, #1151, #1152, #1153, #1154, #1155, #1156, #1157,
#1158, #1159, #1160, #1161, #1162, #1163, #1165, #1166, #1169

Plus the pre-freeze set listed above: #1147, #1148, #1167, #1168, #1170.

</details>

### `tests/servers/test_agent_jobs_api.py::test_sandbox_token_is_refused_on_owner_routes` (E10)

The only case of the 103 that did not port. It imports
`serving.servers.auth._is_inference_path` and asserts on it directly — no
request, no router, no client. The function classifies the gateway's *inference*
routes (`/v1/chat/completions`, `/v1/messages`, `/v1/embeddings`), and this
service serves none of them, so there is nothing here to point it at.
Re-implementing the predicate to give the test something to assert on would be
authoring production code inside a migration task.

| Property | Where it lives now |
|---|---|
| A model-scoped sandbox token cannot act as an owner on `/v1/agent/jobs` | **Ported and passing** — `test_sandbox_token_stays_out_of_owner_routes` drives it over the wire and asserts 403 `insufficient_scope`. |
| The same token cannot write job state on the worker surface | **Ported, then replaced in place by E9** — `test_the_sandbox_grant_cannot_write_job_state` drives the same four worker endpoints against the credential the sandbox actually carries now, and asserts 401. The `SCOPE_MODEL` token's own refusal on that surface is currently uncovered; both facts are recorded under "two cases E9 replaced in place" above (ratified 2026-08-04). |
| Which URL prefixes count as billed inference | Gateway. It is a property of the gateway's route table, and the source file that owns it did not move. |

## E10's auth stand-ins

*This section describes the state at E10 and is superseded twice below — first
by E7 landing, then by the 2026-08-04 API-key ruling, which deleted
`verify_api_key` outright. Read it as history.*

The router moved unmodified, so it still calls `get_current_user`,
`verify_api_key`, `verify_admin_access`, `has_role`, `looks_like_agent_token`,
`get_client_ip` and `log_admin_action` — all E7 placeholders that raise. The
ported tests supply them from `tests/support/e7_auth.py`, whose docstring is the
long version of this note.

Most of the suite is unaffected: 96 of the 102 ported cases stub
`authenticate_agent_owner` exactly as the source did, so the stand-ins only have
to make `require_agent_owner`'s role gate work. Six cases pop that override on
purpose and exercise the real dispatch, and for those it is worth being precise
about what a green run now means:

| Case | Still real coverage of this repo | Now pinning E7's contract instead |
|---|---|---|
| `test_browser_jwt_can_list_agent_jobs` | ~~`_looks_like_browser_jwt` routes a compact JWT to the JWT verifier~~ (that function was deleted with the API-key branch on 2026-08-04 — with one authenticator left there is no namespace to select), and the route answers 200 | — (the token is minted by this repo's own `identity.mint_session`) |
| `test_expired_browser_jwt_is_not_reinterpreted_as_api_key` | 401, **not** a fallback to the API-key path — the regression the test exists for; since 2026-08-04 there is no API-key path to fall back to, and the case now pins that the expiry message survives | the `"Token has expired…"` detail string, which the stand-in reproduces |
| ~~`test_normal_api_key_still_authenticates_agent_owner`~~ | ~~a non-JWT bearer reaches the API-key path~~ | ~~`update_key_last_used` — the `api_keys` table stayed on the gateway~~ **Discarded 2026-08-04** with the branch it covered; see the discarded register. |
| `test_sandbox_token_stays_out_of_owner_routes` | `looks_like_agent_token` keeps a worker token out of the JWT branch; 403 on the wire | the refusal is now flat ("no inference route here") rather than path-based |
| `test_claim_requires_the_dispatcher_credential` | the claim gate is closed to an ordinary caller | — |
| `test_dedicated_dispatcher_token_opens_exactly_one_door` | `verify_dispatcher_access`'s `AGENT_DISPATCHER_TOKEN` fast path, which is this repo's code | the fallback's refusal, via the stand-in |

**Tripwire.** When E7 lands, delete `tests/support/e7_auth.py` and let these
tests run against the real dependencies. The three right-hand entries above are
the acceptance list for that task: if E7 ships and the file is still here, the
port is pinning a stub.

### ✅ E7 landed; the stand-ins are gone

`tests/support/e7_auth.py` is **deleted**, and all six cases above now run
against `cloud_agent.api.deps`. What became of the three right-hand entries:

| Entry | Answer |
|---|---|
| the `"Token has expired…"` detail string | **Real.** `get_current_user` produces it, from `identity.verify_session`'s own `ExpiredSignatureError`. `tests/unit/test_api_auth_deps.py::test_an_expired_session_says_so` pins it independently of the ported case. |
| `update_key_last_used` — the `api_keys` table stayed on the gateway | **Never answerable here, and answered on 2026-08-04 by deletion.** `get_operational_store` read `app.state.operational_store`, which *nothing in this repository sets* — so the API-key branch answered 503 in every real deployment, and the ported case passed only because it injected a fake. E7 recorded that as the one finding that was a decision rather than an implementation; the decision is now made and the branch, `verify_api_key`, `hash_api_key` and `get_operational_store` are gone. See the deferred register row and the discarded-test row. |
| the flat refusal for a sandbox credential | **Real, and it outlived its function.** It was `verify_api_key` raising 403 `insufficient_scope` for anything in the `ajt.` namespace before it looked at a store — which is why it never depended on the branch above being reachable, and why deleting that branch could keep it. It now lives in `authenticate_agent_owner` itself, pinned both on the wire (`test_sandbox_token_stays_out_of_owner_routes`) and at the dependency (`test_owner_auth_dispatch.py::test_a_worker_token_is_refused_as_insufficient_scope`). |

One dependency was **not** deleted despite naming a gateway-side table:
deleting `get_log_store` means deleting `_job_usage`, which would take ported
test cases with it. It answers `None` instead, which is what the code path
behind it was already written to handle — *absent* spend rather than a
fabricated zero.

Its sibling `get_operational_store` went the other way on 2026-08-04. The same
argument did not hold for it: what it fed was not a degraded feature but a
whole authenticator that could not succeed in any deployment, and the ported
case blocking its removal was passing only on an injected fake. Deleted
deliberately, with that case recorded in the discarded register — which is the
difference between this and a silent drop.

`log_admin_action` and `get_client_ip` went the other way and became real:
migration `0005` adds `agent_admin_audit_log`, the store gained
`record_admin_action`, and `admin_hosts.py` passes the agent store where it
used to pass an operational store it never had. A documented no-op was the
alternative and was rejected — this service's admin surface revokes a machine's
credential, retargets every future claim and removes hosts from the pool, and
those are exactly the actions an incident asks about afterwards.

## What stays in HybridInference

Not everything agent-shaped moves. These stay in the gateway because they are
about *inference*, not about *agents*:

- `api_logs.agent_job_id` and the cost/usage queries over it — this is the
  attribution ledger for reporting what a job spent. Monetary enforcement is
  the gateway's existing per-user quota; there is no per-job budget.
- The grant verification path (`model_auth.py` → a gateway-owned grants module):
  the gateway mints and verifies inference grants, so the signing secret never
  leaves it.
- User accounts, login, roles, and ban state, which this service consumes as an
  identity contract rather than a database.

## Landed here

| Phase | Task | Status | Commit |
|---|---|---|---|
| B | B1 — repository scaffold | ✅ landed | `96a2162` |
| B | B2 — CI and secret scanning | ✅ landed | this PR |
| B | B3 — migration record | ✅ landed | this PR |
| B | B4 — environment inventory | ✅ landed | this PR |
| A | A1 — resolve open agent PRs | ✅ done | 5 PRs merged upstream |
| A | A2 — declare freeze | ✅ done | `764a6f97` upstream |
| A | A3 — file manifest | ✅ done | upstream |
| C | C1 — identity JWKS endpoint (+ key rotation) | ✅ merged | upstream #1174 |
| C | C2/C3 — authorization code + token exchange | ✅ merged | upstream #1177 |
| C | C4 — /authorize consent page | ✅ merged | upstream #1179 |
| C | C5–C8 — inference grants, usage, contracts | ✅ merged | upstream #1180–#1183, #1191, #1195 |
| D | D1 — host package skeleton | ✅ done | landed with the B1 scaffold |
| D | D2 — sandbox, runtimes, egress (+proxy) | ✅ merged | this repo #2 |
| D | D3+D4 — workspace setup/paths/snapshot + patch gate | ✅ merged; D3 acceptance pending D6 | this repo #3; one PR because `workspace_snapshot` imports `patch_gate`; snapshot tests are owned by D6 above |
| D | D5 — workspace broker + browser | ✅ merged | this repo #4; `terminal_coordination` reclassified to Phase E (see manifest corrections) |
| D | D6 — runner + broker client (+ logging entrypoint) | ✅ merged | this repo #5, stacked on #4; closes both D6 register rows; D3's pending acceptance resolves here |
| E | E10 — control-plane parity tests | ✅ merged | 102 of 103 cases ported and passing at E10 (`test_agent_jobs_api.py` 102, `test_admin_agent_runner_hosts.py` 10 — counts at that PR; 170 and 16 test functions respectively today) plus the `dbtest` lifecycle; 1 discarded and 1 `xfail(strict)` — both recorded above and both pre-existing upstream, not caused by the move. **101 in `test_agent_jobs_api.py` since 2026-08-04**: the API-key ruling discarded one more, recorded in the discarded register and replaced by `tests/unit/test_owner_auth_dispatch.py` |
| E | E6 — API router and app shell | ✅ merged | this repo #11, stacked on #10; the control plane boots and serves 45 operations (measured at that PR; 85 route decorators across the eight routers mounted in `app.py` today) |
| E | E7+E8+E9 — auth dependencies, session endpoints, grants in the request path | ✅ merged | one PR because they are one path: the cookie E8 mints is what E7 verifies, and the claim E9 rewrites is the only route that was still minting a credential of its own. `deps.py`'s placeholders are implemented, `tests/support/e7_auth.py` is **deleted** (its tripwire), `/v1/session/*` exists, the sandbox carries a gateway grant, and `0005_grants_and_audit` adds `agent_attempts.grant_id` plus `agent_admin_audit_log`. Two ported cases could not survive E9 — see the discarded register — and one gateway-side contract is still owed; see the deferred register |
| E | E5 — source control + publishing | ✅ merged | this repo #10, stacked on #9; cipher re-keyed to `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` |
| E | E4 — visible models over HTTP | ✅ merged | this repo #9, stacked on #8; **rewritten, not moved** — see the discarded-test record below |
| E | E2+E3 — schemas, entitlement, control tokens (re-keyed) | ✅ merged | this repo #8, stacked on #7; `AGENT_CONTROL_TOKEN_SECRET` replaces the gateway's root |
| E | E1 — store + Alembic baseline | ✅ merged | this repo #7, stacked on #6; `initialize()` becomes a no-op, schema **and its five startup backfills** move to `0001_baseline` |
| D | D7 — host deploy files | ✅ merged | this repo #6; co-located overlay dropped by the recorded option-1 decision, standalone host stack retained |
| F | F1+F2+F3 — Next.js scaffold, API client + components, pages as the root app | ✅ merged | this repo; one PR because the moved pages do not type-check without the components and the components do not run without the scaffold. See "The web app: what moved, what was recreated" above. |
| H | H1 — connection migration tooling | ✅ merged; **never run, deliberately** | `deploy/migrate/` — the two tables, re-keyed rather than copied, id-preserving, idempotent, row counts + a plaintext-derived checksum. 24 unit cases and 8 `dbtest` cases against two real databases. By cutover day the payload was **one** GitHub connection (re-made by hand 2026-08-06, DR5's reconnect fallback) and zero GitLab rows, so step 5 was skipped; DR5 was then amended (2026-08-09) to delete the old history outright, and the source tables no longer exist. ~~The tooling stays as the template for any future stack-to-stack move.~~ **Deleted 2026-08-31** (Murphy's call): with the source tables gone it can never run again, and carrying dead code outweighed the template value. The template survives in history — `git show 36767ea:deploy/migrate/migrate_connections.py` — and its tests went with it. |
| H | H2 — staging cutover | ✅ executed 2026-08-06 | `docs/cutover.md` is the runbook **and now the record** — its log is filled retroactively, including the steps that were skipped and why. First end-to-end job same day; the edge-freeze step was never applied and was closed by H4 deleting the routes instead. |
| H | H4 — gateway removal | ✅ executed 2026-08-09 | hybridInference#1235 (supersedes #1184; `docs/gateway-removal.md` was the checklist). Merged and deployed to staging **and** production the same day; the old agent routes answer 404 on both, and the gateway boots with zero `AGENT_*` variables. |
| H | H5 — operations doc | ✅ merged | `docs/operations.md`, moved from the source repo's `docs/developer/agent-sandbox-operations.md`; deviations listed in its own closing section. The README now describes the post-split service boundaries and retained native-session architecture; open-source readiness tracks in #14. |
| H | H3 — production cutover | ✅ executed 2026-08-07 | `/agents` on `freeinference.org` serves this stack; identity + grants answer there (a real grant has been minted on production). hybridInference#1229 — production's console never joined the agent network — was the last piece. |
| G | G1–G7 — dynamic host pool | ✅ merged | the host pool arrived as two additive revisions on the chain this record describes (`0002_host_pool` + `0003_attempt_host`), both folded into `0001_schema_baseline` by the 2026-09-10 rewrite; enrollment, heartbeat, the status machine, host-aware claiming with atomic slots, per-host broker routing, drain/remove/revoke, and the host-loss chaos test. **Extends** `agent_runner_hosts` rather than adding the plan's second `agent_hosts` table — see Open decisions. The host-side client that redeems an enrollment token **is written**: `cloud_agent_host/enrollment.py` + `runner.py:2180`, resolving the register row that used to sit above. |
| E–H | remaining control plane | ✅ nothing left in this table's scope | the phase plan is complete; post-split work tracks in this repository's issues and PRs (per-user MCP: #49/#50; open-source readiness: #14) |

**Verified end to end on staging, 2026-08-06.** The first job to complete on
this stack — `ajob_8b0be5f9e7ff9ad5` — went queued → claimed on the remote host
→ workspace prepared through the broker → checked out → `deepseek-v4-flash`
answered with tool calls → `succeeded`. The split's contract ran with it: the
gateway minted `agr_01KZBRDGJ7E7RZHS6QD6ZSABMD` for the attempt, the control
plane renewed it four times at 30-second intervals, and it was revoked sixteen
seconds after the job finished rather than left to expire. That is smoke.md § 4
(enrolment), § 5 up to `succeeded`, and the renewal half of § 6, on real
infrastructure rather than a single host.

**What it took, and what neither side checked.** The job before it failed three
attempts at the same point, because the control plane could not reach the
remote host's workspace broker. Two independent causes, one after the other:
the host advertised a compose service name that means nothing on the control
plane's machine (fixed in #41), and then the two machines turned out to hold
*different* values for the shared `AGENT_WORKSPACE_BROKER_TOKEN`, which turned
every broker call into a 401. Neither is refused anywhere — the first surfaces
as a 503 and the second as a 401, both indistinguishable from a broken tunnel.
`docs/cutover.md` now carries the token as a blocker to check before step 6.

Still unproven on this stack, and the reason smoke.md's log rows 5 and 6 stay
blank: **the publishing path** (a job that opens a pull request — verified only
against a local bare repository), **a mid-run cancel**, and **the fail-closed
cases** in smoke.md § 6. `docs/cutover.md`'s log records the same three.

Update this table as tasks land. It is the answer to "what is actually done" —
the plan describes intent, this describes reality.

## Schema baseline rewrite (2026-09-10)

The Alembic chain this record describes — `0001_baseline` through
`0029_identity_namespace` — was replaced by a rewritten baseline plus three
additive revisions: `0001_schema_baseline`, `0002_request_keys_and_actors`,
`0003_thread_level_config` and `0004_residency_on_membership`, which is `head`.
The rewrite landed before the
service took real traffic. The pre-launch schema review found the database
enforcing almost none of the store's invariants (a job whose thread does not
exist, a `current_attempt_id` from another job, a membership across tenants,
a snapshot naming a job that does not exist were all accepted) and carrying
rolling-deploy compatibility for writers that no longer existed: the
`agent_thread_sandboxes` and `agent_job_artifacts` tables, the `legacy_1to1`
sandbox mode, the `retained_followup_eligible` cutover flag and the two
`native_turn_no` triggers. The rewritten baseline adds the composite foreign
keys and CHECKs, replaces `included_job_ids` with `agent_snapshot_jobs`, and
drops the residue; its module docstring is the design record. The September 10 record originally
stated that every old-chain database had been rebuilt. The P6 inventory on
September 18 disproved that: production still held jobs, messages and host state
at `0025_retained_followup_gate`. The old revision files are no longer in this
tree, but that surviving database still requires an explicit transition.

For this supported revision, `scripts/convert_legacy_database_copy.py` preserves
the entire original schema and converts a separately restored copy. See
`docs/operations.md` § *Preserving an old-chain database* and the conversion
contract in `docs/plans/2026-09-18-legacy-database-copy.md`. The selected-state
rebuild below is only appropriate when discarding task history is intentional;
it is not a substitute for that conversion. `docs/minimal-identity.md` carries
the separate identity-binding requirements.
