# Continuous deployment

Two branches, two environments, and they federate with the matching gateway:

| Branch | Environment | Deploys | Gateway it talks to |
|---|---|---|---|
| `dev` | staging | automatically, on green CI | the gateway's own `dev`, i.e. its staging deployment |
| `main` | production | only when dispatched | the gateway's `main`, i.e. its production deployment |

`main` is not a branch anyone commits to: `sync-main.yml` fast-forwards it to
`dev` on request, and that is the only way it moves.

## Staging

Merging to `dev` deploys. `.github/workflows/deploy-staging.yml` waits for
`CI` to conclude green on `dev`, then starts a deploy script on each target in
parallel.

**Two machines, two parallel jobs, one runtime compatibility fence.** The control plane and web app
run on spark2 (ARM64); runners, the workspace broker, egress proxy, and tunnels
run on the sandbox host (x86_64). They are deployed by
`deploy/control-plane/deploy.sh` and `deploy/host/deploy.sh`. Image builds and
the control database migration overlap with host deployment. The host marks the
exact target SHA ready only after every runner, broker, tunnel, isolation check,
and Docker data-root check passes. The control-plane script waits for a fresh
matching heartbeat from every new-chat placement host. It also dials every
online host that still owns a retained group, even after the admin placement
pin moved elsewhere, before it replaces any running API container. The protocol
gate therefore preserves host-before-API compatibility without putting the
whole Actions workflow on a serial critical path.

On a cold machine, the control script first starts Postgres and runs Alembic.
If no control-plane container is running, it then starts the target API and the
scoped, loopback-bound MCP relay before entering the readiness wait. That
cold-start exception is necessary because host enrollment/heartbeat need the
API and host readiness probes both fixed sandbox relays: after `compose down`,
keeping either service stopped is a deadlock. There is no old API to preserve
in this branch; web remains stopped, work waits for an eligible host, and a
failed gate stops both bootstrap services again. An ordinary rolling deployment
does not take this branch and leaves the old services untouched until the
exact-SHA host gate passes. As with every rolling migration, new migrations
must remain compatible with the old API during this window. After the gate
passes, bootstrap peers are part of the desired stack; a failed final API/web
probe leaves them running for diagnosis and does not write `.deployed-sha`.

The retained topology is the only executor. Until an enrolled host reports
`shared-sandbox-v1`, the exact deployment-SHA attestation, and a usable broker,
deployment stops; there is no disposable fallback. A host heartbeat is
machine-level, while each claim repeats the protocol attestation from the exact
runner replica so a mixed rolling deployment cannot borrow a newer sibling's
capability. The SHA marker is deployment infrastructure metadata, not a
behavior flag.

Historical native Fork is fenced separately by
`native-fork-at-anchor-v1`. During an image upgrade an existing retained
container may continue ordinary follow-ups, but Fork is unavailable unless the
host's current sandbox image digest equals that group's immutable digest. This
prevents a v1 helper from accepting a v2 at-anchor request and accidentally
forking the latest message.

The enrolled credential lives under `AGENT_HOST_STATE_ROOT`, which may be owned
by the runner container's user. Deployment readiness lives in `.shared-live-ready`
beside `.deployed-sha` in the deployment checkout, where the deployment user can
write it. Runners bind only that marker file read-only; completing readiness
overwrites the same inode so existing mounts see the new SHA. Neither location
is a chat workspace or session mount. Chat files and Agent CLI state stay in
each retained container's Docker writable layer.

The host script is thin on purpose: `sandbox_host.sh up` already checks
the daemon, requires the environment, runs the isolation preflight **fail
closed**, builds the sandbox image, starts the stack, and probes the gateway
from inside the closed network. Re-implementing any of that in the deploy would
give it a second opinion about whether the host may run untrusted code.

### Staging retained topology

There is no topology feature flag. Local, staging, and production builds all
contain only the retained shared-session path. Safety remains fail-closed:

- `STAGING_HOST_SLOTS` defaults to `3` and drives both `AGENT_HOST_SLOTS` and
  `AGENT_RUNNER_REPLICAS`. One runner executes one turn synchronously, so the
  host refuses readiness when declared slots exceed configured executors or
  when fewer than the configured replicas are actually running.
- `STAGING_RETAINED_DOCKER_DATA_ROOT` defaults to `/var/lib/docker`. Override it
  when the daemon uses a dedicated durable data-root; this is a daemon path,
  never a bind mount into a chat container.
