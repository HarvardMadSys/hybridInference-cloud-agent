# Removing the cloud agent from the gateway

> **Status, 2026-08-09: this deletion has been executed.** H4 shipped as
> hybridInference#1235 and was deployed to staging **and** production the same
> day; `docs/cutover.md`'s log and MIGRATION.md's H4 row are the record. The
> gateway's agent routes answer 404 on both, `apps/backend/serving/agent_jobs/`
> is gone, and the gateway boots with zero `AGENT_*` variables. Read everything
> below as the checklist that was followed and the reasoning behind each
> deletion — not as pending work. Two notes are struck in place below, where the
> precondition they describe was resolved before the deletion ran.

The checklist for the **source repository**, `HarvardMadSys/hybridInference`,
after production cutover (task H4). Nothing here happens in this repository —
it is written here because this is where the migration's registers live, and a
deletion list that is not derived from them deletes the wrong things.

**Do not start until H3 is complete and soaked.** The gateway's copy is the
rollback path right up to that point (see
[docs/cutover.md](cutover.md) § Rollback), and rollback stops existing the
moment this list is executed. H3 was executed 2026-08-07 and H4 followed on
2026-08-09.

Every path below was read at the freeze commit `764a6f97`, and line numbers are
that commit's — **with one marked exception**. §1.4's identity paths are C-phase
work that landed *after* the freeze, so they do not exist at `764a6f97` at all;
they are read on `origin/dev` and the section says so. Nothing else in this
document is a `dev` path.

Every path, line number and count here was re-verified against `764a6f97` on
2026-08-04. `git cat-file -e 764a6f97:<path>` is the check for existence;
`git show 764a6f97:<path> | sed -n '<n>p'` for a line.

---

## 1. What stays — read this first

The rule is not "anything agent-shaped goes". It is **inference stays, agents
go**, and four things on the gateway side are inference wearing an agent name.

### 1.1 The grant verification path

| Path | Why |
|---|---|
| `apps/backend/serving/agent_jobs/model_auth.py` | Verifies the capability token a sandbox spends inference with. The signing secret must never leave the gateway, so verification cannot move. |
| `agent_grants` table + its DDL | The fence, after the split. The gateway no longer has an `AgentJobStore` to check an attempt's lease against, so a gateway-owned grants table plus an explicit revoke from the control plane replaces it. |
| `tests/unit/test_agent_model_auth.py` | Its tests. They stay with it. |

**This module cannot survive a naïve `rm -rf agent_jobs/`.** At the freeze it
imports two things the same command deletes:

```text
model_auth.py:29   from serving.agent_jobs.tokens import InvalidAgentToken, parse_worker_token
model_auth.py:33   if TYPE_CHECKING: from serving.storage.agent_job_store import AgentJobStore
```

So relocating it (the plan suggests `serving/grants.py`) means carrying the
token *parsing* it needs and dropping the `AgentJobStore` fence in favour of
the `agent_grants` lookup. The plan's "delete legacy `ajt` model-token
acceptance" is the same edit seen from the other side.

`tests/unit/test_agent_model_auth.py:14` also imports
`serving.agent_jobs.tokens.mint_worker_token` — re-point it at whatever mints
grants, or the retained test does not import.

