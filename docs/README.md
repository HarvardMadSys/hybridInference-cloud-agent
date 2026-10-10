# Documentation

Start with the [project README](../README.md) for a product overview and local
quickstart. This index groups the maintained guides by task. Commands in linked
guides run from the repository root unless the guide says otherwise.

## Develop and test

| Guide | Use it to |
|---|---|
| [Developer guide](development.md) | Set up a checkout, find the code that owns a change, and run checks |
| [Contributing](../CONTRIBUTING.md) | Follow branch, pull-request, and CI requirements |
| [Repository rules](../AGENTS.md) | Understand package boundaries, execution concepts, and security requirements |
| [Local Docker stack](../deploy/local/README.md) | Run the browser, API, gateway fixture, hosts, and retained sandboxes locally |
| [Local repository fixture](../deploy/local/fixture/README.md) | Understand the repository used by local smoke checks |
| [Gateway contracts](../gateway_contracts/README.md) | Review consumed gateway APIs and the published control-plane API snapshot |

## Configure, deploy, and operate

| Guide | Use it to |
|---|---|
| [Environment inventory](../ENVIRONMENT.md) | Find configuration names, defaults, ownership, and provisioning requirements |
| [Operations runbook](operations.md) | Configure credentials, enroll hosts, qualify Kata, deploy, migrate databases, and manage storage and networking |
| [Release guide](../deploy/release/README.md) | Build immutable release artifacts and deploy their recipes in the required order |
| [Compute resources](operations-compute.md) | Configure optional server provisioning, global sandbox placement, and CloudLab node replacement |
| [Deployment reliability](deployment-reliability.md) | Drain runners safely, check schema compatibility, and detect stalled queues |
| [Runner lease troubleshooting](runner-lease-troubleshooting.md) | Diagnose expired attempts and repeated lease failures |
| [Single-host smoke](smoke.md) | Verify a deployment against a gateway and review the original smoke evidence |
| [Security](../SECURITY.md) | Review deployment trust assumptions and report vulnerabilities privately |

## Identity and user-facing behavior

| Guide | Use it to |
|---|---|
| [Agent admission policy](agent-access-policy.md) | Understand permissions, gateway checks, refusals, and compatible rollout |
| [Minimal Agent identity](minimal-identity.md) | Configure the gateway issuer and safely bind an existing database to it |
| [Native conversation recall](context-recall.md) | Understand retained sessions, runtime-specific behavior, and recall verification |
| [Native agent questions](operations.md#native-agent-questions) | Answer a waiting agent request and understand reload, skip, and stale-answer handling |
| [Conversation navigation](conversation-navigation.md) | Understand the turn rail, previews, jumps, and reading position |
| [Sidebar navigation](sidebar-navigation.md) | Understand project grouping and conversation actions |

## Designs and historical evidence

[specs/](specs/) contains dated designs and reviews.
[acceptance/](acceptance/) contains verification records for their stated
revisions and environments. Some maintained guides also retain dated test logs;
those logs describe the recorded run and do not establish that a current
deployment has passed the same checks.

Useful design entry points include:

- [Native sessions, Fork, and shared workspaces](specs/2026-08-31-native-session-fork-shared-workspace-design.md)
- [Sandbox security model](specs/2026-09-01-agent-sandbox-security-model.zh.md)
- [Public egress and accepted inference-grant replay risk](specs/2026-09-23-agent-public-egress-design.zh-CN.md)
- [Per-user MCP conformance](specs/2026-09-11-per-user-mcp-v1-conformance.md)
- [Compute resource lifecycle](specs/2026-10-04-compute-resource-lifecycle-design.zh-CN.md)
- [CloudLab node rotation](specs/2026-10-10-cloudlab-node-rotation-design.zh-CN.md)

The completed repository split is recorded in [MIGRATION.md](../MIGRATION.md),
[cutover.md](cutover.md), and [gateway-removal.md](gateway-removal.md).
[plans/](plans/) preserves scoped work plans. Use maintained guides and current
source for present-day setup, and consult dated records for the decisions and
evidence behind them.

## Keep documentation current

Update a maintained guide in the same change that alters its behavior,
configuration, or commands. Verify examples against the owning script or source,
keep relative links and section anchors working, and distinguish a proposed
design from implemented behavior. Preserve dated design and acceptance records
as evidence of their original baseline.