- `AGENT_CHAT_IDLE_TIMEOUT_S` is fixed at 1800 seconds for staging, so an idle
  group is stopped after 30 minutes and its Docker writable layer is retained.

Before building, `sandbox_host.sh` rejects a missing enrolled
identity, invalid slot/capacity, or a daemon whose reported `DockerRootDir` does
not match the configured retained root. After startup it asks every runner to
attest `shared-sandbox-v1`, authenticates to the broker health endpoint, proves
the control-plane tunnel is reachable, and rejects a revoked host credential.
The control plane rejects claims without its broker credential, enrolled host
identity, and exact replica attestation. A green host deployment therefore
proves both directions of the broker/control-plane path rather than merely
proving that environment strings reached Compose.

Production uses the same topology. Promotion is still gated by production
isolation, real-provider runtime conformance, capacity, and storage checks; it
cannot switch back to an older executor.

## Two stacks per host, and how they stay apart

The sandbox host runs both environments. What separates them is `COMPOSE_PROJECT`
— the prefix on every container, image and network compose creates — so it is
required, with no default, in each stack's own `.env`:

| | Project |
|---|---|
| staging | `freeinference-cloud-agent-sandbox-staging` |
| production | `freeinference-cloud-agent-sandbox-prod` |

Staging and production use separate Actions concurrency groups, so their host
rollouts may run at the same time. This is safe only because every mutable stack
surface in the table below is environment-specific: Compose project, sandbox
image, networks, deployment root and host-state root. The Kata preflight run by
deployment is read-only (`--check`); installing or changing a host runtime is a
separate operator action. Within each environment, the control-plane and host
jobs also run in parallel behind the exact-SHA readiness fence.

**Sharing it is silent, which is why there is no default.** Two deploys under
one project name are not two stacks: the second `up` adopts the first's
containers and recreates them with its own configuration, and the first
environment finds its runners reconfigured with no error anywhere.

`deploy/host/deploy.sh` also refuses to start when containers of *this* stack's
shape are running under a project it does not know. That is not a hypothetical:
this host was first brought up by hand as project `agenthost` while the script
used a different name, and deploying produced two complete stacks — six runners
claiming instead of three, and **two workspace brokers**.

The brokers are the dangerous half. The control plane stores one `broker_url`
per host, so whichever stack heartbeats last wins it, and a job claimed by a
runner in one stack has its files and terminal served from the other. The
symptom is "the workspace is empty", which is nobody's idea of "there are two
of me".

The other environment is expected company rather than a stray, so name it in
`SIBLING_COMPOSE_PROJECTS` (comma-separated) and the guard stops reporting it.
Anything still unrecognised is what the guard is for. `ALLOW_STRAY_STACK=1`
overrides everything, and discards the protection with it.

## Why there is no SSH in any of this

The runners are installed on spark2, which is also the deployment target, and
they run as the user that owns `/srv/freeinference-cloud-agent`. So the deploy
job runs *on* the host: no deploy key, no `known_hosts` secret, no `sudo`. The
gateway's equivalent workflow carries about sixty lines of host-key handling
for one reason — its runners are on a different machine from its target.

For the same reason the host holds **no git credential**. `deploy.sh` fetches
the commit from the runner's own workspace, which is a full checkout of exactly
that commit, on the same disk, authenticated by the job's token. A deploy key
on the box would be a standing secret whose only job is to re-download
something already sitting a few directories away.

## One-time setup on a new host

`deploy.sh` refuses to run against a directory that is not a git checkout,
because `git reset --hard` in a plain directory silently does nothing useful.
Converting an rsync-populated tree is safe **only after** proving it matches
the commit it claims to be:

```bash
cd /srv/freeinference-cloud-agent
git init -b dev
git remote add origin https://github.com/HarvardMadSys/hybridInference-cloud-agent.git
git fetch <a local clone, or origin with credentials>
git reset --mixed "$(cat .deployed-sha)"   # HEAD + index only; files untouched

# This must print nothing. If it prints anything, the host has changes that
# exist nowhere in git and the next deploy would erase them — get them into a
# PR first. That is not hypothetical: #29 recovered two real fixes this way.
git status --porcelain --untracked-files=no
```

`.env` is untracked and stays untracked — it holds this deployment's secrets.
`git reset --hard` never touches untracked files, so deploys leave it alone.
Never run `git clean` in this tree.

## Rolling back

