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
| **Inference grants** | Which models and MCP servers one attempt may call, for the next few minutes | Only the gateway knows what a role may reach — and it is the gateway that meters the spend. |
| **Internal lookups** | The MCP registry, a user's model catalog, whether an account is still active | Three reads that used to be imports. |

A grant says *what*, never *how much*: spend is governed by the account's daily
quota at the gateway, and there is deliberately no per-task budget here. Grants
live minutes and are renewed while an attempt is genuinely still running, so
revocation is expiry rather than bookkeeping — and this service, not the
gateway, is what decides an attempt is still current.

Every call to the gateway fails **closed**. An unreachable catalog resolves to
no models, not every model; an unreadable user status resolves to "cannot use
the service", not "fine".

> **Status: under construction.** The implementation is being extracted from the
> HybridInference monorepo. See [MIGRATION.md](MIGRATION.md) for what has landed
> and what has not.

## Architecture

```text
                 ┌─────────────────────────┐
   browser ─────▶│  web/  (Next.js + BFF)  │
                 └───────────┬─────────────┘
                             │ session cookie
                 ┌───────────▼─────────────┐      identity JWT (JWKS)
                 │  cloud_agent            │◀──── inference grants ────┐
                 │  control plane          │      grant usage          │
                 └───────────┬─────────────┘                           │
                             │ claim / heartbeat / terminal            │
                 ┌───────────▼─────────────┐                  ┌────────┴────────┐
                 │  cloud_agent_host       │                  │   FreeInference │
                 │  runner + broker        │                  │   gateway       │
                 └───────────┬─────────────┘                  └────────▲────────┘
                             │ spawns                                  │
                 ┌───────────▼─────────────┐   scoped grant token      │
                 │  sandbox (Kata/gVisor)  │───────────────────────────┘
                 │  codex / claude / kilo  │
                 └─────────────────────────┘
```

Three deployment units, one repository:

| Unit | Path | Runs where |
|---|---|---|
| Web + BFF | `web/` | alongside the control plane |
| Control plane | `cloud_agent/` | one service, owns the Postgres database |
| Host agent | `cloud_agent_host/` | every machine in the host pool |

## Repository layout

```text
cloud_agent/           control plane: API, auth adapter, scheduler, publishing
cloud_agent_host/      runner, sandbox, runtimes, workspace broker
web/                   Next.js app (user UI + admin dashboard)
contracts/             OpenAPI documents for the three gateway contracts
migrations/            Alembic migrations for the agent-owned database
deploy/                control-plane / host / sandbox deployment
tests/                 unit, servers, integration (dbtest marker)
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

### Complete local Docker run

To exercise the actual browser, OAuth boundary, control plane, enrolled host,
all five pinned Agent CLIs, and retained sandbox lifecycle without a staging
gateway or provider key:

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

Open <http://127.0.0.1:13002>. The stack publishes only that loopback edge;
repository, session, and database state remain Docker-owned. `dev.sh stop`
stops rather than deletes the stack, so the same chat containers can be
started and resumed later. See [deploy/local/README.md](deploy/local/README.md)
for the topology and commands.

## Contributing

Read [AGENTS.md](AGENTS.md) first — it is the onboarding guide for both humans
and AI coding agents, and it carries the migration rules that are currently in
force.

Branch off `dev` as `<user>/<scope>/<name>`; pull requests target `dev`.

## License

[MIT](LICENSE) — the same licence, and the same copyright holder, as the
HybridInference repository this code is extracted from. See
[MIGRATION.md](MIGRATION.md#licence-and-provenance).
