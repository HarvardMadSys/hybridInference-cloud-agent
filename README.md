# Cloud Agent

Cloud coding agents that run in sandboxed containers on a pool of hosts, drive a
user's repository, and publish the result as a pull request.

## Powered by a HybridInference gateway

Model calls go through a [HybridInference](https://github.com/HarvardMadSys/hybridInference) gateway
(FreeInference is the hosted deployment of one),
and **HTTP is the only thing these two services share** — no database, no
signing secret, no Python import. Three contracts, each written down in
[`gateway_contracts/`](gateway_contracts/):

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

MCP servers are owned by each user and resolved from their connections by the
control plane. They are no longer part of the gateway's grants or internal
lookups. The sandbox calls the control plane's MCP-only relay with a separate,
scoped credential.

Every call to the gateway fails **closed**. An unreachable catalog resolves to
no models, not every model; an unreadable user status resolves to "cannot use
the service", not "fine".

> **Status: serving production.** The extraction from the HybridInference
> monorepo is complete: production cut over on 2026-08-07 and the gateway's own
> agent routes were deleted on 2026-08-09. [MIGRATION.md](MIGRATION.md) records
> the split, its decisions, and deferred work. New work tracks in this
> repository's issues and pull requests.

## Architecture

```text
browser ──▶ apps/frontend/ (Next.js + BFF)
                 │ session cookie
                 ▼
         cloud_agent/ — control plane ◀── HTTP ──▶ HybridInference gateway
         jobs · scheduling · publishing           identity · grants · lookups
                 │ claim / heartbeat / workspace
                 ▼
         cloud_agent_host/ — enrolled host
         runner · workspace broker · group supervisor
                 │ Docker lifecycle / per-member live transports
                 ▼
         retained sandbox group — kata | container
         one Docker container · shared /workspace
         one native session and active CLI process per chat member
         claude-code · codex · pi · opencode · kilo
                 │ scoped credentials over fixed relays
                 ├──▶ inference gateway
                 └──▶ control plane's MCP-only listener
```

The deployment defaults to `kata`, which provides a separate guest kernel per
sandbox group and is required for public multi-tenant use. `container` is an
explicit shared-kernel downgrade for trusted repositories. The unisolated
`process` backend remains a unit-test seam and cannot attest the deployed
retained-session topology. See [docs/operations.md](docs/operations.md) for
execution profiles and deployment checks.

### Threads use retained native sessions

A new chat starts in a one-member sandbox group. The container's writable layer
holds the workspace and native Agent CLI state. The broker's group supervisor
keeps a live CLI process per active member; a compatible follow-up sends only
the new message to that process. If the process must be replaced, the CLI
resumes its native session in the same retained container.

Fork creates a separate chat and native session in the same group. Parent and
child share the container and workspace and may run concurrently, with separate
member UIDs, credentials, and process control.

| Context status | Meaning |
|---|---|
| `NEW` | The member has no previously settled native turn. |
| `LIVE` | The turn uses the compatible resident process already running. |
| `RESTORED` | A replacement process resumes the same native session from the retained container. |
| `LOST` | The required native state or container cannot be proved intact; execution fails closed. |

`docker stop` preserves the writable layer for a later `docker start`. Losing
the container or Docker data root is not recoverable through transcript replay
or a platform checkpoint; portable storage and cross-host restore are deferred.
The implemented design is
[`docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md`](docs/specs/2026-08-31-native-session-fork-shared-workspace-design.md).

Three deployment units, one repository:

| Unit | Path | Runs where |
|---|---|---|
| Web + BFF | `apps/frontend/` | alongside the control plane |
| Control plane | `cloud_agent/` | one service, owns the Postgres database |
| Host agent | `cloud_agent_host/` | every enrolled host; runner, workspace broker, egress proxy, and gateway/control-plane/MCP tunnels deploy together |

## Repository layout

```text
apps/backend/          the three Python packages: control plane, host agent,
                       and the shared vocabulary both images ship
apps/frontend/         Next.js app (user UI + admin dashboard)
gateway_contracts/     OpenAPI documents for the three gateway contracts
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
and AI coding agents, including the current workflow and permanent service
boundaries.

Branch off `dev` as `<user>/<scope>/<name>`; pull requests target `dev`.

## License

[MIT](LICENSE) — the same licence, and the same copyright holder, as the
HybridInference repository this code is extracted from. See
[MIGRATION.md](MIGRATION.md#licence-and-provenance).
