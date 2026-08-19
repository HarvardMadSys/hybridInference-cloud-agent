# AGENTS.md

Onboarding guide for anyone working in this repository — human or AI coding
agent. Read this first.

## 1. What this is

FreeInference Cloud Agent runs coding agents (Claude Code, Codex, pi, OpenCode,
Kilo) in sandboxed containers against a user's repository and publishes results
as pull requests.

It **was** extracted from the HybridInference monorepo
(`HarvardMadSys/hybridInference`): production cut over on 2026-08-07 and the
gateway's own agent code was deleted on 2026-08-09, so this repository is now
the only implementation. [MIGRATION.md](MIGRATION.md) records what moved and
what was decided along the way, and names the paths still unproven on this
stack. The extraction plan in the HybridInference repo
(`docs/agents/plans/2026-08-03-cloud-agent-repo-split.md`) is history now, not a
to-do list; new work tracks in this repository's issues.

## 2. Repo map

```text
backend/cloud_agent/    control plane (FastAPI): API, auth adapter, scheduler,
                        source-control integrations, publishing, storage
host/cloud_agent_host/  host agent: runner, sandbox, runtimes, workspace broker
web/                    Next.js user UI + admin dashboard
contracts/              OpenAPI mirrors of the gateway contracts we consume
migrations/             Alembic migrations (agent-owned database only)
deploy/{control-plane,host,sandbox}/
tests/{unit,servers,integration,external}/  # plus fixtures/ and support/
docs/
```

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
- One task from the extraction plan = one pull request.
- Never commit secrets. This repository will be made public; treat every commit
  as already public.

## 5. Migration rules — expired 2026-08-09, except 4 and 5

Rules 1-3 applied **only while the split was running**, and by their own terms
they lapsed when it completed. They are kept here so old pull requests still
read correctly. They do not constrain new work: cleanups, renames and refactors
are ordinary pull requests now.

**Rules 4 and 5 are not migration rules and stay in force permanently** — they
are the architecture of the split, not the hygiene of performing it.

They existed because a migration whose diff mixes moves with edits cannot be
reviewed, and because a parity claim needs an unchanged baseline to be worth
anything.

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
4. **No gateway secrets here.** This service never sees the FreeInference
   gateway's `API_KEY_SECRET` or `JWT_SECRET_KEY`. Grant tokens are minted *and*
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
  scoped to a user, a job, and a model allowlist. It is the only credential that
  enters the sandbox. Revoked when the attempt is superseded. It carried an MCP
  scope until server ownership moved to the user; MCP servers are now resolved
  per user by the control plane, not named in the grant.
  **There is no per-task dollar budget** — that design was considered and
  rejected. All inference spend stays subject to the gateway's existing
  per-user quota; a grant narrows *what* can be called, never re-implements
  *how much*. If a task seems to need a budget field, stop — the plan's C5
  section records the decision.
- **Host.** A machine in the pool running the host agent. It registers with a
  one-time enrollment token, heartbeats, and claims jobs it has capacity and
  capabilities for. Hosts are added and removed without redeploying the control
  plane.
- **Sandbox.** Per-task container with **two egress phases**, enforced by the
  code in `host/cloud_agent_host/egress.py` and `egress_proxy.py`:
  - *Setup phase* — dependency installation may reach package registries, but
    only through the Squid allowlist proxy, which permits CONNECT to an
    allowlisted host by name and refuses everything else.
  - *Agent phase* — untrusted repository code runs, and the sandbox reaches the
    gateway only, via a fixed-address relay. The relay is not a proxy: the
    caller cannot choose a destination.

## 7. Security boundaries — do not relax without review

- During the agent phase the sandbox reaches exactly one network destination:
  the gateway relay. The setup-phase allowlist proxy is the only sanctioned
  widening, and the escape hatches (`AGENT_SANDBOX_ALLOW_OPEN_NETWORK`,
  `AGENT_EGRESS_AGENT_TIER=custom`) must stay unset anywhere untrusted
  repositories run.
- Long-lived or cross-task credentials never enter the sandbox. Only
  short-lived, single-task, scoped, revocable grants do — spend is governed by
  the gateway's per-user quota, not by anything the sandbox holds.
- Public multi-tenant use requires Kata/gVisor isolation, non-root, dropped
  capabilities, seccomp, and resource limits — plain Docker containers are for
  trusted internal repositories only.
