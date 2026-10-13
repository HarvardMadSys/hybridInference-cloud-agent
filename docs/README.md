# Documentation

Use this index to find the maintained guide for your task. If you are new to
Cloud Agent, start with the [project README](../README.md) for a product overview
and local quickstart. Run commands from the repository root unless a guide says
otherwise.

## Develop and test

| Guide | Use it to |
|---|---|
| [Developer guide](developer/development.md) | Set up a checkout, find the code that owns a change, and run checks |
| [Code structure](developer/code-structure.md) | Follow dependencies, locate domain modules, and extend the codebase |
| [Contributing](../CONTRIBUTING.md) | Follow branch, pull-request, and CI requirements |
| [Repository rules](../AGENTS.md) | Understand package boundaries, execution concepts, and security requirements |
| [Local Docker stack](../deploy/local/README.md) | Run the browser, API, gateway fixture, hosts, and retained sandboxes locally |
| [Local repository fixture](../deploy/local/fixture/README.md) | Understand the repository used by local smoke checks |
| [Gateway contracts](../gateway_contracts/README.md) | Review consumed gateway APIs and the published control-plane API snapshot |

## Configure, deploy, and operate

| Guide | Use it to |
|---|---|
| [Environment inventory](../ENVIRONMENT.md) | Look up configuration names, defaults, owning components, and provisioning requirements |
| [Admin Settings](developer/operations.md#admin-settings) | Manage saved settings, integrations, appearance, and runtime limits; initialize settings and preserve encryption keys |
| [Operations runbook](developer/operations.md) | Configure credentials, enroll hosts, qualify Kata, deploy, migrate databases, and manage storage and networking |
| [Release guide](../deploy/release/README.md) | Build immutable release artifacts and deploy their recipes in the required order |
| [Compute resources](developer/operations-compute.md) | Configure optional server provisioning, global sandbox placement, and CloudLab node replacement |
| [Deployment reliability](developer/deployment-reliability.md) | Drain runners safely, check schema compatibility, and detect stalled queues |
| [Backup and recovery](developer/disaster-recovery.md) | Define retention and recovery targets, preserve keys and coherent host state, and recover a previous artifact release |
| [Automatic PR archiving](developer/automatic-pr-archiving.md) | Configure merge detection, understand session mapping, and verify or recover automatic archival |
| [Runner lease troubleshooting](developer/runner-lease-troubleshooting.md) | Diagnose expired attempts and repeated lease failures |
| [Host runtime settings](developer/runtime-settings.md) | Configure host defaults, server overrides, and the storage reserve |
| [Host execution limits](developer/host-execution-limits.md) | Understand command cleanup, output bounds, checkpoints, and storage admission |
| [Single-host smoke](developer/smoke.md) | Verify a deployment against a gateway and review the original smoke evidence |
| [Security](../SECURITY.md) | Review deployment trust assumptions and report vulnerabilities privately |

## Identity and user-facing behavior

| Guide | Use it to |
|---|---|
| [Agent admission policy](developer/agent-access-policy.md) | Understand permissions, gateway checks, refusals, and compatible rollout |
| [Minimal Agent identity](developer/minimal-identity.md) | Configure the gateway issuer and safely bind an existing database to it |
| [Native conversation recall](developer/context-recall.md) | Understand retained sessions, runtime-specific behavior, and recall verification |
| [Avatars and personal memory](user-memory.md) | View automatic avatars, manage saved preferences, and improve future requests |
| [Native agent questions](developer/operations.md#native-agent-questions) | Answer a waiting agent request and understand reload, skip, and stale-answer handling |
| [Tool approvals](mcp-approvals.md) | Review pending calls across conversations and browse older request history |
| [Conversation navigation](conversation-navigation.md) | Understand the turn rail, previews, jumps, and reading position |
| [Workspace files](workspace-files.md) | Open repository files by path, retry reads, and copy or download previews |
| [Sidebar navigation](sidebar-navigation.md) | Understand project grouping and conversation actions |

## Designs and historical evidence

[specs/](specs/) contains dated designs and reviews.
[acceptance/](acceptance/) contains verification records for their stated
revisions and environments. Some maintained guides also retain dated test logs;
those logs describe the recorded run and do not establish that a current
deployment has passed the same checks.

Useful design entry points include:

- [Reusable cloud environments](specs/2026-10-12-repository-environment-design.zh-CN.md)
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

When a change affects behavior, configuration, or commands, update the maintained
guide in the same change. Check examples against the owning script or source,
keep relative links and section anchors working, and distinguish proposed designs
from implemented behavior. Preserve dated design and acceptance records as
evidence of their original baseline.
