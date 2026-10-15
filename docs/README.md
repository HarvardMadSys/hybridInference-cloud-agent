# Documentation

Start with the [project README](../README.md) for a product overview and local
quickstart. `docs/` has three folders:

| Folder | What it holds |
|---|---|
| [developer/](developer/) | Maintained guides for setup, configuration, operations, and contributing. Start with the [developer guide](developer/development.md), the [code structure](developer/code-structure.md), and the [operations runbook](developer/operations.md). These describe current behavior and are updated with the code. |
| [user/](user/) | User-facing feature notes, such as [avatars and memory](user/user-memory.md), [tool approvals](user/mcp-approvals.md), and [conversation navigation](user/conversation-navigation.md). This is a temporary home; the notes will move to the FreeInference distribution's user documentation. |
| [agents/](agents/) | Engineering records written by and for coding agents: [specs/](agents/specs/) and [reviews/](agents/reviews/). |

Repository-wide references stay at the root: [AGENTS.md](../AGENTS.md),
[CONTRIBUTING.md](../CONTRIBUTING.md), [ENVIRONMENT.md](../ENVIRONMENT.md),
[SECURITY.md](../SECURITY.md), and the completed split record,
[MIGRATION.md](../MIGRATION.md).

## Reading the records

| Directory | How to read it |
|---|---|
| `agents/specs/` | Designs and implementation plans as they stood on their date. Later work may have changed a design; the maintained guides and current source describe what is implemented. A plan's commands and checkboxes, such as those in the repository [cutover](agents/specs/cutover.md) and [gateway removal](agents/specs/gateway-removal.md) checklists, describe that work, not the current procedure. |
| `agents/reviews/` | Acceptance runs, conformance audits, and reviews of a stated revision and environment. Their findings apply to what they reviewed; they do not certify a current deployment. |

Historical filenames and line numbers can outlive the code they describe.
Links to surviving files are maintained for navigation; references to removed
files remain as text.

Useful entry points:

- [Reusable cloud environments](agents/specs/2026-10-12-repository-environment-design.zh-CN.md)
- [Native sessions, Fork, and shared workspaces](agents/specs/2026-08-31-native-session-fork-shared-workspace-design.md)
- [Sandbox security model](agents/specs/2026-09-01-agent-sandbox-security-model.zh.md)
- [Public egress and accepted inference-grant replay risk](agents/specs/2026-09-23-agent-public-egress-design.zh-CN.md)
- [Compute resource lifecycle](agents/specs/2026-10-04-compute-resource-lifecycle-design.zh-CN.md)
- [CloudLab node rotation](agents/specs/2026-10-10-cloudlab-node-rotation-design.zh-CN.md)
- [Teams and team environments](agents/specs/2026-10-15-team-shared-workspace-design.zh-CN.md)
- [Per-user MCP conformance audit](agents/reviews/2026-09-11-per-user-mcp-v1-conformance.md)

When a change affects behavior, configuration, or commands, update the
maintained guide in the same change. [AGENTS.md](../AGENTS.md#4-workflow) says
where new records go.
