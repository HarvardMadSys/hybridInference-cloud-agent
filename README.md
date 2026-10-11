# Cloud Agent

Cloud Agent runs coding agents against a user's repository in retained sandbox
containers. Users work through a web interface; enrolled hosts run Claude Code,
Codex, pi, OpenCode, or Kilo; the control plane manages jobs, sessions, and
publication to source control.

Model access and account identity come from a
[HybridInference gateway](https://github.com/HarvardMadSys/hybridInference).
FreeInference is a hosted deployment of that gateway. Cloud Agent is a separate
service with its own database and credentials.

## Getting started

Choose a path based on what you want to do:

| Goal | Start here |
|---|---|
| Run the browser, API, host, and sandboxes locally | [Local Docker stack](deploy/local/README.md) |
| Change the backend or frontend | [Developer guide](docs/development.md) |
| Operate a self-hosted deployment | [Operations runbook](docs/operations.md) |
| Build or deploy a release | [Release guide](deploy/release/README.md) |
| Find configuration or a feature guide | [Documentation index](docs/README.md) |

### Backend development

Install Python 3.12 and [uv](https://github.com/astral-sh/uv), then run these
commands from the repository root:

```bash
make setup-dev
make check
```

The package supports Python 3.12 and 3.13; setup and CI use 3.12 by default.
`make check` formats the backend, checks lint and types, and runs tests that
require neither an external deployment nor a test database. Frontend development
uses Node.js 22 and npm, with separate checks described in the
[developer guide](docs/development.md#run-the-relevant-checks).

| Command | Purpose |
|---|---|
| `make format` | Apply Ruff formatting and automatic lint fixes |
| `make lint` | Check formatting, Ruff rules, and Python docstrings |
| `make typecheck` | Run mypy across all three backend packages |
| `make test` | Run pytest excluding `external` and `dbtest` |
| `make test-db` | Run database-backed tests against a dedicated test database |

### Complete local Docker run

Install Docker 28+, Docker Compose, `openssl`, `curl`, and Python 3, then run:

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

Open <http://127.0.0.1:13002>. This stack includes the web app, OAuth flow,
control plane, database, enrolled host, and all five pinned Agent CLIs. A local
gateway fixture provides deterministic inference responses, so no staging
gateway or provider key is needed. This verifies integration and session
lifecycle; it does not measure model quality or qualify production isolation.

`./deploy/local/dev.sh stop` preserves database and sandbox state for later
startup. Read the [local guide](deploy/local/README.md) for prerequisites,
configuration, logs, and running multiple stacks.

Administrators manage application behavior, integrations and credentials, web
appearance, and sandbox/workspace limits in **Admin → Settings**. Saved values
survive restarts; existing environment values are imported once, and Reset
restores built-in defaults. Database access, identity, encryption, security and
deployment infrastructure remain bootstrap configuration. See the
[Settings runbook](docs/operations.md#admin-settings) for migration and key setup.

**Admin → Database** opens the optional pgAdmin website for PostgreSQL
administration. Enable the private service and its separate login using the
[pgAdmin runbook](docs/operations.md#pgadmin).

## Architecture

```mermaid
flowchart TD
    Browser[Browser] --> Web[Next.js web app and BFF]
    Web --> Control[Control plane: API, scheduling, publication]
    Control --> Database[(Agent-owned PostgreSQL)]
    Control <-->|HTTP contracts| Gateway[HybridInference gateway]
    Host[Enrolled host: runner and workspace broker] <-->|Claims, heartbeats, workspace| Control
    Host -->|Container lifecycle| Sandbox[Retained sandbox group: shared workspace, native CLI sessions]
    Sandbox -->|Scoped inference credential via relay| Gateway
    Sandbox -->|Scoped tool credential via relay| MCP[Control plane MCP-only listener]
    Sandbox -->|TCP 80 and 443 under host rules| Public[Public IPv4 destinations]
```

The web app, control plane, and host pool deploy separately. Each host runs the
runner, workspace broker, group supervisor, and the tunnels needed to reach the
control plane and relays. Hosts enroll once, then advertise their capacity and
capabilities through heartbeats.

A **job** records a task; an **attempt** records one execution on a host. Attempts
carry leases and fencing generations. The store rejects writes from a
superseded attempt, including stale publication results.

### Threads use retained native sessions

A new chat creates a one-member sandbox group with a retained container. Its
writable layer holds the workspace and native CLI state. Compatible follow-ups
send the new message to the member's live CLI process. When that process needs
replacement, the CLI resumes its native session in the same container.

Fork creates another chat and native session in the same group. Parent and child
share the workspace and can run concurrently, with separate member UIDs,
credentials, and process control.

For a completed chat with a draft PR, **Review** beside **Open draft PR** starts
an independent code-review subagent in a workspace tab. It reviews all changes
from the conversation's original base, including current workspace changes,
and is instructed to return findings without editing files. Clicking Review
again reopens that chat's review. Review requires an available native Fork;
archived chats and chats with lost context cannot start one.

| Context status | Meaning |
|---|---|
| `NEW` | No native turn has previously settled for this member |
| `LIVE` | The turn uses the compatible resident CLI process |
| `RESTORED` | A replacement process resumes the retained native session |
| `LOST` | Required native state or container integrity cannot be established; execution is refused |

Stopping a container preserves its writable layer. Losing the container or
Docker data root loses the required native state; transcript replay and
cross-host restoration are not recovery paths. See
[native conversation recall](docs/context-recall.md) for verification and limits.

## Powered by a HybridInference gateway

The services communicate over HTTP. Cloud Agent does not share the gateway's
database, Python modules, or signing secrets. The consumed contracts are
documented in [gateway_contracts/](gateway_contracts/README.md):

| Contract | Responsibility |
|---|---|
| Identity | Exchange login codes and verify gateway-signed identity using public keys |
| Inference grants | Mint, renew, revoke, and read usage for scoped inference grants |
| Internal lookups | Read the user's model catalog and deployment-owned Agent access decision |

The gateway owns account permissions and inference quotas. A grant limits the
models an attempt may call; there is no per-task dollar budget. The control
plane renews grants while their attempt or retained member remains eligible.
Revocation ends access sooner; expiry is the backstop if revocation fails.

Protected owner and admin requests check the gateway's current Agent access
decision. Unavailable or invalid gateway responses do not grant access. Each
Agent database is bound to one gateway identity issuer; see
[admission policy](docs/agent-access-policy.md) and
[identity configuration](docs/minimal-identity.md).

MCP connections belong to users and are resolved by the control plane. Sandboxes
call tools through the MCP-only listener with a separate scoped credential;
inference grants grant no MCP or platform-management authority.

## Sandbox isolation

The host deployment defaults to `kata`, with a separate guest kernel per sandbox
group. Public multi-tenant use requires Kata isolation, non-root execution,
dropped capabilities, seccomp, and resource limits. The `container` backend is
for trusted internal repositories; `process` is a unit-test seam.

Every group has one network for setup and agent execution. Host nftables rules
allow public IPv4 on TCP 80/443, one configured DNS resolver, and the group's
inference and MCP relays. They block host addresses, private and special-purpose
ranges, other groups, other ports, and IPv6. Failed network verification prevents
the group from starting.

Long-lived account and service credentials stay outside the sandbox. Short-lived
inference grants can still be exfiltrated and replayed through the gateway's
public inference API until expiry or revocation, subject to the owner's model
permissions and quota. Read the [security guidance](SECURITY.md) and
[operations runbook](docs/operations.md#the-sandbox-network) before deployment.

## Repository layout

```text
apps/backend/cloud_agent/         FastAPI control plane and PostgreSQL storage
apps/backend/cloud_agent_host/    Host runner, sandboxes, workspace broker, CLI adapters
apps/backend/cloud_agent_shared/  Shared protocols, pins, errors, Git and archive helpers
apps/frontend/                    Next.js user interface and admin dashboard
gateway_contracts/                Gateway contract mirrors and control-plane API snapshot
migrations/                       Alembic revisions for the Agent database
deploy/                           Control-plane, host, sandbox, local, and release recipes
tests/                            Unit, API, integration, and external verification
docs/                             Feature guides, runbooks, designs, and acceptance records
```

## Contributing

Read the [developer guide](docs/development.md),
[contribution workflow](CONTRIBUTING.md), and [repository rules](AGENTS.md).
Branch from `dev` as `<user>/<scope>/<name>`; pull requests target `dev`.
Run `make check` before opening a PR, plus the relevant frontend or database
checks for the change.

The extraction from HybridInference completed in August 2026.
[MIGRATION.md](MIGRATION.md) preserves its decisions and deferred work. Current
work is tracked in this repository's issues and pull requests.

## License

[MIT](LICENSE). The code retains the HybridInference project's copyright and
license; see [licence and provenance](MIGRATION.md#licence-and-provenance).