Dispatch **Deploy Staging** with `sha` set to a compatible commit to go back to.
The workflow always runs the current deployment orchestrator against a separate
checkout of that target, so choosing an old SHA cannot also remove the exact-SHA
fence. A target predating `deploy/DEPLOY_PROTOCOL` is refused before Docker or
the checkout in `DEPLOY_ROOT` is changed, as is a target carrying an older
protocol value; recover forward to a compatible commit instead of bypassing the
fence. Because `workflow_run` definitions come from the default branch, sync
this orchestrator to `main` before relying on that guarantee and do not dispatch
the older default-branch workflow during the transition.

Alembic runs forward on every deploy. Do **not** downgrade a schema while the
newer API is still serving: it may immediately read columns that the downgrade
removed. The supported emergency sequence is code rollback to a
schema-compatible commit first, verify it is serving, and only then perform a
separately reviewed contract migration if one is genuinely required. The
native-turn migrations in this rollout are expand-first and may remain in place
during code rollback.

Control and host rollback jobs also start in parallel, but the control script
will not replace the API until a host advertises the rollback SHA. A missing or
offline sandbox runner therefore blocks a control-only rollback intentionally:
restore/deploy the host half first rather than bypassing the compatibility fence.
There is no implicit break-glass that can install a control protocol no runner
has proved it speaks.

## What the script refuses to do

- Deploy to a directory that is not a git checkout.
- Deploy over modified tracked files (`ALLOW_DIRTY=1` overrides, and discards).
- Deploy a commit other than the one asked for — it verifies the source clone's
  HEAD matches the requested SHA, and verifies the result after resetting.
- Write `.deployed-sha` before the health checks pass. That file answers "what
  is actually running here", and writing it early answers it wrongly for every
  failed deploy.
- Enable shared-live scheduling before a post-check host heartbeat and a
  control-plane-to-broker health request both succeed. `SHARED_READY_TIMEOUT`
  controls this deployment-only wait and defaults to 900 seconds.

## The names

Environment suffixes are on the things that share a machine, and only those.
The sandbox host runs both environments, so everything it creates carries one;
each control plane is alone on its box, so nothing there does.

| | staging | production |
|---|---|---|
| control-plane project | `cloudagent` | `freeinference-cloud-agent-control-prod` |
| control-plane deploy root | `/srv/freeinference-cloud-agent` | `/srv/freeinference-cloud-agent` |
| sandbox project | `freeinference-cloud-agent-sandbox-staging` | `freeinference-cloud-agent-sandbox-prod` |
| sandbox deploy root | `…/freeinference-cloud-agent-staging` | `…/freeinference-cloud-agent-prod` |
| sandbox image | `freeinference-cloud-agent-job:staging` | `freeinference-cloud-agent-job:prod` |
| Agent-turn egress network | `freeinference-cloud-agent-egress-staging` | `freeinference-cloud-agent-egress-prod` |
| setup/proxy networks | Compose-project-derived `-egress-trusted` / `-egress-uplink` | Compose-project-derived `-egress-trusted` / `-egress-uplink` |
| state roots | `/var/lib/freeinference-cloud-agent-staging/…` | `/var/lib/freeinference-cloud-agent-prod/…` |
| runner labels | `deploy-cloud-agent-control-staging`<br>`deploy-cloud-agent-sandbox-staging` | `deploy-cloud-agent-control-prod`<br>`deploy-cloud-agent-sandbox-prod` |

The two words that do the work: **control** is the plane that schedules — API,
web app, its own Postgres; **sandbox** is the machine that runs user code —
runners, workspace broker, egress proxy, tunnels. The image a job runs inside is
`-job`, deliberately not `-sandbox`, so it cannot be confused with
`…-sandbox-<env>-runner`, the image the runner container itself is built from.

**The host name is not one of these.** `AGENT_RUNNER_HOST` names the machine
only until it enrols; after that the control plane reads the host from the
credential the runner holds, so editing the variable renames nothing. Two stacks
on one machine therefore stay apart by having **their own state roots** — the
credential lives under `AGENT_HOST_STATE_ROOT`, and sharing that directory means
the second stack claims as whichever host enrolled first. `sandbox_host.sh`
requires that root plus the stack's existing PlatformOnly network name once
`SIBLING_COMPOSE_PROJECTS` is set, because that is the moment the defaults stop
being safe. The new Trusted/uplink network names derive from the Compose project.

`cloudagent` on spark2 is the one name left alone. It is alone on that machine,
so it needs no suffix, and renaming a compose project renames its **named
volume** with it — `cloudagent_agent-postgres` — which means the next `up`
creates an empty database and staging looks wiped. Worth doing deliberately, if
ever; not worth doing in passing.