> ⚠️ ~~**`agent_grants` does not exist yet.**~~
> **Resolved before H4 ran.** This note guarded on
> `git grep agent_grants origin/dev -- apps/ tests/` returning nothing, which was
> true when it was written and false by the time H4 executed: the table and its
> router landed as C5–C8 (hybridInference#1180–#1183, #1191, #1195), and the
> grep now hits `apps/backend/serving/grants.py` and
> `apps/backend/serving/servers/routers/agent_grants.py`. `model_auth.py` had
> something to relocate onto, and the section above stopped being a plan. The
> scoping lesson is kept because it is still the right way to read the grep: a
> bare `git grep agent_grants origin/dev` also hits
> `docs/agents/plans/2026-08-03-cloud-agent-repo-split.md`, which *specifies* the
> table rather than implementing it — scope the grep to the code, or a plan looks
> like the work is done.

### 1.2 The MCP proxy

An MCP tool call is authenticated with the same capability token as a model
call, so the proxy stays on the gateway side. Three modules and a config
example:

| Path | Note |
|---|---|
| `apps/backend/serving/agent_jobs/mcp_proxy.py` | Request planning, tool filtering, SSE filtering. |
| `apps/backend/serving/agent_jobs/mcp_registry.py` | The deployment's server registry (`MCP_CONFIG_PATH`). |
| `apps/backend/serving/servers/routers/agent_mcp.py` | `/v1/agent/mcp/...`, mounted at `servers/app.py:108`. |
| `config/examples/mcp.example.yaml` | The registry example the docs tell operators to copy. |
| `tests/unit/test_agent_mcp.py`, `tests/servers/test_agent_mcp_api.py` | Their tests. |

Two of these live *inside* `agent_jobs/`, which this task deletes, so they must
be **relocated, not merely spared**. And the router's imports do not survive
the deletion either:

```text
agent_mcp.py:47   from serving.agent_jobs.model_auth import (...)      # relocates with §1.1
agent_mcp.py:52   from serving.servers.deps import get_agent_job_store  # deleted below
```

The store dependency is the fence again: after the split there is no attempt
state on the gateway, so the proxy's "is this attempt still live" check must
come from `agent_grants` exactly as the model path's does.

**Two retained tests will not import after the deletion.** Fix them in the same
pull request, not afterwards:

| Test | Import | Deleted by |
|---|---|---|
| `tests/unit/test_agent_mcp.py:21` | `serving.agent_jobs.runtimes` (`MCP_TOKEN_ENV`, `ClaudeCodeRuntime`, `CodexRuntime`, `RuntimeMCPConfig`, `RuntimeMCPUnavailableError`, `runtimes_supporting_mcp`) | §2.1 — `runtimes.py` moved to the host package |
| `tests/servers/test_agent_mcp_api.py:20,21` | `serving.agent_jobs.tokens` (`SCOPE_MODEL`, `mint_worker_token`) and `serving.servers.deps.get_agent_job_store` | §2.1 and §3 |

The runtime adapters' MCP behaviour is now the *host's* property and is tested
here, in `tests/unit/test_agent_runtimes.py`. The gateway's copy of those cases
should go with the module rather than be kept alive by a shim.

### 1.3 Billing attribution

| Path | What |
|---|---|
| `apps/backend/serving/storage/log_schema.py:88,143,245` | `api_logs.agent_job_id` column and its partial index. |
| `apps/backend/serving/storage/postgres_log.py:310,324` | `get_agent_job_cost` / `get_agent_job_usage`. |
| `apps/backend/serving/storage/database.py:835,897` | `agent_job_id` in the hand-duplicated `api_logs` INSERT. |
| `servers/routers/completions.py:597-601`, `anthropic_messages.py:1075-1076`, `embeddings.py:172` | Propagate `agent_job_id` from the capability token into log metadata. |

This is the ledger that answers "what did that job spend". It never moves —
monetary enforcement is the gateway's per-user quota and always was. **There is
no per-job budget**; do not re-add one on the way out.

### 1.4 Identity

> **These are `dev` paths, not freeze paths — the one exception to the header.**
> C1–C4 landed *after* `764a6f97`: `identity.py` arrived with #1177 on
> 2026-08-03, hours after the freeze. `git cat-file -e 764a6f97:<path>` fails
> for **all seven paths named in this section** and succeeds for all seven on
> `origin/dev` (re-counted and re-checked 2026-08-04), so line numbers here are
> `origin/dev`'s and will move as `dev` does. Re-read them when H4 is
> scheduled; treat the route definitions, not the numbers, as the identifier.

The endpoints this service authenticates against, added by C1–C4 and already on
`dev` (router prefix `/v1/identity`, declared at `identity.py:45`):

```text
GET  /v1/identity/jwks     serving/servers/routers/identity.py:54   (origin/dev)
POST /v1/identity/code                                       :144   (origin/dev)
POST /v1/identity/token                                      :197   (origin/dev)
```

The seven, in full — one backend router, two backend utilities, one storage
test, and three frontend files:

```text
apps/backend/serving/servers/routers/identity.py          (the three routes above)
apps/backend/serving/utils/identity_keys.py
apps/backend/serving/utils/identity_tokens.py
tests/integration/storage/test_identity_auth_codes.py     (names the auth-code storage)
apps/frontend/src/app/authorize/page.tsx                  (the consent page)
apps/frontend/src/app/authorize/page.test.tsx
apps/frontend/src/lib/api/identity.ts
```

None of it is agent code; it is the login contract, and the agent service is
only its first consumer.

### 1.5 Not agent code at all

`tests/unit/storage/test_agent_name_from_prompt.py` (314 lines, 30 test
functions, 60 executed cases — eight of them are parameterized)
matched the manifest grep and is **not** cloud agent. It tests
`serving/storage/utils.py::agent_name_from_prompt`, whose only consumer is
`serving/storage/postgres_log.py:215` — parsing *which coding CLI* made an API
call, for log analytics. It has no counterpart in this repository and must not
be deleted.

The plan's "delete the 25 agent test files" would take it. Do not.

---

## 2. What goes

### 2.1 The backend package

Delete `apps/backend/serving/agent_jobs/` **except** `model_auth.py`,
`mcp_proxy.py` and `mcp_registry.py`, which relocate per §1.1 and §1.2:

```text
__init__.py            egress.py              egress_proxy.py
entitlement.py         github_app.py          patch_gate.py
publish_worker.py      publisher.py           runner.py
runtimes.py            sandbox.py             setup.py
source_control.py      terminal_coordination.py
tokens.py              workspace_broker.py    workspace_broker_client.py
workspace_browser.py   workspace_paths.py     workspace_snapshot.py
```

> ⚠️ **`visible_models.py` is a decision, not a deletion.** It is listed in the
> manifest as "rewritten, not moved", but the rewrite happened *here* — this
> repository asks the gateway over HTTP instead. On the gateway side the module
> is the natural implementation of the `/internal/model-catalog` contract this
> service already calls (`cloud_agent/gateway.py:120`), and
> MIGRATION.md's discarded-test record says so explicitly: the
> published/role/disabled/chat-type filters "stayed behind". It imports only
> `serving.config.settings.has_role` and `serving.model_access`, so it has no
> tie to anything else being deleted.
>
> Decide with C9: if `/internal/model-catalog` is built on it, keep it and
> `tests/unit/test_agent_visible_models.py` with it. If that endpoint is
> written from scratch, delete both. Deleting it *while this service calls the
> endpoint* is the one outcome that must not happen.

### 2.2 Backend, elsewhere

| Path | Note |
|---|---|
| `apps/backend/serving/servers/routers/agent_jobs.py` | 2,550 lines at the freeze — not the manifest's "2,257". |
| `apps/backend/serving/servers/routers/admin/agent_runner_hosts.py` | |
| `apps/backend/serving/schemas_agent_jobs.py` | |
| `apps/backend/serving/storage/agent_job_store.py` | |
| The nine agent tables | `agent_threads`, `agent_jobs`, `agent_attempts`, `agent_job_events`, `agent_thread_messages`, `agent_job_artifacts`, `agent_repo_grants`, `agent_oauth_states`, `agent_gitlab_connections` — plus `agent_runner_hosts` and `agent_runner_policy`, which the manifest's count of nine omits. Eleven tables. Drop them only after H1's import is confirmed and the backups are retained. |

Also relevant to `schemas_admin.py`: the three classes `AgentRunnerHost`,
`ListAgentRunnerHostsResponse` and `SetActiveAgentRunnerHostRequest` were
copied into this repository's `schemas.py`. On the gateway they go with the
admin router.

### 2.3 Wiring to strip (files stay, agent parts go)

| Path | What goes |
|---|---|
| `servers/app.py:21,22,107,108` | `agent_jobs` and `agent_mcp` router imports and `include_router` calls. `agent_mcp` **stays** — re-point its import at the relocated module rather than deleting the line. |
| `servers/bootstrap.py:796-892,1274-1299` | `AgentJobStore` init, the attempt reaper task, the publish loop, GitHub App and GitLab OAuth credential wiring, and the reaper's shutdown cancel. The block starts at the four `agent_* = None` pre-declarations (796-799) and ends at the closing paren of its own `logger.info` (892). |
| `servers/deps.py:73,110-113,175-193` | `AgentJobStore` import, the four service slots, `get_agent_job_store`, `get_agent_app_credentials`, `get_agent_gitlab_oauth`. |
| `servers/auth.py:15` | The `serving.agent_jobs.model_auth` import → re-point at the relocated grants module. This is the original reverse dependency the whole split exists to remove; it does not disappear, it changes address. |

### 2.4 Frontend

| Path | |
|---|---|
| `apps/frontend/src/app/agents/` | 8 files |
| `apps/frontend/src/components/features/agents/` | 30 files |
| `apps/frontend/src/lib/api/agents.ts` | |
| `apps/frontend/src/lib/api/__tests__/agents.test.ts` | **526 lines, 27 cases** — 26 `it` blocks, one of which is an `it.each` of two, which is why counting the blocks gives 26 and running the file gives 27. Absent from both the plan's F2 text and the manifest's frontend inventory — the same omission shape as `schemas_admin.py`. It covers the SSE reader (frame parsing across chunk boundaries, cursor tracking, reconnect without a gap), so its absence from a list is not evidence it does not matter. |

`/agents` becomes a redirect to the new domain.

### 2.5 Deploy and ops

| Path | Where it went |
|---|---|
| ~~`.github/workflows/agent-job-runner.yml`~~ | ~~this repo, `.github/workflows/`~~ **Removed 2026-09-02:** the manually dispatched disposable/process runner contradicted the retained-only topology; enrolled sandbox hosts are the sole execution substrate. |
| `deploy/docker/Dockerfile.agent-runner` | `deploy/host/` |
| `deploy/docker/Dockerfile.agent-sandbox` | `deploy/sandbox/` |
| `deploy/docker/Dockerfile.agent-egress-proxy` | `deploy/host/` (since replaced by the one sandbox network, see docs/specs/2026-09-23-agent-public-egress-design.zh-CN.md) |
| `deploy/docker/Dockerfile.agent-gateway-tunnel` | `deploy/host/` |
| `deploy/docker/agent-gateway-tunnel.sh` | `deploy/host/` |
| `deploy/docker/docker-compose.sandbox.yml` | `deploy/host/` |
| `ops/deploy/sandbox_host.sh` | `deploy/host/` |
| `ops/setup/setup_kata_runtime.sh` | `deploy/host/setup_kata_runtime.sh` |
| `tests/unit/ops/test_setup_kata_runtime.py` | `tests/unit/test_setup_kata_runtime.py` |

> The last two **do not match the manifest's grep** — no "agent" in either
> name — so regenerating the manifest will never surface them. They are listed
> here because MIGRATION.md caught the test file the same way, by hand.

Also delete the sandbox image's four wrapper assets, which moved with the
Dockerfile in D7 and which the manifest also omitted:
`deploy/docker/pi-freeinference`, `opencode-freeinference`,
`kilo-freeinference`, `codex-requirements.toml`.

### 2.6 The co-located runner overlay — four files, decided

MIGRATION.md's open-decisions register resolved this on 2026-08-04: **option 1,
drop it.** The standalone remote-runner compose is the only topology, and a
co-located runner is that compose with the tunnel pointing at localhost.

Delete all four:

```text
deploy/docker/docker-compose.agent-runner.yml
ops/deploy/agent_runner.sh
tests/unit/test_agent_deploy_config.py          # asserts on that compose
tests/unit/ops/test_agent_runner_preflight.py   # executes that script
```

The guarantees those tests pinned (internal egress network, read-only docker
socket, non-root sandbox, Kata preflight) are covered here by
`tests/unit/test_deploy_config.py`, `test_deploy_security.py` and
`test_setup_kata_runtime.py` — ported whole, with three assertions skipped for
the Trusted-tier gap the decision came with. Read that register entry before
deleting, so the skips are not a surprise later. (Since replaced by the one
sandbox network, see docs/specs/2026-09-23-agent-public-egress-design.zh-CN.md.)

### 2.7 Tests

Delete 24 test files and one fixture directory:

```text
tests/integration/servers/test_agent_jobs_lifecycle.py
tests/integration/storage/test_agent_job_store.py
tests/integration/test_agent_publisher.py
tests/servers/test_admin_agent_runner_hosts.py
tests/servers/test_agent_jobs_api.py
tests/unit/ops/test_agent_runner_preflight.py
tests/unit/test_agent_deploy_config.py
tests/unit/test_agent_egress.py
tests/unit/test_agent_egress_proxy.py
tests/unit/test_agent_entitlement.py
tests/unit/test_agent_github_app.py
tests/unit/test_agent_job_tokens.py
tests/unit/test_agent_patch_gate.py
tests/unit/test_agent_publish_worker.py
tests/unit/test_agent_runner.py
tests/unit/test_agent_runner_worktree.py
tests/unit/test_agent_runtimes.py
tests/unit/test_agent_sandbox.py
tests/unit/test_agent_setup.py
tests/unit/test_agent_source_control.py
tests/unit/test_agent_workspace_broker.py
tests/unit/test_agent_workspace_broker_client.py
tests/unit/test_agent_workspace_snapshot.py
tests/unit/ops/test_setup_kata_runtime.py
tests/fixtures/agent_runtime_streams/           # claude_code_stream_contract.jsonl
```

Plus `tests/unit/test_agent_visible_models.py` **only if** §2.1's decision goes
that way.

Arithmetic, because the plan's "25 agent test files" does not survive contact.
The manifest grep matches **29 paths** under `tests/` at the freeze: 28 test
files plus one fixture. Of the 28 —

- **4 stay**: `test_agent_mcp.py`, `test_agent_mcp_api.py` (§1.2),
  `test_agent_model_auth.py` (§1.1), `test_agent_name_from_prompt.py` (§1.5).
- **1 is conditional**: `test_agent_visible_models.py`, per §2.1.
- **23 are deleted**, listed above.

`tests/unit/ops/test_setup_kata_runtime.py` makes 24. It carries no "agent" in
its name, so the grep never matched it and a regenerated manifest never will —
it is on the list because MIGRATION.md caught the same omission during D7.

---

## 3. The two stale references

`patch_gate.py` leaves in D4 and its package is deleted here, so anything
pointing at its path breaks **at this commit**, not before:

| Path | Line | What it does |
|---|---|---|
| `ops/release/public_export.py` | 36 | `_PATCH_GATE = REPO / "apps" / "backend" / "serving" / "agent_jobs" / "patch_gate.py"` — loaded by path to reuse the sandbox's credential-shape regexes in the open-source export audit. |
| `tests/unit/release/test_public_export_audit.py` | 161 | The same path again, asserting the reuse is real and that the gate grew no import the release tool cannot satisfy. |

The manifest's coordination note names only the first. Both had to be dropped or
re-pointed **before** this pull request landed, and that was the neutral-upstream
split's owner to do — it is the concrete instance of the "no repo-wide sweeps
over agent paths" coordination rule. Two options were listed: inline the
credential shapes into the export tool (accepting that two lists now drift,
which is what the reuse existed to prevent), or vendor a copy of the shapes into
`ops/release/`.

**Neither option was taken, because the question was dissolved.** The
public-export mechanism itself was retired on 2026-08-26 (hybridInference#1316):
the repository is being published whole, so nothing is exported from a filtered
tree any more. `ops/release/public_export.py` exists on neither the gateway's
`dev` nor its `main`, and neither does its test. The §4 acceptance item resting
on it went with it, as did the manifest's "hard-referenced by
`public_export.py:36`" note.

---

## 4. Acceptance

Every item below was checked when H4 executed on 2026-08-09; hybridInference#1235
states the result ("Acceptance — all four, on this tree") — zero `AGENT_*`
variables, 147 routes, `rg 'agent_jobs' apps/` with no hits, 4,174 tests. The
list is kept unticked as the checklist that was run rather than as open work;
the database item is additionally pinned by
`tests/unit/storage/test_no_agent_tables.py`.

- [ ] `rg 'agent_jobs' apps/` returns nothing outside the relocated grants and
      MCP modules.
- [ ] The gateway boots with **zero** agent environment variables set.
- [ ] A fresh database gets no agent tables — `agent_grants` and the identity
      code storage excepted.
- [ ] `make test` is green, including the retained MCP and `model_auth` tests
      with their imports re-pointed (§1.1, §1.2).
- [ ] ~~`ops/release/public_export.py` runs (§3).~~ **Withdrawn** — the tool was
      retired on 2026-08-26 (hybridInference#1316) and does not exist.
- [ ] `/agents` redirects to the new domain.
- [ ] Backups of the eleven dropped tables are retained somewhere with a stated
      expiry, and H1's import has been confirmed against production data — not
      just staging.

## 5. Order

1. C5–C8 land: `agent_grants` exists on the gateway and the control plane mints
   through it. **Without this, §1.1 has nowhere to relocate to.**
2. H3: production cutover, then soak.
3. The export-tool references (§3) are fixed by their owner.
4. This pull request.
5. H5: the source repo's `CLAUDE.md` agent sections become a pointer here.
