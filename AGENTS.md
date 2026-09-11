# AGENTS.md

Onboarding guide for anyone working in this repository — human or AI coding
agent. Read this first.

## 1. What this is

Cloud Agent runs coding agents (Claude Code, Codex, pi, OpenCode, Kilo) in
sandboxed containers against a user's repository and publishes results as pull
requests.

The extraction from the HybridInference monorepo
(`HarvardMadSys/hybridInference`) is complete: production cut over on
2026-08-07 and the gateway's own agent code was deleted on 2026-08-09.
[MIGRATION.md](MIGRATION.md) records the split, its decisions, and deferred
work. The extraction plan in the HybridInference repo
(`docs/agents/plans/2026-08-03-cloud-agent-repo-split.md`) is historical; new
work tracks in this repository's issues and pull requests.

## 2. Repo map

```text
cloud_agent/            control plane (FastAPI): API, auth adapter, scheduler,
                        source-control integrations, publishing, storage
cloud_agent_host/       host agent: runner, sandbox, runtimes, workspace broker
web/                    Next.js user UI + admin dashboard
contracts/              OpenAPI mirrors of the gateway contracts we consume
migrations/             Alembic migrations (agent-owned database only)
deploy/{control-plane,host,sandbox}/
tests/{unit,servers,integration,external}/  # plus fixtures/ and support/
docs/
```

Not every module is worth a line here; these are the ones a newcomer needs to
find early. The control plane's database schema is **one baseline revision plus
additive ones** (`migrations/versions/0001_schema_baseline.py` and on), not a
long chain — `MIGRATION.md` § *Schema baseline rewrite* explains why, and
`docs/operations.md` § *Rebuilding the agent database* is the runbook for a
database stamped with the chain that predates it.

| Module | What it owns |
|---|---|
| `cloud_agent/app.py` | The two listeners: the main API and the MCP-only one |
| `cloud_agent/api/` | One router module per surface — jobs, hosts, admin, session, MCP relay |
| `cloud_agent/storage/store.py` | The Postgres store: claims, fences, residency, publication |
| `cloud_agent/grants.py`, `ledger.py`, `grant_usage.py` | Gateway inference grants, and the usage snapshots read back from them |
| `cloud_agent/gateway.py`, `agent_access.py`, `identity.py` | The three gateway contracts: lookups, admission, identity |
| `cloud_agent/identity_namespace.py` | Which Gateway minted the `user_id`s this database is keyed by |
| `cloud_agent/mcp/`, `api/mcp_relay.py`, `mcp_proxy.py` | Per-user MCP connections, the protocol host that drives one connection generically, and the relay that fronts them |
| `cloud_agent/publisher.py`, `publish_worker.py`, `source_control.py` | Branch and draft-PR publication, and the GitHub/GitLab credentials behind it |
| `cloud_agent/admin_observability.py` | The cross-user admin reading surface and its audit |
| `cloud_agent_host/enrollment.py` | Host registration and the persisted host credential |
| `cloud_agent_host/runner.py` | Claims, executes one turn, heartbeats, drains on SIGTERM |
| `cloud_agent_host/chat_sandbox.py`, `sandbox.py` | Retained group lifecycle and the Docker/Kata boundary |
| `cloud_agent_host/egress.py`, `egress_proxy.py` | The two egress phases and the setup allowlist proxy |
| `cloud_agent_host/runtimes.py`, `shared_protocol.py` | The five Agent CLI adapters and the version pins they attest |

## 3. Quality gates

```bash
make setup-dev   # uv venv + uv sync
make check       # format + lint + test — must be green before every PR
```

`make test` runs everything except the `external` and `dbtest` markers.
Database-backed tests are opt-in: `make test-db` (needs Postgres; see
[ENVIRONMENT.md](ENVIRONMENT.md)).

## 4. Workflow

- Branch off `dev` as `<user>/<scope>/<name>`. Pull requests target `dev`.
- Keep each pull request focused on one feature, fix, or documentation change.
- Never commit secrets. This repository will be made public; treat every commit
  as already public.

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
   `contracts/`.

## 6. Key concepts

- **Job / attempt / lease.** A job is the user's task; an attempt is one
  execution of it on one host. Attempts carry a lease with a fencing
  generation — a superseded attempt's writes are rejected by the store, which
  is what makes retries safe and stops duplicate pull requests.
- **Grant.** A short-lived, per-attempt capability token minted by the gateway,
  scoped to a user, a job, and a model allowlist. It authorizes inference and is
  revoked when the attempt is superseded. MCP servers are resolved per user by
  the control plane and reached through its MCP-only relay with a separate
  scoped credential; they are no longer named in the inference grant.
  **There is no per-task dollar budget** — that design was considered and
  rejected. All inference spend stays subject to the gateway's existing
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
  [session design](docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md).
  The group has **two egress phases**, enforced by the
  code in `cloud_agent_host/egress.py` and `egress_proxy.py`:
  - *Setup phase* — dependency installation may reach package registries, but
    only through the Squid allowlist proxy, which permits CONNECT to an
    allowlisted host by name and refuses everything else.
  - *Agent phase* — untrusted repository code runs, and the sandbox reaches
    only two fixed-address relays: the gateway for inference and the MCP-only
    listener for tools. Each sandbox group gets its own internal Docker
    network; the compose network is only the discovery backplane from which
    those two trusted relay endpoints are attached. Neither relay is a
    caller-selected forward proxy, and unrelated sandbox groups never share
    a bridge.

## 7. Security boundaries — do not relax without review

- During the agent phase the sandbox reaches exactly two network destinations:
  the inference gateway relay and the structurally isolated MCP-only relay.
  Per-group internal networks enforce both the off-host boundary and cross-group
  isolation; `internal: true` on one shared bridge is not sufficient. The
  setup-phase allowlist proxy is the only sanctioned widening, and the
  escape hatches (`AGENT_SANDBOX_ALLOW_OPEN_NETWORK`,
  `AGENT_EGRESS_AGENT_TIER=custom`) must stay unset anywhere untrusted
  repositories run.
- Long-lived account and service credentials never enter the sandbox. Each
  member receives its own scoped inference and MCP credentials; sibling
  members cannot read or reuse them. Spend remains governed by the gateway's
  per-user quota.
- Public multi-tenant use requires Kata isolation, non-root, dropped
  capabilities, seccomp, and resource limits — plain Docker containers are for
  trusted internal repositories only.
