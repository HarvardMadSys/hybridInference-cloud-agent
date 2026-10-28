# AGENTS.md

Onboarding guide for anyone working in this repository — human or AI coding
agent. Read this first.

## 1. What this is

Cloud Agent runs coding agents (Claude Code, Codex, pi, OpenCode, Kilo) in
sandboxed containers against a user's repository and, when the owner asks,
publishes results as pull requests.

The extraction from the HybridInference monorepo
(`HarvardMadSys/hybridInference`) is complete: production cut over on
2026-08-07 and the gateway's own agent code was deleted on 2026-08-09.
[The repository extraction record](docs/agents/reviews/2026-08-09-repository-extraction-record.md)
records the split, its decisions, and deferred work. The extraction plan in the HybridInference repo
(`docs/agents/plans/2026-08-03-cloud-agent-repo-split.md`) is historical; new
work tracks in this repository's issues and pull requests.

## 2. Repo map

```text
apps/
├── backend/
│   ├── cloud_agent/           control plane (FastAPI): API, auth adapter,
│   │                          scheduler, publishing, storage
│   ├── cloud_agent_host/      host agent: runner, sandbox, runtimes, sandbox manager
│   └── cloud_agent_shared/    vocabulary both images ship: capability strings,
│       │                      protocol versions, pins, sandbox network, shared errors,
│       │                      and the operation-profiling collector both sides run
│       └── git/               the patch gate and the source-archive builders
└── frontend/                  Next.js user UI + admin dashboard
gateway_contracts/             OpenAPI mirrors of the gateway contracts we consume
migrations/                    Alembic migrations (agent-owned database only)
deploy/{control-plane,host,sandbox}/
tests/{unit,servers,integration,external}/  # plus fixtures/ and support/
docs/{developer,user,agents}/  maintained guides, user notes, agent records
```

Not every module is worth a line here; these are the ones a newcomer needs to
find early. The control plane's database schema is **one baseline revision plus
additive ones** (`migrations/versions/0001_schema_baseline.py` and on), not a
long chain — the extraction record's § *Schema baseline rewrite* explains why, and
`docs/developer/operations.md` § *Rebuilding the agent database* is the runbook for a
database stamped with the chain that predates it.

| Module | What it owns |
|---|---|
| `apps/backend/cloud_agent/app.py` | The two listeners: the main API and the MCP-only one |
| `apps/backend/cloud_agent/api/` | One router module per surface — jobs, hosts, admin, session, MCP relay |
| `apps/backend/cloud_agent/schemas/` | Domain-organized wire models, explicitly exported by `cloud_agent.schemas` |
| `apps/backend/cloud_agent/application/` | Flows that run without a request: job admission and creation, native Fork materialization, residency credentials |
| `apps/backend/cloud_agent/bootstrap/` | Internal assembly: the resources the listeners are built from, and the lifespan that starts and stops them |
| `apps/backend/cloud_agent/workers/` | The background loops — attempt reaping, grant renewal, queue and goal recovery, shared-workspace snapshot capture |
| `apps/backend/cloud_agent/ports/`, `apps/backend/cloud_agent/storage/` | Narrow capability contracts and Postgres repositories: claims, fences, residency, publication |
| `apps/backend/cloud_agent/grants.py`, `ledger.py`, `grant_usage.py` | Gateway inference grants, and the usage snapshots read back from them |
| `apps/backend/cloud_agent/gateway.py`, `agent_access.py`, `identity.py` | The three gateway contracts: lookups, admission, identity |
| `apps/backend/cloud_agent/identity_namespace.py` | Which Gateway minted the `user_id`s this database is keyed by |
| `apps/backend/cloud_agent/mcp/`, `api/mcp_connections.py`, `api/mcp_relay.py` | Per-user connections, protocol hosting, authorization and the MCP relay |
| `apps/backend/cloud_agent/mcp/runtime.py` | Process-local MCP host, fenced session maintenance, and shutdown |
| `apps/backend/cloud_agent/publisher.py`, `publish_worker.py`, `source_control.py` | Branch and draft-PR publication, and the GitHub/GitLab credentials behind it |
| `apps/backend/cloud_agent/admin_observability.py` | The cross-user admin reading surface and its audit |
| `apps/backend/cloud_agent_host/enrollment.py` | Host registration and the persisted host credential |
| `apps/backend/cloud_agent_host/runner.py` | Claims, executes one turn, heartbeats, drains on SIGTERM |
| `apps/backend/cloud_agent_host/claimed_job.py`, `control_plane_client.py` | Claim payloads, lease errors, and attempt-scoped worker HTTP calls |
| `apps/backend/cloud_agent_host/chat_sandbox.py`, `sandbox.py` | Retained group lifecycle and the Docker/Kata boundary |
| `apps/backend/cloud_agent_host/chat_sandbox_network.py` | Per-group bridge and relay lifecycle, guest network proof, and exact network release |
| `apps/backend/cloud_agent_host/chat_sandbox_models.py` | Retained-container specs, inspection/status values, and identity vocabulary |
| `apps/backend/cloud_agent_shared/egress.py`, `apps/backend/cloud_agent_host/network_guard.py` | The one sandbox network: its configuration and host rules, and the helper that installs and verifies them |
| `apps/backend/cloud_agent_host/runtimes/`, `cloud_agent_shared/runtime_catalog.py`, `shared_protocol.py` | The five Agent CLI adapters, shared discovery facts, and version pins |

