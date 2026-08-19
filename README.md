# FreeInference Cloud Agent

Cloud coding agents that run in sandboxed containers on a pool of hosts, drive a
user's repository, and publish the result as a pull request.

## Powered by FreeInference

Model calls go through the [FreeInference](https://freeinference.org) gateway,
and **HTTP is the only thing these two services share** — no database, no
signing secret, no Python import. Three contracts, each written down in
[`contracts/`](contracts/):

| Contract | What it answers | Why it cannot live here |
|---|---|---|
| **Identity** | Who this user is | The gateway owns the accounts. It signs with a private key and publishes the public half, so this service verifies without holding anything it could also mint with. |
| **Inference grants** | Which models one attempt may call, for the next few minutes | Only the gateway knows what a role may reach — and it is the gateway that meters the spend. |
| **Internal lookups** | A user's model catalog, and whether an account is still active | Two reads that used to be imports. |

A grant says *what*, never *how much*: spend is governed by the account's daily
quota at the gateway, and there is deliberately no per-task budget here. Grants
live minutes and are renewed while an attempt is genuinely still running, so
revocation is expiry rather than bookkeeping — and this service, not the
gateway, is what decides an attempt is still current.

MCP is in neither contract. A grant carried an MCP scope, and the lookups
carried a registry read, until server ownership moved to the user: each person
connects their own servers and the control plane resolves them per user, so
there is no deployment-wide registry left for the gateway to answer for.

Every call to the gateway fails **closed**. An unreachable catalog resolves to
no models, not every model; an unreadable user status resolves to "cannot use
the service", not "fine".

> **Status: serving production.** The extraction from the HybridInference
> monorepo is complete — production cut over on 2026-08-07 and the gateway's own
> agent routes were deleted on 2026-08-09, so this repository is now the only
> implementation. [MIGRATION.md](MIGRATION.md) records what moved, what was
> decided, and which paths are still unproven on this stack.

## Architecture

```text
                 ┌──────────────────────────────────────┐
   browser ─────▶│  web/  (Next.js + BFF)               │
                 └──────────────────┬───────────────────┘
                                    │ session cookie
                 ┌──────────────────▼───────────────────┐
                 │  backend/cloud_agent — control plane │◀──┐
                 │  jobs · scheduler · publishing       │   │
                 │  thread residency · affinity claim   │   │  identity JWT (JWKS)
                 │  ThreadCheckpoint store · MCP relay  │   │  inference grants
                 └──────────────────┬───────────────────┘   │  internal lookups
                                    │ claim / heartbeat     │
                 ┌──────────────────▼───────────────────┐   │
                 │  host/cloud_agent_host — host agent  │   │  ┌─────────────────┐
                 │  runner · workspace broker · relay   │   └─▶│  FreeInference  │
                 │  HarnessDriver / Session / Turn      │      │  gateway        │
                 │  residency heartbeat (gen-scoped)    │      └────────▲────────┘
                 └──────────────────┬───────────────────┘               │
                                    │ spawns                            │
                 ┌──────────────────▼───────────────────┐               │
                 │  sandbox — process | container | kata│───────────────┘
                 │  claude-code · codex · pi ·          │  scoped grant token, through
                 │  opencode · kilo                     │  the egress relay
                 └──────────────────────────────────────┘
```

The sandbox backend is chosen per host. `kata` gives each job its own kernel and
is what public multi-tenant use requires; `container` is an explicit
shared-kernel downgrade that every deploy restates; `process` is no isolation at
all and refuses to start unless the operator acknowledges that. Whichever one
runs, the harness inside it runs with its permission prompts disabled — the
sandbox is the boundary, so a prompt inside it has nothing left to protect.
[docs/operations.md](docs/operations.md) covers provisioning and the checks that
gate each backend.

### Threads are resident sessions

A thread is not a transcript replayed into a fresh process. The harness session
stays resident on a host between turns, and each turn takes the highest rung
still available:

| Rung | What the turn does | Needs |
|---|---|---|
| `hot` | injected into the live process on its existing session | a process that predates this turn |
| `warm_native` | resident workspace, harness restarted with its own `--resume` | the residency survived, the process did not |
| `cold_native` | fresh sandbox, `ThreadCheckpoint` imported | a checkpoint was exported |
| `replay` | fresh sandbox, transcript replayed as a prompt | always available |

Degradation is automatic and observable: each step triggers only when the one
above it is unavailable, and every attempt records which rung it ran on. The
design — residency generation fencing, the driver contract, and the credential
model — is in
[`docs/specs/2026-08-17-session-resident-harness-threads-design.md`](docs/specs/2026-08-17-session-resident-harness-threads-design.md).

Three deployment units, one repository:

| Unit | Path | Runs where |
|---|---|---|
| Web + BFF | `web/` | alongside the control plane |
| Control plane | `backend/cloud_agent/` | one service, owns the Postgres database |
| Host agent | `host/cloud_agent_host/` | every machine in the host pool; the runner, the workspace broker, the egress proxy and the gateway/control-plane/MCP tunnels deploy together |

## Repository layout

```text
backend/cloud_agent/   control plane: API, auth adapter, scheduler, publishing
host/cloud_agent_host/ runner, sandbox, runtimes, workspace broker
web/                   Next.js app (user UI + admin dashboard)
contracts/             OpenAPI documents for the three gateway contracts
migrations/            Alembic migrations for the agent-owned database
deploy/                control-plane / host / sandbox deployment
tests/                 unit, servers, integration (dbtest), external (external)
docs/                  operations and design docs
```

## Getting started

Prerequisites: Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
make setup-dev
make check
```

`make check` = format + lint + test. Every change must leave it green.

| Command | What it does |
|---|---|
| `make format` | `ruff format` then `ruff check --fix` |
| `make lint` | ruff format check, ruff lint, pydocstyle |
| `make test` | pytest, excluding `external` and `dbtest` |
| `make test-db` | the `dbtest` tier (needs Postgres) |

## Contributing

Read [AGENTS.md](AGENTS.md) first — it is the onboarding guide for both humans
and AI coding agents, and it carries the migration rules that are currently in
force.

Branch off `dev` as `<user>/<scope>/<name>`; pull requests target `dev`.

## License

[MIT](LICENSE) — the same licence, and the same copyright holder, as the
HybridInference repository this code is extracted from. See
[MIGRATION.md](MIGRATION.md#licence-and-provenance).
