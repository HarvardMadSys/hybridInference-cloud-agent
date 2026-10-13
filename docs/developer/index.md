# HybridInference Cloud Agent

Cloud Agent runs coding agents against a user's repository inside retained
sandbox containers. Users work from a web interface, while enrolled hosts run
Claude Code, Codex, pi, OpenCode, or Kilo. The control plane coordinates jobs,
sessions, and publication to source control. Model access and account identity
come from a [HybridInference gateway](https://github.com/HarvardMadSys/hybridInference),
over HTTP only.

This is the documentation for the software itself: running it locally,
operating a deployment, understanding how it works, and contributing to it. The
source is at
[github.com/HarvardMadSys/hybridInference-cloud-agent](https://github.com/HarvardMadSys/hybridInference-cloud-agent),
under the MIT license; bugs and questions go to its
[issue tracker](https://github.com/HarvardMadSys/hybridInference-cloud-agent/issues).

## Where to start

| If you want to | Start here |
|---|---|
| Run the browser, API, gateway fixture, hosts, and sandboxes on one machine | [Local Docker stack](local-stack.md) |
| Set up a checkout, find the code that owns a change, and run checks | [Developer guide](development.md) |
| Find the module that owns a responsibility and follow its dependencies | [Code structure](code-structure.md) |
| Send a change | [Contributing](contributing.md) |
| Configure credentials, enroll hosts, qualify Kata, deploy, and migrate | [Operations runbook](operations.md) |
| Look up a configuration name, default, or owning component | [Configuration reference](configuration.md) |
| Build release artifacts and deploy them in the required order | [Releases](releases.md) |
| Understand the sandbox trust model before deploying | [Security](security.md) |
| See which gateway APIs Cloud Agent consumes | [Gateway contracts](gateway-contracts.md) |

## Scope of this site

These pages document the software, not any one installation of it. How end
users work with a particular deployment, which agents and models it offers,
and how to get an account there are the operator's to publish. Dated designs,
plans, and acceptance records stay in the repository as engineering history and
are linked from here where they explain a decision.

```{toctree}
:caption: Getting started
:maxdepth: 2

local-stack
development
code-structure
contributing
```

```{toctree}
:caption: Concepts
:maxdepth: 2

context-recall
agent-access-policy
minimal-identity
gateway-contracts
security
```

```{toctree}
:caption: Operations
:maxdepth: 2

operations
configuration
operations-compute
releases
deployment-reliability
disaster-recovery
automatic-pr-archiving
runtime-settings
host-execution-limits
runner-lease-troubleshooting
smoke
```