[Code structure](docs/developer/code-structure.md) maps module responsibilities and
dependency direction, including the shared profiler and frontend API/UI modules.
Neither implementation package imports the other; shared facts live in
`cloud_agent_shared`. Lifecycle wiring must not import HTTP routers, and callers
outside storage must not import repository implementations.

## 3. Quality gates

```bash
make setup-dev   # uv venv + uv sync
make check       # format + lint + type-check + test — must be green before every PR
```

`make test` runs everything except the `external` and `dbtest` markers.
Database-backed tests are opt-in. When a change touches storage, SQL, a
migration, or a database test, run `make test-db-local` (needs Docker). It uses
this machine's shared test PostgreSQL, gives the checkout fresh databases of its
own on every run, and fails if any database test was skipped; `ARGS="<paths>
-x"` narrows it. Never run `make test-db-local-down`: it discards every
worktree's databases and fails their runs. `make test-db` runs the tier against
a PostgreSQL you provide (see [ENVIRONMENT.md](ENVIRONMENT.md)).

For frontend changes, use Node.js 22 and run `make ci-frontend`. It installs the
locked dependencies with `npm ci`, then checks lint, formatting, types, tests,
and build guards. `make setup-dev` installs backend dependencies only.

The developer documentation site is published in English and Simplified
Chinese. When a change edits a page under `docs/developer/` or a root guide the
site includes (`ENVIRONMENT.md`, `CONTRIBUTING.md`, `SECURITY.md`,
`deploy/local/README.md`, `deploy/release/README.md`,
`gateway_contracts/README.md`), run `make docs-translate`, translate the
entries it adds or marks fuzzy, and run `make docs-verify`. On the stable
pages (the home page, the developer guide, the code structure page, and the
`SECURITY.md` and `CONTRIBUTING.md` the site includes), CI fails while a string
has no catalog entry or a fuzzy one; on the other pages it warns. No page
catches an entry left empty, which renders in English.
[Translating the docs](docs/developer/translations.md) has the workflow and
terminology.

