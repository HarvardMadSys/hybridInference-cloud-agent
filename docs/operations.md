<!--
Moved from the source repository at the freeze commit `764a6f97`
(`docs/developer/agent-sandbox-operations.md`), task H5. Paths were updated for
this repository's layout and nothing else was rewritten; every deviation from
the source text is listed in the "What changed when this moved" section at the
end. "The gateway" still means the FreeInference gateway; after the split, the
job API, the store, the publisher and the admin host switch belong to *this*
service's control plane, and the gateway keeps inference, MCP and identity.
-->

# Cloud agent sandbox — operations

What is on `dev` today, what is verified, and the exact steps left before a
job can run for real. This page is about operating it; design rationale lives
in the project's issue history.

## What a job does

```text
POST /v1/agent/jobs           owner queues work
  → dispatcher claims          internal-role credential, mints three tokens
    → runner checks out        read-only clone credential, never enters the sandbox
    → sandbox runs the agent   only a model-scoped token crosses the boundary
      → normalized events      streamed to the owner over SSE
      → model calls            billed through the gateway into api_logs
    → patch artifact           the agent never pushes
  → publisher validates        .github/ block, secret scan, path escape, size
    → agent/<job-id> branch    pinned refspec, never dev/main
      → draft PR               a human reviews before anything merges
```

Two properties are load-bearing and easy to break by accident:

- **No repository credential ever enters the sandbox.** The agent emits a
  patch; the publisher — running in the gateway, outside the sandbox — is the
  only component holding a GitHub token that can *write*. The runner does hold
  a clone credential, but it is read-only, scoped to the one repository, and
  deliberately never recorded in `.git/config`: the checkout adds no remote,
  the credential travels in git's environment rather than its argv, and the
  runner refuses to start the agent if it finds the token anywhere under
  `.git/`.
- **The sandbox's only credential is model-scoped.** It buys inference as the
  job's owner: the grant names the models it may reach, it dies on a short TTL
  unless the control plane keeps renewing it, and what it spends lands against
  that owner's gateway daily quota. **There is no per-job spend cap** — C5/C6
  removed it deliberately, so do not quote one as the bound on a leaked
  credential; the bound is the grant's scope plus its TTL. It cannot write the
  event log, overwrite the patch, or move the job to a terminal state; the
  runner holds the token that can.

## Verified

Against real components on a developer machine:

| | How |
|---|---|
| Store: fencing, reaper, one-shot publish | dbtests against Postgres |
| Job API, SSE resume, worker endpoints | live gateway |
| Per-job credential: auto-revoke ~~, budget 429~~ | live gateway. The struck half exercised a per-job spend cap that no longer exists: C5/C6 replaced it with the owner's gateway daily quota, which this service does not enforce and so cannot verify here |
| Runner + runtime adapter | real Claude Code CLI, end to end |
| Publish: patch → branch → draft PR | real `git push` to a local bare repo |
| **A real job on a real model** | live Claude Code CLI × `deepseek-v4-flash` through the production gateway: docstring landed on disk, patch produced, 3 calls / 74,523 tokens / $0.0075 attributed to the job in `api_logs` |
| Sandbox isolation | real Docker: non-root, `CapEff: 0000000000000000`, cap-drop enforced, `--network none`, `--rm` leaves nothing |
| Checkout → edit → patch | real git against a real repository: pinned commit materializes the right tree, an agent edit and a new file both appear in the patch, no remote is left behind |
| Worktree handover to the sandbox user | real Docker: root creates a `0700` worktree, chowns it to 10001, and a `--user 10001` container writes to it and produces a diff. Without the chown the same container gets `Permission denied` and `fatal: not a git repository` |
| The gateway on staging | `/v1/agent/*` live: job create/get/list/cancel, SSE stream, and a non-dispatcher key refused at `/worker/claim` with 401 |
| MCP: registry refusals, proxy authorization, tool filtering | unit + API tests against a mocked upstream: an unset credential refuses at load, a stdio server is rejected, a job cannot reach a server it was not granted, a dead fence loses tools with inference, a blocked tool never leaves the gateway, and a filtered catalogue never advertises one |
| **MCP: what `--strict-mcp-config` (#1129) actually prevents** | the flag was added on the strength of the CLI's documented behaviour; this is the measurement. Real Claude Code 2.1.220 in a job's exact configuration (headless, `bypassPermissions`, cwd = a checkout carrying a `.mcp.json`): without the flag the CLI reports the repository's server alongside the platform's, with it only the platform's. No approval step was involved either way |
| MCP: the adapter's `--mcp-config` is accepted by the real CLI | the argv `ClaudeCodeRuntime.prepare` emits, run against the real binary: the platform's server appears in `system/init`, and a deliberately bogus flag in the same argv fails instantly, so the flags are being parsed rather than ignored |
| MCP: the token can stay out of argv | the same run with the header written as `Bearer ${AGENT_MCP_TOKEN}` and the variable exported: the CLI expanded it and the proxy authenticated the request, while the command line carried only the placeholder |
| **MCP: the proxy against a real hosted server** | the shipping router and filters against live `mcp.deepwiki.com` (which answers over SSE, so the streaming filter is the one that ran): `initialize` → `serverInfo: DeepWiki`, `tools/list` → its three real tools, `tools/call read_wiki_structure` → real documentation for `psf/requests` |
| MCP: the allowlist, on a real catalogue | same server, `tools: [read_wiki_structure]`: `tools/list` returned that one tool of the three, and `tools/call ask_question` came back `-32601` without the request leaving the gateway |
| MCP: the authorization matrix, end to end | live token + granted server 200; live token + a server the job was not granted 403; a superseded attempt 401; an ordinary `hyi-` key 401; no credential 401 |
| MCP: Claude Code driving the proxy | the real CLI, pointed at the proxy, completed the whole handshake through it — `initialize`, `notifications/initialized`, the `GET` stream, `tools/list` — each reaching deepwiki and returning 200 |
| **A self-hosted runner completing a real job** | `ajob_04de5d509a25ded2` against staging: claimed → cloned `psf/requests` at `414f0513` (verified equal to that repo's HEAD) → Claude Code 2.1.220 in a Docker sandbox as uid 10001 → `Read` + `Edit` with `is_error: false` → 518-byte patch stored → `succeeded`. The patch was read back and matches the file the agent left on disk |

## Not verified

Be precise about these when reporting status:

- **The full model matrix.** One real job on one real model is verified (see
  above); the 2-runtime × 3-model matrix has not been run, so cross-model
  behaviour differences are still unknown.
- **No real *job* has used MCP.** The proxy is verified against a live server
  (see above), but through a harness: the shipping router and filters, with a
  faked job store, and an agent CLI running outside a sandbox. What has not run
  is the whole chain — a queued job, a runner, a real sandbox on the
  `platform_only` network reaching `/v1/agent/mcp/...`. That last hop is the
  same host and network as the model calls the sandbox already makes, so it
  follows rather than being hoped for, but it has not been observed.
- **No credentialed MCP server has been exercised live.** `deepwiki` takes no
  credential, so the registry's header attachment — the part that matters most
  for GitHub — is covered by unit tests and by a live run that only proves a
  configured header is sent, not that a real server accepts it.
- **No model has actually used these tools.** Every live check above was made
  by a client, not by an agent deciding to call something. Whether a small
  model on staging uses an MCP tool well, or at all, is unmeasured.
- **The Actions workflow has never executed on GitHub.** See the blockers.
- **Production.** `/v1/agent/*` is live on staging; production has not been
  deployed from it — production returns 404 on those routes today.
- **Staging's model surface is thin.** Of the 15 models `/v1/models` lists,
  only `glm-5.1` and `qwen3.6-35b` actually resolve for an agent-job token, and
  both land on `qwen3.6-35b`. Everything else answers `404 Model not found`, so
  a job that names one fails at its first turn. Pick a model that resolves
  before concluding anything about the chain.
- **Kata.** No job has run under an actual Kata kernel, so every run so far
  shared the host's. Isolation was verified on a shared-kernel container.

  What changed is that the host side is now provisionable rather than absent.
  `deploy/host/setup_kata_runtime.sh` installs and links a pinned Kata release,
  and the runner's own preflight refuses to claim jobs until one real container
  comes back reporting a kernel that is *not* the host's;
  `deploy/host/sandbox_host.sh up` surfaces that verdict before it
  returns. Both are covered by unit tests, including the case where the runtime
  is accepted and the sandbox still lands on the host kernel.

  Neither has been run on the staging runner host. Installing into `/opt/kata`
  is a root-level change to a live machine, so it is the next step rather than a
  completed one — and until it happens, this bullet stays here. See
  [Provisioning Kata on a runner host](#provisioning-kata-on-a-runner-host).

  The daemon-side wiring was already ruled out as the problem. The three cases
  give three distinct errors, which is what makes this meaningful rather than
  hopeful:

  | `--runtime` | Result |
  |---|---|
  | unset (runc) | runs |
  | a name that does not exist | `unknown or invalid runtime name` — the daemon rejects it, so the flag is reaching the daemon and is not being silently dropped |
  | `io.containerd.kata.v2` | `failed to start shim` — the name is accepted and the shim is attempted; only the binary is missing |

  A dropped flag would be the dangerous failure: the kata backend would
  silently run as a plain shared-kernel container while every log line and
  config said otherwise. That is ruled out.

## Who may run a job against which repository

Two independent entitlements, and a repository qualifies on either. Both fail
closed: with neither, no job can be created at all.

**A connection the user made.** They authorize the GitHub App, GitHub returns a
code to `<frontend>/agents/connected`, and the platform exchanges it for a
token that speaks *as that user* to ask which installations they can reach.
Those installation ids are recorded against the user, and the repositories they
cover are what the composer offers. Every step is GitHub's answer rather than
the requester's — which is the point, because the requester chooses the
repository and the *platform* mints the credential for it. Anything the
requester could simply assert would be a confused deputy.

**The deployment allowlist** (`AGENT_REPO_ALLOWLIST`), for the single-tenant
dogfood where the repository is the operator's own and there is no user to
connect.

Disconnecting removes the platform's half. Uninstalling the App on GitHub is
the other half, and is what actually revokes the platform's reach.

## MCP servers

An agent with no egress can still use the MCP ecosystem, because it never
reaches a server directly:

```text
sandbox ──[job token, gateway URL]──▶ gateway ──[deployment credential]──▶ MCP server
```

This is the publisher's shape reused. The sandbox authenticates to
`/v1/agent/mcp/<name>` with the capability token it already holds, over the
network it already has, so **`platform_only` stays closed** — no allowlist
entry, no new network, nothing to configure. The gateway holds the upstream
address and its credential, and the sandbox learns neither.

Adopting a server is a stanza of YAML, not code — **on the gateway**, which is
where the registry and the proxy stayed after the split. Point `MCP_CONFIG_PATH`
(or that repository's manifest `paths.mcp`) at a registry; its
`config/examples/mcp.example.yaml` is the one to copy. No file means no MCP
servers, which is a supported shape. This service only ever names servers by id.

```yaml
servers:
  - name: github
    url: https://api.githubcopilot.com/mcp/
    default: true
    headers:
      Authorization: "Bearer ${GITHUB_MCP_TOKEN}"
    tools: [get_issue, list_issues, get_pull_request]
```

Four things about this that look like details and are not:

- **Only remote streamable-HTTP servers.** `command:` is refused rather than
  unimplemented. A stdio server is third-party code, and the two places to run
  it are the sandbox that has no egress (where a server wrapping an external
  API is useless) and the gateway that is trusted. Hosting them needs its own
  isolated tier.
- **`tools:` is the control that matters, and it is enforced in both
  directions.** A tool not listed is never advertised to the model *and* is
  refused if called anyway. Omit it and every tool the server exposes is
  reachable — including any write tool it gains in a later release, with no
  config change here to notice. The gateway logs
  `agent_mcp_server_unfiltered` at startup for each such server. An agent runs
  on untrusted repository content, so a README can try to talk it into calling
  whatever the credential permits: scope the token narrowly and list the tools.
- **An unset `${VAR}` refuses at startup**, naming the variable, instead of
  sending a blank `Authorization` and failing deep inside somebody's job.
- **A job reaches the servers it was created with**, recorded on the job row.
  Not the registry's whole list — a token names a server in the URL, and the
  check is against the job's own grant. Omitting `mcp_servers` at creation
  takes the registry's `default: true` entries; an explicit `[]` means none.

Only `claude-code` can be given servers, because it is the only adapter with a
gateway-mediated path. Every other one still fails closed the way #1129 left
them, so a job naming servers on one of them is refused at creation rather than
run with the tools silently missing. Codex is a matter of verifying two flags
against the pinned `CODEX_VERSION`, not of design.

The credential is referenced, not embedded: the generated config carries
`${AGENT_MCP_TOKEN}` and the adapter exports it, so the job token never becomes
a process argument — where it would be readable from the process table and
liable to be copied into an error tail or an event payload the owner reads.

### What `--strict-mcp-config` is holding shut

The flag landed in #1129 on the strength of the CLI's documented behaviour, and
`--mcp-config` now supplies the platform's servers alongside it. The pairing is
load-bearing in both directions: strict mode makes our config the *only* source
of servers, and our config is passed even when empty so there is no gap.

What the flag prevents was measured rather than assumed, in exactly the
configuration a job runs in (headless `-p`, `bypassPermissions`, cwd = the
checkout). A directory containing

```json
{"mcpServers":{"evil_repo_server":{"command":"/bin/echo","args":["pwned"]}}}
```

produces these two `system/init` lines, differing only in the flag:

| invocation | `mcp_servers` reported by the CLI |
|---|---|
| `--mcp-config <ours>` | `evil_repo_server`, `github` |
| `--mcp-config <ours> --strict-mcp-config` | `github` |

No prompt, no approval step: the permission mode the sandbox needs for its
*own* work is exactly what removes the one gate that would have stopped this.
That is why the flag stays unconditional now that there is a real config to
pass beside it — a job with no MCP at all is the case where an injected server
would be least expected and least noticed.

The design's own rule about egress is what applies here: a boundary the agent
participates in is not a boundary.

## Before a job can run for real

Two things are outside the code and must be done by a human.

### 0. A closed agent network needs the gateway on it

`platform_only` means "our gateway and nothing else", which assumes the gateway
is *on that network* — true when it is the compose `backend` service, false the
moment `AGENT_GATEWAY_URL` points at a remote one. The sandbox then resolves
nothing and every job dies at its first model call, with an error that reads
like a broken model.

Preflight probes this: it starts one container on the agent phase's real
network and **makes a request**, refusing at startup if no HTTP response comes
back. Any status counts, 401 and 404 included — the question is whether packets
arrive, not what the gateway makes of them. It used to stop at DNS, which
passes for anything holding a record, including a relay that answers to the
gateway's name and cannot reach the gateway behind it; that is exactly the
shape of a cross-machine deployment, so the topology needing the check most was
the one it could not see. A sandbox image with neither `curl` nor `python3`
falls back to DNS and logs `agent_sandbox_gateway_probe_degraded` rather than
letting the weaker check pass for the stronger one.

For a runner host that is not the gateway's, see
[Running runners on another machine](#running-runners-on-another-machine) — the
supported answer is a tunnel on the closed network, not a routable one. Opening
the sandbox's network instead is possible and is a real cost:

```bash
AGENT_EGRESS_AGENT_TIER=custom
AGENT_EGRESS_NETWORK_CUSTOM=agent-routable
```

That gives the agent phase general egress — untrusted repository code included
— which is the property `platform_only` exists to hold.

One more compose detail worth knowing: the overlay *declares* `agent-egress`,
so compose insists on creating it. A network of that name created by hand
beforehand is refused with a label mismatch — let compose own it, or point the
tier variables at names compose does not declare.

### 1. The workflow must be on the default branch

A workflow is only *registered* once its file exists on the default branch
(`main`). Until then it cannot be triggered **by any means** — not
`repository_dispatch`, not the schedule, and not `workflow_dispatch` even with
an explicit `--ref dev`. Verified rather than assumed:

```console
$ gh workflow list --all | grep agent-job-runner      # absent
$ gh api -X POST .../workflows/agent-job-runner.yml/dispatches -f ref=dev
{"message":"Not Found","status":"404"}
```

So the Actions path is gated on a production release, not on configuration.

Self-hosted runners have no such constraint: they poll `/v1/agent/worker/claim`
and can run as soon as the gateway is deployed. That is the shorter path to a
first real job.

### 2. Credentials

**Dispatcher credential** — set the same `AGENT_DISPATCHER_TOKEN` on the
gateway and the runner; the claim gate accepts it directly.

```bash
# both sides — one value, one purpose
AGENT_DISPATCHER_TOKEN=$(openssl rand -hex 32)
```

Prefer this over pointing the runner at `ADMIN_TOKEN` (which the gate still
accepts, for migration): the runner host executes untrusted repository code
next door, and the credential it holds should open exactly one door. The
dedicated token claims work and does nothing else — to every other route it is
an invalid credential, and rotating it touches nothing but the two settings
above.

It must not be an ordinary user key: that endpoint takes the oldest queued job
**across all tenants** and returns its repo, prompt, and a working capability
token, so an ordinary key there would be a cross-tenant read.

**GitHub credential** — install a GitHub App and give the *gateway* its private
key. Nothing needs to be minted or rotated by hand: the platform signs a
ten-minute App JWT and exchanges it for an hour-long token scoped to the
installation covering the repository being published to.

```bash
AGENT_GITHUB_APP_ID=123456
# A path the gateway can open — which in a Compose deployment means a path
# inside the *backend container*. It mounts `config/`, `distributions/` and
# `var/data/`, and nothing else: an /etc path on the host reads as "no such
# file" from in there. `var/data` is gitignored, so the key survives the
# deploy's `git reset --hard` and is never a candidate for commit.
AGENT_GITHUB_APP_PRIVATE_KEY_PATH=/app/var/data/agent-app.pem
```

Put the file at `<APP_DIR>/var/data/agent-app.pem` on the host, owned by the
service user and `chmod 600`. Alternatively `AGENT_GITHUB_APP_PRIVATE_KEY`
takes the PEM inline (escaped `\n` are accepted and unescaped), which avoids
the mount question entirely at the cost of a very long line in `.env`.

The App needs `contents: write` and `pull_requests: write` and nothing else —
notably not `workflows`, so a patch touching `.github/` cannot be pushed even
if the gate were bypassed. Revocation is uninstalling the App.

For the *connect* flow (a user authorizing their own repositories) the App
also needs its OAuth half, with the callback pointing at this deployment's
frontend:

```bash
AGENT_GITHUB_APP_CLIENT_ID=Iv1.xxxxxxxx
AGENT_GITHUB_APP_CLIENT_SECRET=...
AGENT_GITHUB_APP_INSTALL_URL=https://github.com/apps/<app-slug>/installations/new
# App setting "Callback URL": <FRONTEND_URL>/agents/connected?provider=github
```

Note `FRONTEND_URL` is read from `.env`, which is applied last and so wins
over the distribution overlay — check the host's value rather than the
overlay's when composing the callback.

The Integrations page derives the normal Connect/Reauthorize link from
`AGENT_GITHUB_APP_CLIENT_ID` and GitHub's web OAuth endpoint. The install URL
is used only after OAuth confirms that the user cannot see an existing App
installation. Keeping those two URLs separate is required for reconnects:
GitHub sends an already-installed App's `/installations/new` link to its
settings page without invoking the OAuth callback.

That same redirect is what *Configure repositories* relies on, so the install
URL stays wired to a connected user's Manage menu: it is the only route to the
page where the App's repository selection changes, and GitHub picks the user
or organization settings page itself. Set `AGENT_GITHUB_APP_INSTALL_URL` to
the `/apps/<slug>/installations/new` form — pointing it at the OAuth endpoint
instead leaves a user with no installation looping through authorization.

The same App also supplies the runner's clone credential, and the two are
*not* the same token. An installation token inherits every permission the App
holds unless it asks for less, so the clone token is requested as
`contents: read` on the single repository being worked on. Getting that wrong
would put a push-capable credential on the host that executes untrusted
repository code.

With no App configured the claim returns no clone token and the runner clones
anonymously, which is enough for a public repository. A private repository
needs the App.

A static `AGENT_GITHUB_TOKEN` still works as a fallback, but it is a
long-lived credential someone has to create and rotate; prefer the App.
With neither, the publisher loop idles and says so — jobs still run and still
produce patches, they just never become PRs.

## Running self-hosted

The runner must be a **standing service**, not a process someone starts by
hand: a queued job waits until something claims it, and "someone's laptop had
the runner up that afternoon" is how the first real job actually ran.

Opting a host in is a one-time `.env` edit:

```bash
AGENT_DISPATCHER_TOKEN=<openssl rand -hex 32>   # same value gates /worker/claim
AGENT_SANDBOX_BACKEND=container                  # host has no Kata shim yet
```

That second line is a **deliberate downgrade**, and the runner says so on every
start: it puts every job on the host's own kernel. It was the price of getting a
standing runner up before any host had Kata. Remove it once the host is
provisioned — see below — and the boundary is verified instead of warned about.

One command per host makes the runner standing; compose's
`restart: unless-stopped` plus an enabled Docker daemon carries it across
crashes and reboots:

```bash
deploy/host/sandbox_host.sh up 4    # build images, tunnel + 4 runners
deploy/host/sandbox_host.sh check   # probe the gateway from the closed network
deploy/host/sandbox_host.sh status  # replicas + recent log tail
deploy/host/sandbox_host.sh down    # stop them
```

There is one script now, not two. The co-located `agent_runner.sh` was an
overlay on the gateway's own compose stack and did not move — see
MIGRATION.md's open-decisions register. A runner on the gateway's own machine
is this same standalone compose with the tunnel pointing at localhost, which
is a configuration rather than a second file. The `preflight` verb went with
the overlay; the runner still preflights at startup and `up` prints the
verdict.

### Provisioning Kata on a runner host

An ordinary container isolates processes; every sandbox still issues syscalls
straight at the host kernel. Kata gives each job a lightweight VM with its own
kernel, so a container escape has a hypervisor behind it. That is what makes the
difference between "fine for repositories we trust" and "fine for a repository
we have never read", because the sandbox runs whatever the repository's build
and test scripts do.

The runtime belongs to the **host**, not the runner image: the runner container
holds only a Docker client and talks to the host's daemon, and it is that daemon
that has to resolve `io.containerd.kata.v2`. Nothing in the container image can
supply it. One command per host, needing root:

```bash
sudo deploy/host/setup_kata_runtime.sh
```

It is idempotent, checks the host can actually start VMs (`/dev/kvm`, CPU
virtualization extensions) before downloading anything, verifies the release
against a pinned SHA-256, and links the shim into `/usr/local/bin` so
containerd finds it without a unit-file edit. The version is pinned in the
script; upgrading is a two-line change there, and `--check` then fails on every
host still carrying the old one.

The setup also selects the release's packaged `vmlinuz-debug.container` for
the default Kata configuration. That kernel is built with Kata's reviewed
eBPF/BTF debug fragment; the ordinary guest kernel is not sufficient for the
process observer. `--check` verifies both the asset and the active config, and
runner preflight goes further: it starts a disposable sandbox with the same
non-root, capability-dropped and `no-new-privileges` shape as a real retained
guest, then exercises the same fixed privileged exec used to start the bundled
observer. Its self-test requires all three lifecycle probes to attach, executes
one short-lived child, and requires that child's fork, exec, and exit records to
arrive through the ring buffer. A missing BPF syscall, BTF image, tracepoint,
event-delivery path, or exec-time permission is therefore a deployment failure
before the runner can claim work, not an empty process list under a completed
turn. The same probe also requires the fixed tool-marker helper used by Claude's
exact boundaries. See Kata's
[debug-kernel build contract](https://github.com/kata-containers/kata-containers/blob/main/tools/packaging/kernel/README.md#debug-kernel).

Only that fixed, image-bundled observer runs as guest root. Agent CLIs and
repository code still run under their per-member UIDs with all capabilities
dropped and `no-new-privileges`; they cannot read, edit, or directly inject
records into the root-only journal. Claude's fixed non-root marker helper talks
to it over a credential-checked Unix socket. A same-UID tool can still execute
that helper, so exact per-tool association is diagnostic, never a security or
billing authority; the eBPF lifecycle census itself is independent of marks.
Kata containers retain `CAP_BPF` in the bounding set only so Docker's
creation-time seccomp profile admits the later helper syscall. Non-root agent
processes still have empty permitted and effective capability sets, and
`no-new-privileges` prevents them from promoting that bounding bit. Turn-level
CPU and memory remain conservative periodic container statistics. Process
existence is separate, event-driven telemetry: fork/exec/exit survives even
when a process begins and ends entirely between two statistics samples, and
the kernel's records carry what that process consumed — the group's peak RSS
and the bytes it read from and dirtied to storage at exit, and its scheduler
runtime once it left the CPU for the last time (tick-based CPU time reads
zero for most sub-millisecond processes, so it is not used). Those are the
per-call CPU, memory and I/O numbers under Kata: a process is charged to the
tool call whose window alone contains its start, and a process that
overlapping calls could both own, one whose thread group has not fully
settled, or any process in a census that lost events, is charged to no call
and marks those calls as partial. Work done inside the Agent CLI's own
process belongs to no call. The self-test in preflight also requires a
sub-millisecond child's records to carry non-zero CPU, a non-zero peak RSS
and a group-exit flag, so a guest kernel whose BTF does not relocate them
fails deployment rather than reporting zeros; storage and read/write byte
accounting are kernel configuration and arrive as unavailable when absent.
The per-turn table is therefore a lifecycle census of process starts observed
after that turn's cursor, not a point-in-time snapshot of resident processes
that were already running before the turn.

Requirements: an x86_64 Linux host, bare metal or with nested virtualization
enabled. Kata cannot run without KVM.

Afterwards, drop `AGENT_SANDBOX_BACKEND=container` from the host's `.env` and
redeploy. `deploy/host/sandbox_host.sh up` then gates on it (as does
whatever pipeline deploys the gateway, on its own side):

- with the default `kata` backend, the shim must be installed, **and** one real
  container started from the sandbox image must report a kernel that is not the
  host's. A runtime the daemon accepts while the sandbox still lands on the host
  kernel fails this — that is the case every cheaper signal misses.
- there is no automatic fallback to a shared kernel. Accepting one stays
  possible and stays explicit: set `AGENT_SANDBOX_BACKEND=container`, and every
  deploy restates what it costs.

The control plane independently enforces the same fact at scheduling time.
Set `AGENT_DEFAULT_EXECUTION_PROFILE=untrusted-kata-v1` for any deployment that
accepts untrusted repositories, after at least one enrolled Kata Host passes
preflight. A secure job is claimable only when all of the following are true in
the same claim transaction:

- the caller presents that Host's current per-host credential (the shared
  dispatcher token is insufficient);
- the exact runner replica reports `io.containerd.kata.v2`;
- Python preflight started the configured sandbox image under that runtime and
  observed a non-empty guest kernel different from the Host kernel.

The claim, not the Host's mutable heartbeat row, supplies the attempt's runtime,
image digest, CLI version and kernel stamp. This matters during rolling deploys,
when runc and Kata replicas can temporarily share one Host row. A missing probe,
an old runner, a custom runtime, a self-declared `kata` capability or a revoked
Host credential simply cannot claim the secure job; there is no fallback to
runc. `trusted-v1` keeps the legacy claim path only for trusted internal/dev
repositories. Existing rows migrate to that compatibility profile, while every
follow-up and fork inherits the profile already stored on its thread.
Restartable chats additionally carry the original image digest in their immutable
container labels and use that content-addressed image for any retry that occurs
before Docker returned a container id; a moved image tag cannot change the chat's
actual image while its database row still reports the old one.

### How many runners

> **Capability-gated shared-live boundary.** With both
> `AGENT_RESTARTABLE_SANDBOX=1` and `AGENT_SHARED_LIVE_SANDBOX=1`, and only on a
> host advertising `shared-sandbox-v1`, the implementation in
> `specs/2026-08-31-native-session-fork-shared-workspace-design.md` dispatches
> Claude Code, Codex, Pi, OpenCode, and Kilo turns through one host-level
> Session Supervisor. An ordinary chat owns one container/workspace and one
> live CLI transport that remains resident between compatible turns. A
> latest-settled Fork immediately invokes the pinned CLI's native fork-only
> surface, adds a second chat/session/process to that same container, and lets
> source and child run concurrently without a group turn lock. It creates no
> Docker image/container clone and sends no model prompt. An older-turn Fork
> still returns `409 historical_snapshot_unavailable` rather than silently
> choosing newer context.
>
> Group idle timeout closes every idle resident process and uses `docker stop`,
> never `docker rm`; the next member operation starts the exact container and
> lazily resumes that member. Workspace, dependencies, Agent CLI home, and
> native session data remain in the Docker writable layer. Forked chats share
> one Git/workspace/publication lineage, with only a bounded quiescent barrier
> for snapshot capture. Capture runs on a short control-plane schedule separate
> from attempt/grant reaping. A pending snapshot remains eligible after normal
> idle stop: the broker starts the exact generation/container, captures it, and
> re-arms the same group inactivity clock. Historical `legacy_1to1` containers
> remain readable and resumable, but retained Fork is rejected for them. The
> Docker-clone broker route, saga, and host implementation are absent. Keep the
> capability off for general traffic until the exact-runtime
> live-provider and Kata/gVisor conformance gates pass in the target deployment.

> In shared-live mode, the Docker socket (or mTLS Engine endpoint) is the only
> host control/data interface for chat state. `AGENT_WORKDIR_ROOT` and the
> per-job snapshot/state binds documented below are legacy-mode settings and
> are removed from the restartable supervisor. Infrastructure bootstrap
> configuration is separate; no repository, workspace, Agent CLI home, or
> session file is mapped from the host.

> The checked-in production compose remains mixed-mode while shared-live is
> default-off, so its shared runner/broker still carries the legacy workdir
> bind. Retained requests do not use that path. The local retained stack uses
> Docker-managed infrastructure volumes (and chat containers with `Mounts=[]`)
> so it exercises the Docker-only storage topology; removing the production
> legacy bind is the later retained-only cutover step.

### One chat inactivity clock (and separate safety watchdogs)

`AGENT_CHAT_IDLE_TIMEOUT_S` is the restartable chat's sole lifecycle timeout
and defaults to **1800 seconds (30 minutes)**. The preview-era
`AGENT_SESSION_IDLE_TIMEOUT_S` spelling remains a deprecated fallback. Once
the last turn is durably
settled and no interactive terminal is attached, the host Session Supervisor
issues `docker stop`; it never issues `docker rm`. A later acquire starts and
re-inspects the same labelled container. Supervisor shutdown itself leaves all
chat containers untouched. After a broker restart the supervisor does not
infer an active turn from an empty in-memory lease table. Docker observations
ride the enrolled host heartbeat;
the control plane checks the durable active-attempt fence and idle deadline,
then returns an exact generation-scoped stop command. The following heartbeat
records Docker's STOPPED state. While the broker has a proven activation, the
same heartbeat carries whether a turn/terminal/file-or-Git operation is active
and the remaining host-monotonic idle delay. A terminal carries its last
activity timestamp when it closes: a manual close is fresh activity, while
reaping an already-idle PTY continues the original 30-minute chat window
instead of stacking a second one. For a reconstructed generation-zero
RUNNING container with no durable active attempt and no deadline, the first
heartbeat contributes one fresh 30-minute recovery window; the database accepts
that window only once, so subsequent beats cannot keep extending it or stop an
active turn.

Before a prompt reaches Agent CLI stdin, the worker persists its `job_id` as
the sandbox's admission receipt. If that retained activation's attempt lease
expires, this preview stops it, marks the context LOST, and fails the job; it
does not automatically retry even when the crash may have happened just before
stdin. Any later messages already waiting behind that turn are failed in the
same database transaction, and a new follow-up receives
`sandbox_context_lost` instead of becoming a permanently unclaimable queued
job. That conservative choice prevents duplicate prompt/tool execution.

The other timeout-looking settings do not form additional chat states and do
not deactivate a healthy chat:

- `AGENT_TIMEOUT_S` defaults to 3600 seconds. It is a hard watchdog for one
  hung agent turn. A turn that reaches it is cancelled/fenced; it is never
  interpreted as an ordinary settled-idle transition;
- `AGENT_TERMINAL_IDLE_TTL_SECONDS` (30 minutes) closes a stale PTY whose
  client and process have stopped producing activity. For retained chats the
  effective PTY idle limit is the earlier of this guard and the chat clock;
  releasing it preserves the last activity timestamp and adds no second wait;
- `AGENT_TERMINAL_HARD_TTL_SECONDS` (8 hours) bounds one PTY even if it stays
  noisy forever. It is a resource/security guard, not sandbox deactivation;
- `AGENT_RESIDENCY_TTL_S` defaults to 1800 seconds and fences each member's
  live-process ownership plus renewable inference grant. Losing that lease
  demotes/restarts the member; it does not itself stop a healthy chat group or
  create another inactivity clock;
- `AGENT_WORKSPACE_TTL_S` defaults to seven days and cleans only legacy host
  worktrees. It must not address Docker-owned restartable chat state.

`AGENT_RESTARTABLE_SANDBOX=1` enables retained Docker lifecycle;
`AGENT_SHARED_LIVE_SANDBOX=1` additionally enables creation of supervisor-owned
live groups and advertisement of `shared-sandbox-v1` after exact image/runtime
attestation. `AGENT_SHARED_LIVE_RUNTIMES` defaults to
`claude-code,codex,pi,opencode,kilo`. Compatible follow-ups send only the new
prompt to the same resident process; recovery resumes the pinned native session
in a replacement process. Latest-settled Fork is immediate and runtime-native
inside the same container/workspace; older-turn forks fail closed and no new
shared-live Fork creates a Docker clone. Keep the capability off for general
traffic until the Kata/gVisor and real-provider matrix passes for every enabled
CLI. The frontend treats `NEW`, `LIVE`, `RESTORED`, and `LOST` as canonical
context-correctness states; `hot`, `warm_native`, `cold_native`, and `replay`
remain diagnostic transport/recovery telemetry.
After a shared turn settles, its snapshot and draft-PR fields may still change
asynchronously. The detail view and recent-task list poll only while that
publication state is pending/capturing/captured/publishing, and stop at
published/failed; legacy and ordinary terminal jobs do not acquire a background
polling loop.

The preview does not yet implement R6 disk-pressure admission/alerting or a
non-mutating pre-prompt context fingerprint. Keep general-traffic rollout off
until those operational gates and the Kata/live-provider matrix are complete;
the local stack below is intentionally suitable for functional development,
not evidence that those production gates have passed.

### Retained Docker storage on a host

Restartable chat state is not under `AGENT_WORKDIR_ROOT` or
`AGENT_SNAPSHOT_ROOT`. It is the Docker container writable layer, so durable
same-host operation attaches storage to the Docker daemon, not to each
sandbox. A Linux host can use this shape (device and filesystem choices remain
operator-specific):

```bash
# Mount the durable block volume before Docker starts.
findmnt --target /mnt/agent-docker

# /etc/docker/daemon.json
{"data-root":"/mnt/agent-docker"}

sudo systemctl restart docker
docker info --format '{{.DockerRootDir}}'
# /mnt/agent-docker
```

Then set `AGENT_RETAINED_DOCKER_DATA_ROOT=/mnt/agent-docker`. The workspace
broker compares that exact daemon-reported value during startup and refuses a
mismatch. It deliberately cannot prove `findmnt` itself: giving the broker a
host-root mount just to inspect storage would violate the Docker-only boundary.
Put the mount check in host provisioning/systemd (`RequiresMountsFor=` is a
reasonable additional guard) before Docker and the agent stack start.

Do not bind `/mnt/agent-docker`, `/var/lib/docker`, `/workspace`, or
`/agent-state` into the broker or sandbox. Do not use generic `docker system
prune`, container TTL cleanup, `docker rm`, or image garbage collection against
`org.freeinference.chat-sandbox=true` objects. Inspect and operate them through
Docker (`docker inspect`, `docker exec`, `docker cp`, and the broker RPC), not by
walking overlay directories. Backup or move the whole Docker data-root only
with the daemon stopped and using Docker/storage-driver-supported procedures.

This makes `docker stop`/`docker start` survive runner, broker, Docker daemon,
and host reboots as long as the same Docker data-root returns. It does **not**
make a missing container recoverable on another host: export to object/cold
storage and cross-host restore remain the explicitly deferred persistent
checkpoint phase. Until that lands, loss of the Docker data-root is context
`LOST`, and the control-plane database still supplies the authoritative
thread→host→container identity needed to locate it.

A missed heartbeat, an offline/disabled host row, or a temporarily unreachable
broker is not evidence that this Docker state vanished. Keep the chat pinned
and unavailable, recover or re-enrol the same named host, and reconcile its
exact labelled container. Only an inspection on that owning Docker daemon that
proves the expected container/data-root is gone may transition the context to
`LOST`; never create a blank replacement on another host.

**One runner runs one job at a time.** It claims, runs the job to completion,
then claims the next — so the replica count *is* how many jobs the deployment
can run concurrently, and everything else waits in the queue. With one
replica, two users are serialised; a job that runs to its hour-long timeout
holds up everyone behind it.

Set it with `AGENT_RUNNER_REPLICAS` (default 3). Replicas need no
coordination — `claim_job` uses `FOR UPDATE SKIP LOCKED`, so they share one
queue with no leader and no sharding — and each job's sandbox is capped at
4g / 2 cpus, so budget roughly that per replica.

Do not scale with a `--scale` flag instead: it survives exactly until the next
`docker compose up` without it, which drops the fleet back to one and presents
as "every user is queueing" long after anyone remembers scaling it.

Queue order is global FIFO with no per-user fairness yet, so one user
submitting a batch can occupy every replica. Per-user concurrency quotas are
P1 work.

The script validates the required environment before starting and surfaces
the runner's own preflight verdict (gateway reachable, image spawnable,
workdir bind-mountable, egress network resolvable) instead of leaving it in a
detached log. Equivalent by hand:

```bash
# Add `--build-arg CLAUDE_CODE_VERSION=<exact version>` (and the equivalents for
# codex / pi / opencode / kilo) to run a version other than the Dockerfile's.
# `sandbox_host.sh` does this for you from the host's .env.
docker build -f deploy/sandbox/Dockerfile.agent-sandbox \
             -t hybridinference-agent-sandbox:latest .

AGENT_SANDBOX_IMAGE=hybridinference-agent-sandbox:latest \
  docker compose -f deploy/host/docker-compose.sandbox.yml \
               up -d --scale runner=4
```

Scaling is only `--scale`: `claim_job` uses `FOR UPDATE SKIP LOCKED`, so
runners share one queue with no leader and no sharding.

| Variable | Default | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | `kata` | `process` (no isolation) refuses to start unless `AGENT_SANDBOX_ALLOW_UNISOLATED=1`. On the default `kata`, deploy refuses until the host has the shim and one real container proves it gets its own kernel; `container` is an explicit, and loudly restated, shared-kernel downgrade |
| `AGENT_SANDBOX_IMAGE` | required | Image built from `Dockerfile.agent-sandbox` or an equivalent deployment-owned image |
| `CLAUDE_CODE_VERSION`, `CODEX_VERSION`, `PI_VERSION`, `OPENCODE_VERSION`, `KILO_VERSION` | Dockerfile default | Which agent CLI version this host bakes into its sandbox image. Blank uses the reviewed pin — set one only to run something else here (staging soaking a new CLI, or a rollback). Must be an *exact* version: the build refuses a dist-tag or a range, because the pin is derived into the image labels that the heartbeat reports, the claim stamps onto every attempt, and the checkpoint gate compares before resuming a thread's session. Bumping one changes what the event parser must handle — re-run the agent-loop conformance suite |
| `AGENT_SANDBOX_NETWORK` | `agent-egress` | Declared `internal: true`, so a sandbox reaches the gateway and nothing else. Also the `platform_only` network unless `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` overrides it |
| `AGENT_EGRESS_SETUP_TIER` / `_AGENT_TIER` | `platform_only` | One of `platform_only` / `trusted` / `custom` / `full`, **per phase**. The design's external-beta shape is setup=`trusted`, agent=`platform_only`; the overlay ships both closed because there is no setup phase yet and no allowlist-fronted network to run one on |
| `AGENT_EGRESS_NETWORK_*` | — | Network per tier. A tier with no network is an error when a phase selects it, never a fall back to a more open one |
| `AGENT_SNAPSHOT_ROOT` | — | Where setup snapshots live. A snapshot holds only what the setup script added to the worktree — never the checkout — and is keyed by repository, script, and sandbox image. Unset disables caching, so every job reinstalls. Bind it at the same path inside and out, like the worktrees |
| `AGENT_SNAPSHOT_TTL_S` | `604800` | Seven days, as the design specifies. A stale entry means a wrong dependency tree |
| `AGENT_EGRESS_ALLOWLIST` | — | Checked at startup: it may not contain an agent vendor's telemetry domain, which would let a "closed" sandbox report on the repository it was given |
| `AGENT_WORKDIR_ROOT` | `/var/lib/hybridinference/agent-jobs` | **Legacy/default-off mode only:** a host path bind-mounted at the same path inside the mixed-mode runner/broker. Legacy preflight test-mounts it and fails at startup if unavailable. Restartable chat containers never mount or address it; remove the bind only at retained-only cutover. |
| `AGENT_RUNNER_HOST` | — | Names the *machine*, shared by its replicas: the unit the admin host switch picks between. `deploy/host/sandbox_host.sh` fills it from the host's own name. Never derived inside the container, where the hostname is a container id. Unset keeps the machine out of the pool and changes nothing else |
| `AGENT_SANDBOX_UID` / `_GID` | `10001` | Only for a custom sandbox image; must match its user |
| `AGENT_REPO_ALLOWLIST` | — | Comma-separated `owner/name`, or `owner/*`, for the single-tenant dogfood. Users who connect the App themselves do not need it; unset simply means the only entitlement is a user's own connection |
| `AGENT_GITHUB_APP_CLIENT_ID` / `_CLIENT_SECRET` | — | The App's OAuth half. Only the user-facing connect flow needs it; minting installation tokens uses the private key alone |
| `AGENT_GITHUB_APP_INSTALL_URL` | — | GitHub App installation URL, `https://github.com/apps/<slug>/installations/new`. Used when OAuth finds no accessible installation, and behind *Configure repositories* for a connected user. The gateway adds a short-lived, user-bound `state`; set the App callback to `<frontend>/agents/connected?provider=github` |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` | — | GitLab.com OAuth application credentials, kept on the gateway. Configure only `read_user` and `read_api` scopes |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | — | Exact registered callback, normally `<frontend>/agents/connected?provider=gitlab`. HTTPS is required except for localhost development |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK` | — | Accepts a non-`internal` sandbox network. Preflight refuses one otherwise, so a missing setting cannot quietly mean full egress |
| `MCP_CONFIG_PATH` | — | **Gateway-side.** The deployment's MCP server registry, read where the proxy runs. Unset, or a path that does not exist, means no MCP servers. Copy that repository's `config/examples/mcp.example.yaml` |
| `AGENT_GITHUB_TOKEN` | — | Gateway-side; unset means the publisher idles |
| `AGENT_PUBLISH_BASE_BRANCH` | `dev` | What a draft PR targets when its job named no branch of its own. A job started from a branch targets *that* branch; this is only the fallback |

The authenticated `/agents/integrations` page is the only supported place to
start either OAuth flow. OAuth state is unpredictable, bound to the signed-in
user and provider, expires after ten minutes, and is consumed once. GitLab also
uses PKCE. GitLab access and refresh tokens are encrypted before persistence,
rotated on refresh, never returned to the browser, and revoked best-effort on
disconnect.

GitHub repositories retain the complete Agent lifecycle: checkout, branch
discovery, push, and draft PR publishing use short-lived GitHub App installation
tokens. GitLab is intentionally narrower in this release: it verifies the
authenticated GitLab user and shows their accessible projects for discovery.
GitLab projects do not appear in the Agent task composer, and the gateway does
not claim to read their source or publish GitLab merge requests yet.

### Which machine runs the jobs

**Admin → Settings → Cloud Agent Host.** The list is every machine that has
polled for work, and the radio button picks the one whose runners may claim.
"Any host" is the default and means what it always meant: whoever polls first
takes the job.

Runners *pull*, so this is a gate on the claim rather than a dispatch target —
the gateway cannot push a job at a machine, and the only moment it gets to say
"not you" is when a runner asks for work. Three consequences worth knowing
before using it:

- **The switch is not preemptive, and not a drain.** The new host starts
  claiming immediately; jobs already running on the old one keep running to the
  end, holding their leases. The two overlap — a drain would mean waiting for
  the old host to empty first, and this does not do that. Expect the old host
  to stay busy for as long as its longest running job.
- **A runner reporting no host is refused while any host is pinned.** Failing
  closed is deliberate: leaving the machine you just switched away from able to
  claim would make the switch a lie. A runner without `AGENT_RUNNER_HOST` set
  therefore stops taking work the moment anything is pinned.
- **A machine joins the pool by polling, not by being registered.** Start a
  runner there and it appears within seconds — including while it is being
  turned away, which is what makes it selectable in the first place. Pinning to
  a name nothing has ever polled from is refused, because the symptom is a
  queue that hangs with nothing in the logs.
- **A host name is a scheduling label, not a machine identity.** The runner
  reports it, so anything holding `AGENT_DISPATCHER_TOKEN` can report any name;
  two machines configured alike are one entry in the list. The credential is
  the actual boundary. Use this to decide where *our* machines run *our* jobs,
  and do not build anything on it that must survive a hostile runner.
- **There is no failover.** Pinning turns the other machines off, so if the
  pinned host's runners stop, the queue stops with them — nothing takes over.
  The admin page warns when the pinned host has not polled recently, but that
  signal is imperfect in the other direction too: `last_seen` is refreshed by
  claims alone, so a machine whose runners are all busy on long jobs looks
  exactly like one that is down. An independent heartbeat is the fix and is not
  built yet.

Adding a *second* machine is not only this switch. A sandbox reaches the
gateway over a network declared `internal: true`, which resolves nothing off
its own host — so a runner box that is not the gateway's host needs the
routable-network configuration from
[§0 above](#0-a-closed-agent-network-needs-the-gateway-on-it) (or its own
gateway). Preflight refuses at startup rather than failing every job at its
first model call, but it refuses *there*, on that machine, not here.

### Running runners on another machine

A second machine is worth adding for capacity, or because it is the one with
the hardware. What makes it more than "run the script over there" is that a
sandbox reaches the gateway over a network declared `internal: true` — no route
off the host at all — which works only because the gateway is normally a
container on that same network. On a machine with no gateway, that network is
empty and every job dies at its first model call.

The answer is a **fixed-destination tunnel on the closed network**:

```text
runner host                                    gateway host
┌───────────────────────────────────┐
│ [sandbox] ─┐                      │
│            ├→ gateway-tunnel      │──ssh──→ 127.0.0.1:8080
│ [runner] ──┘        ↑             │
│   ✗ internet   one address, fixed │
└───────────────────────────────────┘
```

The sandbox's world is unchanged: one endpoint, ours. The tunnel is a relay and
not a proxy — no CONNECT, no caller-chosen destination — so nothing the agent
sends can widen it.

**Why ssh rather than exposing the gateway's port.** Every claim carries
`AGENT_DISPATCHER_TOKEN` and every job call carries that job's capability
token. Binding `8080` to the lab network puts both in cleartext on a shared
segment. The tunnel keeps the gateway listening on loopback, which is where it
already listens.

```bash
deploy/host/sandbox_host.sh up 4     # tunnel + 4 runners
deploy/host/sandbox_host.sh check    # probe the gateway from the closed network
deploy/host/sandbox_host.sh status
deploy/host/sandbox_host.sh down
```

`deploy/host/docker-compose.sandbox.yml` is standalone and contains
only the runners, their workspace broker, the tunnel, and the two networks —
no `backend` service. That was the point of it in the source repository, where
the alternative overlay declared `depends_on: backend` and, pointed at a
machine with no gateway, did not fail but started one there. It is the only
topology this repository ships.

| Variable | Notes |
|---|---|
| `AGENT_TUNNEL_SSH_DESTINATION` | `user@gateway-host`. The only address anything on this machine can reach through the tunnel |
| `AGENT_TUNNEL_SSH_KEY` | Private key, mounted read-only. Give it its own key and restrict it on the gateway: `command="",restrict,port-forwarding,permitopen="127.0.0.1:8080"` — a runner host needs one forwarded port, not a shell |
| `AGENT_TUNNEL_SSH_KNOWN_HOSTS` | The gateway's host key. **Required**, and host checking is not disableable: an unverified hop would hand the dispatcher credential to whatever answers on that address, which is the attack encrypting it is meant to prevent |
| `AGENT_TUNNEL_REMOTE_PORT` / `_HOST` | Where the gateway listens *on its own machine*; `127.0.0.1:8080` by default |
| `AGENT_GATEWAY_HOSTNAME` | The name both the runner and the sandbox resolve the tunnel by, `agent-gateway` by default. **Do not override `AGENT_GATEWAY_URL` with the gateway's real address:** the runner would reach it and the sandbox, on the closed network, would not — jobs would be claimed and then die at their first model call |

Generate the known_hosts entry deliberately, and look at it:

```bash
ssh-keyscan -p 22 <gateway-host> > /etc/agent-tunnel/known_hosts
```

Then `sandbox_host.sh check` answers the only question that matters, from
the only vantage point whose answer means anything — a container on the closed
network, asking the gateway for `/health`. Reaching the gateway from the host's
own shell proves nothing about what a sandbox can reach.

Once it is up, the machine appears in **Admin → Settings → Cloud Agent Host**
and can be selected there.

#### The way back: the control plane reaching this host's broker

The tunnels above all point one way — out of this machine. One thing needs to
come the other way.

Every file listing, terminal keystroke and `git status` for a job goes **control
plane → workspace broker**, and the broker runs here, beside the runner that
owns the worktree. The control plane resolves which broker from the host row,
which holds whatever the runner reported at enrolment. On a co-located
deployment that report is `http://workspace-broker:8092` and it works,
because the control plane is on this network too.

On a separate machine it is a name that resolves nowhere. The control plane
stores it, dials it, and gets a DNS failure it surfaces as **503** — including
on `terminals/suspend`, which the runner calls during a normal job. The job
fails there, the lease expires, the next attempt fails at the same point, and
the job runs out of attempts. That is not a hypothetical; it is what the first
real job on this deployment did, three times.

The route cannot be opened from the other side: the control plane's machine
generally cannot reach this one at all. So it rides the connection this machine
already opens — a reverse forward on the control-plane tunnel:

```text
  runner host                                control plane host
┌──────────────────────────┐              ┌──────────────────────────┐
│ workspace-broker         │◀─── -R ──────│ 172.24.0.1:18092         │
│        :8092             │              │  (docker bridge gateway) │
│                          │              │        ▲                 │
│ control-plane-tunnel ────┼──── ssh ────▶│        │ control-plane   │
└──────────────────────────┘              └────────┴─────────────────┘
```

| variable | |
|---|---|
| `AGENT_BROKER_REVERSE_BIND` | The **control plane host's docker bridge gateway**. Not `127.0.0.1`, which its containers cannot see; not `0.0.0.0`, which publishes the broker to that machine's whole network behind one shared token. Find it with `docker network inspect <net> --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'` |
| `AGENT_BROKER_REVERSE_PORT` | A free port on that host |
| `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` | `http://<bind>:<port>` — what the runner reports, and what the control plane will dial |

A non-loopback remote bind requires `GatewayPorts yes` or `clientspecified` in
the **control plane host's** sshd. Without it the bind is refused and, because
the tunnel runs with `ExitOnForwardFailure`, ssh exits and the container
restarts rather than sitting there having bound nothing.

Set all three or none. An advertise URL with no tunnel behind it is an address
nothing answers on, which is the same 503 with a longer explanation.

Check it from the control plane's side, which is the only side whose answer
means anything:

```bash
docker exec <control-plane-container> \
  python -c "import urllib.request; print(urllib.request.urlopen('http://<bind>:<port>/health', timeout=5).status)"
```

**401 is the good answer.** It means the broker replied and refused an
unauthenticated request; the route is what was being tested. A DNS error or a
timeout is the failure this section exists for.

### Three things about this topology that look like details and are not

**The runner is not the gateway image.** It builds from
`Dockerfile.agent-runner`, which adds a Docker client (the container backend
shells out to `docker` to start each sandbox) and `git` (the runner checks the
repository out). The gateway image has neither, and a runner built from it
fails preflight on every host.

**The job worktree must be a host path with the same name on both sides.** The
runner asks the daemon to bind-mount each job directory into the sandbox, and
the daemon resolves that path on the *host* — not inside the runner container.
A named volume works for the runner's own file operations and then fails every
`docker run --mount` with exit 125.

**The runner runs as root, and that is the design.** It creates each job
worktree, checks the repository out, and then chowns the tree to uid 10001 —
the sandbox image's user — before mounting it. Skip that and the agent cannot
write a single file, and git refuses to look at the repository at all
("dubious ownership"). An unprivileged runner falls back to making the tree
world-writable and logs `agent_sandbox_workdir_world_writable`; that is only
defensible on a dedicated single-purpose host.

The isolation boundary is between the runner and the sandbox it starts, not
around the runner. Running the runner unprivileged does not buy isolation — it
already holds the dispatcher credential and the Docker socket.

### Dependency installation, and the proxy that allows it

A job may carry a `setup_script`. It runs before the agent, under its own
egress tier, and its result is cached per repository and script — so a retry
does not reinstall.

The tier is where the work is. `setup` defaults to `trusted` and `agent` stays
`platform_only`, which in this deployment means three networks:

| Network | Internal | Who is on it |
|---|---|---|
| `agent-egress` | yes | the sandbox during the agent turn, and the gateway |
| `agent-egress-trusted` | yes | the sandbox during setup, and the proxy |
| `agent-egress-uplink` | **no** | the proxy, alone |

Both sandbox networks have no route of their own. The difference is that
`agent-egress-proxy` sits on the trusted one with a second leg, and the sandbox
is handed `http_proxy`/`https_proxy` pointing at it. The environment variables
are a convenience for package managers, not the boundary: a setup script that
ignored them finds a network that cannot route anywhere.

The agent turn — the part driven by untrusted model output — still reaches the
gateway and nothing else. That does not change when setup is opened.

**Adding a domain.** `AGENT_EGRESS_ALLOWLIST` is *added to* the built-in
registry list (PyPI, npm, crates, Go, RubyGems, Maven, github.com), never a
replacement for it. `*.example.com` covers subdomains. An agent vendor's
telemetry domain is refused outright — a sandbox that can phone home about a
private repository is not closed, whatever the tier is called. The list is
rendered into the proxy's config by `agent-egress-proxy-config`, a one-shot
service that runs before the proxy and fails the deploy if the list is
unusable. Editing that config by hand skips the validation that keeps a
hostile value out of it.

**Three preflight failures and what each means.** The runner refuses to claim
jobs unless the setup tier is genuinely enforcing:

- *"the sandbox network … is not `internal`"* — a tier variable points at a
  routable network. The setup phase would have the whole internet while the
  config still said `trusted`.
- *"egress proxy is unreachable"* — the proxy is not running, or not on that
  network. Check `docker compose logs agent-egress-proxy`; a config Squid
  rejects shows up there as a parse error on the first line. This one stops the
  runner claiming **any** job, including jobs that install nothing — deliberate,
  because it means the deploy is broken, but if this host genuinely does not
  need dependency installation, `AGENT_EGRESS_SETUP_TIER=platform_only` skips
  the tier and its probe entirely.
- *"egress proxy answered … it is not enforcing an allowlist"* — the proxy
  served a host that does not exist and is on no list. Treat this as an open
  proxy on the sandbox's network.

**Where a denied request is visible.** Only in the proxy's log — an internal
network refuses a connection silently, so before this there was nowhere to
observe a blocked attempt from. `deploy/host/sandbox_host.sh status`
tails it;
a denial is the line with `verdict=TCP_DENIED/403`.

**What this does not give you.** No secrets reach the sandbox (there is still
no secrets store, by design), the sandbox runs unprivileged so `apt install`
does not work — use `pip --user`, a virtualenv, `npm ci` — and a tool that
ignores the proxy environment (bun, at the time of writing) cannot reach a
registry at all.

## Runtime verification

Run the adapter contract tests after changing runtime integration code. They
replay recorded and synthetic events to pin normalized mapping without a live
model or deployment credential:

```bash
uv run pytest tests/unit/test_agent_runtimes.py
```

The MCP suites (`test_agent_mcp.py`, `test_agent_mcp_api.py`) stayed in the
gateway repository with the proxy and the registry, which is where an MCP tool
call is still authenticated. Run them there after touching either side of the
adapter contract.

These tests do not invoke the installed agent binaries. An installed-CLI smoke
test and full live-model matrix belong to the deployment operating those
binaries, models, and credentials. Keep that conformance configuration in the
deployment overlay, not in the neutral upstream repository.

## Known gateway findings

Surfaced by the conformance suite, tracked separately from this feature:

- An upstream mid-stream disconnect is masked with a synthesized
  `finish_reason: stop` + `[DONE]`, so a client cannot detect truncation. For
  agent workloads that is a poisoning vector: truncated tool-call arguments
  look complete.
- An in-stream 429 error frame is typed `server_error`, so a client branching
  on `type` to decide whether to back off misclassifies a rate limit.

## Model behaviour observed on the first real run

`deepseek-v4-flash` replied *"I've added a docstring to the `greet` function.
It now reads: ..."* immediately after receiving a tool result with
`is_error: true` saying the write had been denied. Nothing had changed on
disk.

This is why the runner reports what the tools did rather than what the agent
says about them: it collects errored `tool_result` entries, emits them into
the owner's stream, and refuses to record a run as successful when it produced
no patch *and* a tool failed. A model's own account of its work is not
evidence.

## What changed when this moved

Task H5 moved this page from the source repository's
`docs/developer/agent-sandbox-operations.md` at the freeze commit `764a6f97`.
Under the migration's move-as-is rule the edits are **paths and the artifacts
that did not move** — nothing else was rewritten, reordered or brought
up to date. Every deviation from the source text:

| Source | Here | Why |
|---|---|---|
| `ops/setup/setup_kata_runtime.sh` | `deploy/host/setup_kata_runtime.sh` | D7 |
| `ops/deploy/sandbox_host.sh` | `deploy/host/sandbox_host.sh` | D7 |
| `deploy/docker/Dockerfile.agent-sandbox` | `deploy/sandbox/Dockerfile.agent-sandbox` | D7 |
| `ops/deploy/agent_runner.sh up/preflight/status/down` | the standalone script's `up/check/status/down` | The co-located overlay was dropped (MIGRATION.md, open decisions, decided 2026-08-04). It has no `preflight` verb; the runner preflights at startup and `up` prints the verdict. |
| `docker compose -f docker-compose.yml -f docker-compose.agent-runner.yml` | `docker compose -f deploy/host/docker-compose.sandbox.yml` | Same decision. There is no base compose here and no `backend` service. |
| "Use that script, not `agent_runner.sh`" | a note that the standalone compose is the only topology | Same decision. |
| The staging deploy-pipeline paragraph (`deploy_staging.sh`, the same-invocation requirement, the overlay attaching `backend` to the agent-egress network) | removed | It describes the *gateway's* deploy pipeline driving the overlay that did not move. Keeping it would be an instruction nobody here can follow. |
| `AGENT_RUNNER_HOST` row naming `agent_runner.sh` and `deploy_staging.sh` | names the standalone script | Same. |
| `MCP_CONFIG_PATH` / `config/examples/mcp.example.yaml` | marked gateway-side | The MCP registry and proxy stayed on the gateway: a tool call is authenticated with the same capability token as a model call. This service only names servers by id. |
| `pytest … test_agent_mcp.py test_agent_mcp_api.py` | `pytest tests/unit/test_agent_runtimes.py` | Those two suites stayed with the proxy. Run them in the gateway repository. |

### What did *not* change, deliberately

- **The Verified / Not verified tables.** They record what was measured against
  the pre-split system on specific dates. Re-asserting them for the new stack
  would be a claim nobody has tested; re-verification is E11's and H2's job, not
  a documentation edit's.
- **"The publisher runs in the gateway", and similar.** True when written. After
  the split the store, the reaper and the publisher run in *this* service's
  control plane, and the gateway keeps inference, the MCP proxy, and identity.
  The security property the sentence is making — the component holding a
  write-capable GitHub token is outside the sandbox — is unchanged by the move.
- **`/var/lib/hybridinference/agent-jobs`, `hybridinference-agent-sandbox`, and
  the other inherited names.** Renaming remains a separate pull request.
  `AGENT_WORKDIR_ROOT` must stay unchanged for legacy worktrees during the
  mixed-mode service cutover (see [cutover.md](cutover.md) step 6), but
  restartable chats live only in Docker writable layers and do not depend on
  it.
- **Anything the split itself changes** — the control plane's own URL, host
  enrollment, inference grants. Those belong in [cutover.md](cutover.md) and in
  `ENVIRONMENT.md`, not in a page that was moved rather than rewritten.
