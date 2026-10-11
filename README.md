# Cloud Agent

Cloud Agent runs coding agents against a user's repository inside retained
sandbox containers. Users work from a web interface, while enrolled hosts run
Claude Code, Codex, pi, OpenCode, or Kilo. The control plane coordinates jobs,
sessions, and publication to source control.

Cloud Agent gets model access and account identity from a
[HybridInference gateway](https://github.com/HarvardMadSys/hybridInference).
FreeInference is a hosted deployment of that gateway. Cloud Agent remains a
separate service, with its own database and credentials.

When writing a new task or follow-up, select **Rewrite prompt** to expand your
draft with the selected model. If you select **Ask clarification questions
first**, the model asks a few questions before rewriting your prompt. The result
stays in the editor, where you can review and edit it before sending. Canceling
the rewrite, or an unsuccessful rewrite, leaves the original draft unchanged.
Rewrites use the account's normal model permissions and inference quota.

To receive desktop alerts when an agent asks a question or a turn finishes,
open **Settings → Notifications**, select **Enable notifications**, and allow
the browser permission prompt. You can toggle question and turn-completion
alerts separately. While the app is open, alerts cover all your chats,
including chats you are not currently viewing. Clicking an alert opens the
relevant turn. Notification preferences are stored separately for each
account. Alerts are not delivered while the browser is closed.

## Getting started

Choose the path that matches what you want to do:

| Goal | Start here |
|---|---|
| Run the browser, API, host, and sandboxes locally | [Local Docker stack](deploy/local/README.md) |
| Change the backend or frontend | [Developer guide](docs/development.md) |
| Operate a self-hosted deployment | [Operations runbook](docs/operations.md) |
| Build or deploy a release | [Release guide](deploy/release/README.md) |
| Find configuration or a feature guide | [Documentation index](docs/README.md) |

### Backend development

Install Python 3.12 and [uv](https://github.com/astral-sh/uv), then run the
following commands from the repository root:

```bash
make setup-dev
make check
```

The package supports Python 3.12 and 3.13. Setup and CI use Python 3.12 by
default. `make check` formats the backend, checks lint and types, and runs tests
not marked `external` or `dbtest`. Frontend development uses Node.js 22 and
npm; its checks are documented separately in the
[developer guide](docs/development.md#run-the-relevant-checks).

| Command | Purpose |
|---|---|
| `make format` | Apply Ruff formatting and automatic lint fixes |
| `make lint` | Check formatting, Ruff rules, and Python docstrings |
| `make typecheck` | Run mypy across all three backend packages |
| `make test` | Run pytest with four workers, excluding `external` and `dbtest` |
| `make test-db` | Run database-backed tests against a dedicated test database |

### Complete local Docker run

To run the complete local stack, install Docker 28+, Docker Compose, `openssl`,
`curl`, and Python 3. Then run:

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

Open <http://127.0.0.1:13002>. The stack includes the web app, OAuth flow,
control plane, database, an enrolled host, and all five pinned Agent CLIs. A
local gateway fixture supplies deterministic inference responses, so you do not
need a staging gateway or provider key. This setup verifies integration and
session lifecycle. It does not measure model quality or qualify production
isolation.

Run `./deploy/local/dev.sh stop` to stop the stack while preserving its database
and sandbox state for the next startup. See the
[local guide](deploy/local/README.md) for prerequisites, configuration, logs,
and instructions for running multiple stacks.

Administrators can manage application behavior, integrations and credentials,
web appearance, and sandbox/workspace limits from **Admin → Settings**. Saved
values survive restarts. Existing environment values are imported once, and
Reset restores the built-in defaults. Database access, identity, encryption,
security, and deployment infrastructure remain bootstrap configuration. The
[Settings runbook](docs/operations.md#admin-settings) explains migration and key
setup.

**Admin → Database** opens the optional pgAdmin website for PostgreSQL
administration. The first administrator chooses the pgAdmin login from the
dashboard. Follow the [pgAdmin runbook](docs/operations.md#pgadmin) to enable
the private service.

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

The web app, control plane, and host pool are deployed separately. Each host
runs the runner, workspace broker, group supervisor, and the tunnels needed to
reach the control plane and relays. A host enrolls once, then reports its
capacity and capabilities through heartbeats.

A **job** records a user task. An **attempt** records one execution of that task
on one host. Every attempt carries a lease and fencing generation. If an attempt
is superseded, the store rejects its writes, including stale publication
results.

### Threads use retained native sessions

Starting a new chat creates a retained container and a sandbox group with one
member. The container's writable layer holds both the workspace and native CLI
state. Compatible follow-ups go to that member's live CLI process. If the
process must be replaced, the CLI resumes its native session inside the same
container.

Fork adds another chat and native session to the same sandbox group. The parent
and child share the workspace and can run at the same time, but each has its own
member UID, credentials, and process control.

After a chat with a draft PR finishes, select **Review** beside **Open draft
PR** to start an independent code-review subagent in a workspace tab. The
review covers every change since the conversation's original base, including
current workspace changes. The reviewer is instructed to report findings
without editing files. Selecting Review again reopens that chat's existing
review. Review needs an available native Fork; it cannot start from an archived
chat or a chat whose context has been lost.

Select **Watch PR and merge** beside a published PR to start a follow-up in the
same chat. The follow-up monitors tests and reviews, addresses failures and
feedback, and merges once the current PR head satisfies its required checks and
review requirements. Starting it preserves unsent composer text and
attachments. Before watching can begin, every chat that shares the workspace
must have no active work or waiting messages.

The agent needs source-control tools that can read checks and reviews, mark
drafts ready, and merge. The supplied GitHub MCP template is read-only, so
merging requires a writable connection. The agent reports missing access and
other blockers. Watching is limited by the agent turn's timeout. If the agent
makes fixes, the turn ends so the platform can publish them. After publication,
select the button again to continue watching.

| Context status | Meaning |
|---|---|
| `NEW` | No native turn has previously settled for this member |
| `LIVE` | The turn uses the compatible resident CLI process |
| `RESTORED` | A replacement process resumes the retained native session |
| `LOST` | Required native state or container integrity cannot be established; execution is refused |

Stopping a container preserves its writable layer. Losing the container or the
Docker data root also loses the required native state. Transcript replay and
cross-host restoration are not recovery paths. See
[native conversation recall](docs/context-recall.md) for verification details
and limits.

## Powered by a HybridInference gateway

Cloud Agent and the gateway communicate only over HTTP. Cloud Agent does not
share the gateway's database, Python modules, or signing secrets. The contracts
it consumes are documented in
[gateway_contracts/](gateway_contracts/README.md):

| Contract | Responsibility |
|---|---|
| Identity | Exchange login codes and verify gateway-signed identity using public keys |
| Inference grants | Mint, renew, revoke, and read usage for scoped inference grants |
| Internal lookups | Read the user's model catalog and deployment-owned Agent access decision |

The gateway owns account permissions and inference quotas. An inference grant
limits which models an attempt can call; there is no per-task dollar budget. The
control plane renews grants while their attempt or retained member remains
eligible. Revocation can end access sooner, while expiry remains the backstop if
revocation fails.

Protected owner and admin requests check the gateway's current Agent access
decision. An unavailable or invalid gateway response never grants access. Each
Agent database is bound to a single gateway identity issuer. See the
[admission policy](docs/agent-access-policy.md) and
[identity configuration](docs/minimal-identity.md) for details.

MCP connections belong to individual users and are resolved by the control
plane. Sandboxes call tools through the MCP-only listener with a separate,
scoped credential. Inference grants provide no MCP or platform-management
authority.

## Sandbox isolation

The host deployment defaults to `kata`, which gives each sandbox group a
separate guest kernel. Public multi-tenant use requires Kata isolation,
non-root execution, dropped capabilities, seccomp, and resource limits. The
`container` backend is only for trusted internal repositories, and `process`
is a unit-test seam.

Each group uses one network for both setup and agent execution. Host nftables
rules allow public IPv4 on TCP 80/443, one configured DNS resolver, and that
group's inference and MCP relays. The rules block host addresses, private and
special-purpose ranges, other groups, other ports, and IPv6. If network
verification fails, the group does not start.

Long-lived account and service credentials stay outside the sandbox. However,
short-lived inference grants can still be exfiltrated and replayed through the
gateway's public inference API until they expire or are revoked. Those calls
remain subject to the owner's model permissions and quota. Before deployment,
read the [security guidance](SECURITY.md) and the
[operations runbook](docs/operations.md#the-sandbox-network).

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

Start with the [developer guide](docs/development.md),
[contribution workflow](CONTRIBUTING.md), and [repository rules](AGENTS.md).
Create branches from `dev` using `<user>/<scope>/<name>`, and target pull
requests at `dev`. Before opening a PR, run `make check` along with any frontend
or database checks relevant to your change.

The extraction from HybridInference was completed in August 2026.
[MIGRATION.md](MIGRATION.md) records its decisions and deferred work. Current
work is tracked in this repository's issues and pull requests.

## License

[MIT](LICENSE). The code retains the HybridInference project's copyright and
license. See [license and provenance](MIGRATION.md#licence-and-provenance).