Complexity and size only ratchet down (#771). New code stays within the limits:
backend functions within McCabe 15, 12 branches and 50 statements, backend
modules within 1,500 lines, frontend functions within complexity 15 and frontend
files within 800 lines. The backend limits cover `apps/backend` only, not
tests, scripts, `deploy/` or migrations; the frontend limits skip
`*.test.ts(x)`, `__tests__/` and `e2e/`.

- A backend function over a limit today carries a `noqa` naming #771. Never add
  one to new code. Once the function is back under, `make check` deletes the
  `noqa` (ruff's `RUF100` fix).
- Oversized backend modules are listed in `tests/unit/test_file_size_budget.py`
  and may not grow. Lower an entry when its module shrinks.
- `apps/frontend/eslint-ratchet.json` lists the frontend files over a limit,
  each at its current value. ESLint applies a listed value to every function in
  that file, so it lets a new function there go past 15; keep new functions
  within 15 anyway. Lower an entry when its file improves.

If `apps/frontend/node_modules` is absent, install it before reporting checks
as blocked. From `apps/frontend`, run `npm ci`, `npm run type-check`, and
`npm test`; `vitest` and `tsc` come from the project's development dependencies.

## 4. Workflow

- Branch off `dev` as `<user>/<scope>/<name>`. Pull requests target `dev`.
- Keep each pull request focused on one feature, fix, or documentation change.
- Put documentation where [docs/README.md](docs/README.md) expects it.
  Maintained guides go in `docs/developer/`. New designs and implementation
  plans go in `docs/agents/specs/`; verification and review records
  (acceptance runs, reviews of a revision) go in `docs/agents/reviews/`. Name
  records `YYYY-MM-DD-<topic>.md`. Do not create other top-level folders under
  `docs/`: tools that default elsewhere, such as the superpowers plugin's
  `docs/superpowers/`, must write to these folders instead.
- Never commit secrets. This repository will be made public; treat every commit
  as already public.
- Hosts roll out before the control plane, and each side supports the previous
  release only. Managed compute hosts are the exception: the new control plane
  rolls them forward once it runs
  ([Compute resources](docs/developer/operations-compute.md)). Code kept for an
  older release is marked
  `COMPAT(remove-after=YYYY-MM-DD): <what makes it removable>` in a comment, and
  `tests/unit/test_compat_markers.py` fails once that date has passed: remove
  the code then, or move the date with the reason it must stay.

## 5. Migration rules — historical scope and permanent boundaries

The move-only restrictions in rules 1 and 2 expired when the split completed on
2026-08-09. They remain here for reading migration pull requests; cleanups,
renames, and refactors are ordinary new work now. Rule 3 remains a quality
requirement, and rules 4 and 5 remain permanent service boundaries.

1. **Move as-is.** When a task says "move" a file, change imports and paths
   only. No renames, no splitting large files, no reformatting, no dead-code
   removal, no "while I'm here" fixes. A reviewer will diff your result against
   a pristine copy from the source repo and expect only import lines to differ.
   Cleanups happen after cutover, as their own pull requests.
2. **If it cannot move without a logic change, stop and report.** Do not
   improvise a redesign inside a migration task.
3. **Never weaken a test to make it pass.** A ported test that fails is a parity
   break — report it. Adapting fixtures and imports is fine; changing an
   assertion is not.
4. **No gateway secrets here.** This service never sees the gateway's
   `API_KEY_SECRET` or `JWT_SECRET_KEY`. Grant tokens are minted *and*
   verified by the gateway. Our own tokens use our own secrets
   (`AGENT_CONTROL_TOKEN_SECRET`, `AGENT_SESSION_SECRET`).
5. **The gateway is reached over HTTP only.** No Python import of `serving.*`,
   ever. If you need something the gateway knows, it needs a contract in
   `gateway_contracts/`.

## 6. Key concepts

- **Job / attempt / lease.** A job is the user's task; an attempt is one
  execution of it on one host. Attempts carry a lease with a fencing
  generation — a superseded attempt's writes are rejected by the store, which
  is what makes retries safe and stops duplicate pull requests.
- **Grant.** A short-lived capability token minted by the gateway, scoped to a
  user, a job or chat, and a model allowlist; it authorizes inference. Each
  claim mints a per-attempt grant, revoked when the attempt is superseded or
  ends. The sandbox CLI does not run on that grant: each chat member's CLI runs
  on its own residency grant, minted before the CLI starts, renewed by host
  heartbeats while the member stays resident (so it can outlive one turn), and
  revoked with the residency. MCP servers are resolved per user by the control
  plane and reached through its MCP-only relay with a separate scoped
  credential; no inference grant names them. A chat member's CLI holds its
  residency's MCP credential, which the relay resolves on every call to the
  turn that residency is running, so follow-ups keep the live process.
  **There is no per-task dollar budget** — that design was considered and
  rejected. Gateway-backed inference spend stays subject to the gateway's existing
  per-user quota; a grant narrows *what* can be called, never re-implements
  *how much*. If a task seems to need a budget field, stop — the plan's C5
  section records the decision.
- **Host.** A machine in the pool running the host agent. It registers with a
  one-time enrollment token, heartbeats, and claims jobs it has capacity and
  capabilities for. Hosts are added and removed without redeploying the control
  plane.
- **Sandbox.** A retained container and workspace per sandbox group. Each chat
  has its own native session and active CLI process; Fork adds a member to the
  same group and workspace. Native resume stays in that container. There is no
  transcript-replay or platform-checkpoint recovery path. See the implemented
  [session design](docs/agents/specs/2026-08-31-native-session-fork-shared-workspace-design.md).
  The group has **one network**, the same for setup and for the agent, with
  nothing to select
  ([design](docs/agents/specs/2026-09-23-agent-public-egress-design.zh-CN.md)).
  Each group gets its own routable bridge, and before anything joins one the
  host installs this deployment's nftables table (`cloud_agent_shared/egress.py`
  renders it, `cloud_agent_host/network_guard.py` installs and verifies it):
  - public IPv4 on TCP 80/443, so curl, pip, npm and Git over HTTPS work;
  - the one named resolver on TCP/UDP 53;
  - the two relays' service ports on the group's own bridge — the gateway for
    inference and the MCP-only listener for tools;
  - the exact public IPv4 address and port of each SSH server its owner
    registered, checked and enabled for one of the group's chats;
  - and nothing else: not the host, private or special-purpose addresses,
    other groups, other ports, UDP 443, or IPv6. Neither relay is a
    caller-selected forward proxy, and there is no proxy at all.

## 7. Security boundaries — do not relax without review

- A sandbox group reaches public IPv4 on TCP 80/443, the one named resolver,
  its own two relays, and the exact public IPv4 address and port of each SSH
  server its owner registered, checked and enabled for one of the group's
  chats — and nothing else: none of the host's addresses, no
  private, link-local/metadata, CGNAT, multicast or reserved range, nothing in
  `AGENT_EGRESS_PROTECTED_CIDRS`, no other group, and no IPv6. Per-group
  bridges and this deployment's host nftables table enforce it on the actual
  packets; the table is installed and read back before any group joins its
  bridge, and a failed check stops the group rather than falling back to
  anything. There is no mode, flag or proxy that widens it, and adding one is
  a design change, not a configuration.
- MCP tool calls are served only on the MCP-only listener; the public main
  API must not expose them. The MCP tunnel continues to target that listener.
- **Accepted inference-grant replay risk (2026-09-25).** A sandbox holds its
  member's short-lived grant, and code or instructions it encounters may
  cause that grant to leave the sandbox. The gateway may accept it through
  its public inference API until expiry or revocation. Calls still use the
  grant owner's model permissions and account quota; it grants no
  MCP or platform-management authority. Resident grants can be renewed across
  turns, so a short TTL is not a fixed total exposure window. This accepted
  risk replaces the separate internal inference entry as a release prerequisite;
  neither that entry nor a host credential-substitution relay is required.
  See the [decision and remaining boundaries](docs/agents/specs/2026-09-23-agent-public-egress-design.zh-CN.md).
- Platform-held long-lived account and service credentials never enter the sandbox. Each
  member receives its own scoped inference and MCP credentials, which a
  sibling member cannot read: members have distinct UIDs, private provider
  homes and tmpfs, and no credential is passed as a process argument. That
  stops reading, not writing: siblings share one writable workspace, `.git`
  included, so code a member plants there — a Git hook or `core.fsmonitor`,
  a build or test script — runs with the credentials of whichever member
  executes it next. Gateway-backed spend remains governed by the gateway's per-user quota;
  owner model connections use the owner's separate provider key and billing.
  The runner also injects a separate short-lived GitHub App
  token as `GH_TOKEN` in the owning member's environment, restricted to
  `contents: write` and `pull_requests: write` on the job's repository. Account
  tokens, refresh tokens, and the App's private key remain outside. Direct
  sandbox publication does not pass through the trusted publisher's patch
  gate; repository branch protection and GitHub rules still apply.
- **Owner-provided environment credentials.** An owner may deliberately inject
  personal variables and Secrets through a frozen environment binding. Repository
  code, dependencies and the agent can read them, send them through the existing
  public egress, or write them into files/model context. These are user-supplied
  credentials, not platform credentials. The platform encrypts stored values,
  keeps Secrets write-only, filters setup/runtime phases, masks known values in
  durable output and scans trusted snapshot/publication additions, including
  binary content. These controls do not prevent deliberate exfiltration or direct
  Git pushes. Same-owner Fork inherits the binding; the only cross-owner
  sharing is a team's environment, below. Reserved platform/host variable names and the existing fixed
  network and member credential boundaries still apply. An owner's
  `OPENAI_*`/`ANTHROPIC_*` variables supplied through an environment binding never
  replace an agent CLI's own inference credentials; a member-private `BASH_ENV`
  script gives them to the commands the CLI runs. See the
  [environment design](docs/agents/specs/2026-10-12-repository-environment-design.zh-CN.md).
- **Team environments (2026-10-15).** A team's owner and admins may save an
  environment for the team, and every active member may run their own tasks,
  in their own sandboxes, with it. Its Secrets are therefore readable by every
  member who runs it: the write-only editor hides them on screen, not between
  members, and the dialog says so. A member's personal globals join it only at
  run time, so its installation is the same for everyone. Its installation
  snapshots serve the whole team, and a member's task or test can queue a
  build from a commit that member checked out, so a member can shape what the
  others' sandboxes start from. Members of a team are one trust domain for its
  environments, as for a repository they can all push to. A binding records
  the member's access generation, and every read that hands its values to a
  container (each turn's claim, terminals, actions, Fork, preparation, tests)
  refuses once the member has left or the team is deleted; a value already
  read cannot be taken back, so rotate it. See the
  [team design](docs/agents/specs/2026-10-15-team-shared-workspace-design.zh-CN.md).
- **Owner SSH servers (2026-10-15).** An owner may register their own SSH
  servers and enable them per chat. The platform generates one Ed25519 key per
  server, stores it encrypted, and writes it only into the enabled chat
  member's private tmpfs, with the host key the owner confirmed pinned. The
  agent, repository code and anything they run can read that key, use or
  exfiltrate it, and run anything the server account may run; an owner revokes
  it by deleting the server, which cuts live sessions and erases the stored
  key, and by removing the key line from `authorized_keys`. Keys are masked in
  the runner's durable output: a key reaches a member only when that member's
  own turn starts, from that turn's claim, and a push mid-turn only takes
  access away, so every key in a sandbox is one its running turn masks.
  Snapshot and publication scanning do not look for them. The control plane
  dials a server only when its owner runs a check, under the same
  public-address rules, an hourly per-owner limit, and a 64 KB cap on what it
  reads back. See the
  [SSH server design](docs/agents/specs/2026-10-15-user-ssh-servers-design.zh-CN.md).
- **Owner model connections (2026-10-27).** An owner may add their own
  inference endpoint (Base URL, API key, model ids) and run
  their own tasks on it instead of the Gateway; they pay the provider. It is
  private to them: never shared with a team, never reachable through another
  owner's Fork, and every claim, residency, follow-up, waiting message, goal
  round and Fork re-reads it as theirs. The key is encrypted under its own
  domain, write-only, and handed to a host only in the claim and residency of
  its owner's turn on it; the member's CLI holds it in its environment and
  activation tmpfs, never in argv, and the runner masks it in durable output.
  Repository code, dependencies and the agent can read and exfiltrate it
  through the existing public egress, as with owner environment credentials.
  Trusted snapshot and publication paths scan the exact sandbox group's used
  key history, including rotations and same-owner Fork members. An encrypted
  scanning copy is recorded only when a key is bound to a residency and kept
  with the sandbox group record, even after connection deletion; execution
  paths never select it. It remains a real provider secret, and deletion here
  does not revoke it upstream. Other groups' keys never enter this scan;
  exceeding its bounded history fails closed. Such a turn needs no
  Gateway grant, catalog or quota, but login and Agent admission stay
  Gateway-backed. Which APIs a model is reached on is per model, found only
  by its owner's explicit verification of that model: a streaming tool round
  trip on each of Chat Completions, Responses and Anthropic Messages (at most
  six provider requests and one hourly check), stored as native outcomes
  (supported, unsupported or failed) against the connection's current key and
  Base URL. A model never verified, verified on an older key or Base URL, or
  with no usable API runs on nothing; an advanced manual declaration of the
  APIs is the only override. A runtime runs on its own API when verified for
  that model (Chat Completions: OpenCode, Kilo, pi; Responses: Codex;
  Anthropic Messages: Claude Code). Otherwise only Codex and Claude Code may
  run on a model verified for Chat Completions, through a member-private
  translator: the CLI is exec'd behind a launcher that forks a
  loopback service as the same member UID, bound to that one upstream, key
  and model, answering only a platform-issued token scoped to the owner,
  member, connection revision, client protocol and model (masked by the
  runner like the key; a sibling's token is refused)
  and dying with the CLI. The CLI never holds the key on that path, and the
  service refuses, rather than drops, what Chat Completions cannot carry
  (an image a tool returned goes to the upstream as a following user image
  part). No other pair is translated, and nothing falls back to the Gateway,
  another model or another API at inference time; the composer offers, and
  every gate accepts, the same per-model paths. A claim and its residency must
  agree on both the credential revision and the plan. See the
  [protocol compatibility design](docs/agents/specs/2026-10-28-model-protocol-compatibility.zh-CN.md).
  Changing the key or Base URL clears every model's verification; that, a
  change to the manual declaration, disabling or deleting closes every process
  still running on the old state, while a rename or a model-list edit closes
  nothing and a newly listed model starts unverified. A verification is kept
  only if the key, Base URL and enabled state are still what it tested and
  the model is still listed, merged per model. The control plane's verification dials only public IPv4 HTTPS under
  the MCP connection policy and the sandbox's protected ranges, pinned, with no
  redirects and no upstream body echoed. See the
  [model connection design](docs/agents/specs/2026-10-27-user-model-connections.zh-CN.md).
- Public multi-tenant use requires Kata isolation, non-root, dropped
  capabilities, seccomp, and resource limits — plain Docker containers are for
  trusted internal repositories only.
- The Docker socket is the host's authority. Only the runner and the workspace
  broker mount it, and `:ro` on that mount does not narrow the Docker API:
  either can start a privileged container, so a flaw in either is a host
  compromise. No other service may mount it; the deployment tests and CI's
  Compose check pin the holders.