## Production — what is still missing

`.github/workflows/deploy-prod.yml` exists and runs nothing yet, by
construction: its jobs ask for runner labels no machine carries, so a dispatch
queues instead of deploying. Borrowing staging's labels in the meantime would
have made the first production dispatch redeploy staging.

To finish it:

1. **Two production hosts** — one control plane, one sandbox host. Install a
   repository runner on each and label them `deploy-cloud-agent-control-prod`
   and `deploy-cloud-agent-sandbox-prod`.
2. **Convert each deploy root to a git checkout** and place its `.env`, exactly
   as in *One-time setup on a new host* above. The sandbox host's `.env` needs
   `COMPOSE_PROJECT` and `SIBLING_COMPOSE_PROJECTS`; see `deploy/host/.env.example`.
3. **Set the repository variables** the workflow refuses to run without:
   `PROD_CONTROL_DEPLOY_ROOT`, `PROD_SANDBOX_DEPLOY_ROOT` and
   `PROD_SANDBOX_COMPOSE_PROJECT`. `PROD_CONTROL_COMPOSE_PROJECT` only if that
   machine runs another stack of this service. `PROD_HOST_SLOTS` defaults to
   three and drives both scheduler slots and runner replicas;
   `PROD_RETAINED_DOCKER_DATA_ROOT` defaults to `/var/lib/docker` and must name
   the daemon's actual durable data root.
4. **Configure the production gateway first** — `IDENTITY_JWT_PRIVATE_KEY`,
   `IDENTITY_ALLOWED_REDIRECTS`, and a `GATEWAY_GRANT_DISPATCH_TOKEN` that is
   *not* staging's — then confirm it from outside:

   ```bash
   GATEWAY_GRANT_DISPATCH_TOKEN=... python deploy/preflight/check_gateway.py https://<production gateway>
   ```

5. **Then** the gateway's own frontend can point `/agents` at this deployment
   (`AGENT_WEB_INTERNAL_URL` and `AGENT_CONTROL_PLANE_INTERNAL_URL`). That
   rewrite is the user-visible cutover, and it must come last: set before this
   service exists, it hands `/agents` to nothing.

Order matters throughout. Configure the gateway first. Within this service,
the production workflow starts host and control jobs together; the control job
replaces the API only after the host publishes the target-SHA heartbeat and its
reverse-broker readiness fence passes.

Rollback is the same as staging's: dispatch **Deploy Prod** with `sha` set to
the commit to go back to.

## Renaming staging's sandbox stack

The names above are what a new stack gets. Staging's existing one still runs as
`hybridinference-agent-remote`, and its repository variable still says so, which
is why merging this changes nothing on that host. Moving it is a deliberate step
with a drain in it, because a compose project cannot be renamed — only replaced:

1. Drain: stop dispatching, or accept that jobs in flight are superseded.
2. `docker compose -p hybridinference-agent-remote -f deploy/host/docker-compose.sandbox.yml --env-file .env down --remove-orphans`
   — the old project must be **gone**, not merely idle, or the guard on the next
   deploy will report it and refuse.
3. Add `COMPOSE_PROJECT=freeinference-cloud-agent-sandbox-staging` to that host's
   `.env`, and rename the deploy root to match if you are renaming it too
   (`mv` it, then update `STAGING_SANDBOX_DEPLOY_ROOT`).
4. Point `STAGING_SANDBOX_COMPOSE_PROJECT` at the new name.
5. Dispatch **Deploy Staging**. It builds under the new project and the
   containers come back as `freeinference-cloud-agent-sandbox-staging-runner-1`
   and so on.

The old stack's images stay behind under the old prefix; `docker image prune`
after you are satisfied, not before.

## Health checks

`/healthz` on the control plane, and the web app **at its base path** —
`AGENT_WEB_BASE_PATH`, not `/`. The app is mounted under a prefix, so `/` is a
404 by design; a check pointed there fails every deploy of a perfectly healthy
app. On staging: `/` → 404, `/agents` → 200, `/agents/` → 308.

## Conversation recall verification

Health and matching deployment SHAs do not prove model recall. Before accepting
a rollout, follow [the native conversation recall checks](../../docs/context-recall.md),
including the opt-in Flash model preflight and an app follow-up/idle restore.
The model check requires a separately supplied test key and is not enabled by
the deployment workflows automatically.
