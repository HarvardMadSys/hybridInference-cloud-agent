<!--
Moved from the source repository at the freeze commit `764a6f97`
(`docs/developer/agent-sandbox-operations.md`), task H5. The provenance table
at the end records the move-only baseline. Later extraction and retained-
sandbox work amended this page to describe the service that now ships here.
"The gateway" means the FreeInference inference/identity service; the job API,
store, publisher, MCP relay and admin host switch belong to this service's
control plane.
-->

# Cloud agent sandbox — operations

What is on `dev` today, what is verified, and the exact steps left before a
job can run for real. This page is about operating it; design rationale lives
in the project's issue history.

For the pre-drain Kata check, old-release migration regression, peer-failure
handling and queue alerts, see [deployment reliability](deployment-reliability.md).

## What a job does

```text
POST /v1/agent/jobs           owner queues work
  → enrolled host claims       exact shared-live capability attestation
    → runner creates/starts     one retained Docker group and its writable layer
      → native Agent CLI        one live process/session per member chat
      → normalized events       streamed to the owner over SSE
      → model calls             billed through the gateway into api_logs
    → quiescent group snapshot  immutable diff; the agent never pushes
  → publisher validates         .github/ block, secret scan, path escape, size
    → agent/<origin-chat-id>     one stable branch per workspace group
      → draft PR                a human reviews before anything merges
```

Two properties are load-bearing and easy to break by accident:

- **No repository credential ever enters the sandbox.** The host captures a
  quiescent group diff; the publisher — running in the control plane, outside the sandbox — is the
  only component holding a GitHub token that can _write_. The runner does hold
  a clone credential, but it is read-only, scoped to the one repository, and
  deliberately never recorded in `.git/config`: the checkout adds no remote,
  the credential travels in git's environment rather than its argv, and the
  runner refuses to start the agent if it finds the token anywhere under
  `.git/`.
- **The sandbox has two narrow, non-interchangeable credentials.** The
  gateway-issued `agr` grant buys inference as the chat owner: it names the
  allowed model, is scoped to one member residency, and is renewed only while
  that exact host/member generation keeps heartbeating. Spend lands against
  the owner's gateway daily quota. This service separately mints an
  attempt-fenced `mcp` token that reaches only the MCP relay and only the
  servers recorded on the job. **There is no per-job spend cap** — C5/C6
  removed it deliberately,
  so do not quote one as the bound on a leaked grant. Neither credential can
  write the event log, upload a replacement patch, or move the job to a
  terminal state; the trusted runner holds the token that can.

### Automatic task titles

New conversations initially use the first meaningful prompt line, shortened to
36 characters. After a claim is dispatched, conversations that still need a
title get a background request to `GATEWAY_BASE_URL` using that attempt's
existing inference grant and selected model. This is a separate request, not a
CLI turn: it does not alter the task prompt or conversation. It always summarizes
the conversation's original prompt, including when a later turn recovers naming.
Each call uses at most 4,000 input characters and 256 output tokens, has a
10-second deadline, and is attributed to the same owner and current job.

The generated title summarizes the task in the user's language and retains
relevant identifiers. The proxy's `⏳ The model is starting up` response is a
model-server warmup status, not a generated title, even when the gateway returns
it as an HTTP 200 completion. Warmup responses, transport failures, and transient
HTTP errors get up to five calls with backoff, within a 120-second total deadline.
Other malformed responses, including reasoning-only output, leave the fallback.
If this window is exhausted, a later claim can try again; there is no idle
background regeneration. The UI refreshes eligible titles for up to 130 seconds
without waiting for the running turn to finish, and notifies navigation lists
when a title arrives after the conversation becomes idle.

Manual renames always take precedence, including a rename to the same text;
each retry rechecks title ownership and the attempt fence. Follow-ups keep a
valid conversation name, and forks inherit it. Legacy automatic or inherited
titles containing the warmup status can recover on their next claim. Other
existing titles remain unchanged, and no title data migration is required.

### Conversation status and run history

The conversation UI shows four states: **Queued**, **Running**, **Idle**, and
**Failed**. Any running turn takes precedence over queued turns; when no turn
is active, the latest turn determines whether the conversation is failed or
idle. Completed and user-stopped turns both leave the conversation idle.
Opening an older turn does not change this conversation status. The owner API
reports it as `conversation_state`, independently of the selected job's `state`.

Each turn keeps its own completion, failure, or cancellation result. Execution
history is collapsed by default and appears only when that turn has multiple
attempts. Admin run details show the server and timestamps directly, with past
attempts available for inspection. Selecting history changes the records being
viewed; it does not restart a task or change its server. These display rules do
not change task scheduling or retry behavior.

## Verified

Against real components on a developer machine:

|                                                                              | How                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Store: fencing, reaper, retained-group snapshot publish                      | dbtests against Postgres                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Job API, SSE resume, worker endpoints                                        | control-plane API and server suites                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Per-job inference grant: attempt fencing ~~, budget 429~~                    | live gateway. The struck half exercised a per-job spend cap that no longer exists: C5/C6 replaced it with the owner's gateway daily quota, which this service does not enforce and so cannot verify here                                                                                                                                                                                                                                                                                                               |
| Retained runner + runtime adapters                                           | Docker-only local product stack with the exact Claude Code, Codex, Pi, OpenCode, and Kilo pins: every CLI passed `NEW → LIVE → docker stop → RESTORED`                                                                                                                                                                                                                                                                                                                                                                 |
| Native Fork concurrency                                                      | every pinned CLI created a distinct child native session immediately; source and child were simultaneously `RUNNING` against one container/workspace and both crossed the local inference barrier                                                                                                                                                                                                                                                                                                                      |
| Publish: patch → branch → draft PR                                           | real `git push` to a local bare repo                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| **A real job on a real model**                                               | live Claude Code CLI × `deepseek-v4-flash` through the production gateway: docstring landed on disk, patch produced, 3 calls / 74,523 tokens / $0.0075 attributed to the job in `api_logs`                                                                                                                                                                                                                                                                                                                             |
| Retained Docker lifecycle                                                    | real mount-free (`Mounts=[]`) chat containers keep one writable layer and container id across group idle `docker stop` and later `docker start`; the phase bridge is released while stopped and recreated on wake, and publication capture replaces neither                                                                                                                                                                                                                                                            |
| Source import → edit → snapshot                                              | the trusted runner imports a credential-scrubbed source archive into `/workspace`; real Git status/diff and the quiescent group publication snapshot complete without a host worktree or snapshot bind mount                                                                                                                                                                                                                                                                                                           |
| Sandbox process boundary                                                     | real Docker: non-root Agent CLI member UIDs, dropped capabilities, and closed agent egress; production multi-tenant Kata/gVisor remains a deployment gate below                                                                                                                                                                                                                                                                                                                                                        |
| The control plane on staging                                                 | `/v1/agent/*` live: job create/get/list/cancel, SSE stream, and an unauthenticated host refused at `/worker/claim`                                                                                                                                                                                                                                                                                                                                                                                                     |
| MCP: per-user resolution, relay authorization, tool filtering                | unit + API tests against a mocked upstream: one user's credential never resolves for another, a job cannot reach a server it was not granted, a dead fence loses tools, and a blocked tool never leaves the relay                                                                                                                                                                                                                                                                                                      |
| **MCP: what `--strict-mcp-config` (hybridInference#1129) actually prevents** | the flag was added on the strength of the CLI's documented behaviour; this is the measurement. Real Claude Code 2.1.220 in a job's exact configuration (headless, `bypassPermissions`, cwd = a checkout carrying a `.mcp.json`): without the flag the CLI reports the repository's server alongside the platform's, with it only the platform's. No approval step was involved either way                                                                                                                              |
| MCP: the adapter's `--mcp-config` is accepted by the real CLI                | the argv `ClaudeCodeRuntime.prepare` emits, run against the real binary: the platform's server appears in `system/init`, and a deliberately bogus flag in the same argv fails instantly, so the flags are being parsed rather than ignored                                                                                                                                                                                                                                                                             |
| MCP: the token can stay out of argv                                          | the same run with the header written as `Bearer ${AGENT_MCP_TOKEN}` and the variable exported: the CLI expanded it and the proxy authenticated the request, while the command line carried only the placeholder                                                                                                                                                                                                                                                                                                        |
| **MCP: the proxy against a real hosted server**                              | the shipping router and filters against live `mcp.deepwiki.com` (which answers over SSE, so the streaming filter is the one that ran): `initialize` → `serverInfo: DeepWiki`, `tools/list` → its three real tools, `tools/call read_wiki_structure` → real documentation for `psf/requests`. Measured before the deployment-wide registry was replaced by the per-user catalogue (hybridInference#48); `deepwiki` is not a catalogue entry today, so this records the relay path rather than a shippable configuration |
| MCP: the allowlist, on a real catalogue                                      | same server, `tools: [read_wiki_structure]`: `tools/list` returned that one tool of the three, and `tools/call ask_question` came back `-32601` without the request leaving the relay. Same caveat as the row above: the tool-name allowlist path is real but no current catalogue entry uses it — `github` declares `read_scope="endpoint"` instead                                                                                                                                                                   |
| MCP: the authorization matrix, end to end                                    | live token + granted server 200; live token + a server the job was not granted 403; a superseded attempt 401; an ordinary `hyi-` key 401; no credential 401                                                                                                                                                                                                                                                                                                                                                            |
| MCP: Claude Code driving the proxy                                           | the real CLI, pointed at the proxy, completed the whole handshake through it — `initialize`, `notifications/initialized`, the `GET` stream, `tools/list` — each reaching deepwiki and returning 200                                                                                                                                                                                                                                                                                                                    |
| **A self-hosted runner completing a real job**                               | `ajob_04de5d509a25ded2` against staging: claimed → cloned `psf/requests` at `414f0513` (verified equal to that repo's HEAD) → Claude Code 2.1.220 in a Docker sandbox as uid 10001 → `Read` + `Edit` with `is_error: false` → 518-byte patch stored → `succeeded`. The patch was read back and matches the file the agent left on disk                                                                                                                                                                                 |

## Not verified

Be precise about these when reporting status:

- **The full model matrix.** One real job on one real model is verified (see
  above); the 2-runtime × 3-model matrix has not been run, so cross-model
  behaviour differences are still unknown.
- **No real _job_ has used MCP.** The proxy is verified against a live server
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
- **The deleted `agent-job-runner` workflow has never executed on GitHub.** It
  was the source repository's disposable runner (see §1 below) and no longer
  exists here, so it can never run. This repository's own Actions do run:
  green `dev` commits publish neutral releases, and CI's
  `Build control-plane` job builds the image.
- **This service's production deployment.** `/v1/agent/*` belongs to this
  service, so whether it answers in production is a fact about this
  deployment. The 404 this bullet used to report was the _gateway's_ agent
  routes — a different service, deleted 2026-08-09 — never these. Deployment
  state is owned and recorded by each downstream distribution. The generic
  artifact interface is `deploy/release/README.md` and
  `deploy/DEPLOY_PROTOCOL`; read those rather than inferring a named
  environment's state from this page.
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
  comes back reporting a kernel that is _not_ the host's;
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

  | `--runtime`                | Result                                                                                                                          |
  | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
  | unset (runc)               | runs                                                                                                                            |
  | a name that does not exist | `unknown or invalid runtime name` — the daemon rejects it, so the flag is reaching the daemon and is not being silently dropped |
  | `io.containerd.kata.v2`    | `failed to start shim` — the name is accepted and the shim is attempted; only the binary is missing                             |

  A dropped flag would be the dangerous failure: the kata backend would
  silently run as a plain shared-kernel container while every log line and
  config said otherwise. That is ruled out.

## Who may run a job against which repository

Two independent entitlements, and a repository qualifies on either. Both fail
closed: with neither, no job can be created at all.

**A connection the user made.** They authorize the GitHub App, GitHub returns a
code to `<frontend>/agents/connected`, and the platform exchanges it for a
token that speaks _as that user_ to ask which installations they can reach.
Those installation ids are recorded against the user, and the repositories they
cover are what the composer offers. Every step is GitHub's answer rather than
the requester's — which is the point, because the requester chooses the
repository and the _platform_ mints the credential for it. Anything the
requester could simply assert would be a confused deputy.

**The deployment allowlist** (`AGENT_REPO_ALLOWLIST`), for the single-tenant
dogfood where the repository is the operator's own and there is no user to
connect.

Disconnecting removes the platform's half. Uninstalling the App on GitHub is
the other half, and is what actually revokes the platform's reach.

## Reading a task's recorded events

The admin console shows per-tool-call measurements (Resources) and per-request
model records (LLM requests), but it does not list a task's raw runtime events.
That panel was removed; the two routes behind it were not, and they are how an
operator answers "what did this task actually do" for a support case or an
abuse report. See the amendment in
[the admin observability spec](specs/2026-09-10-admin-observability-v1.md).

**Metadata.** `GET /admin/agent/runs/{job_id}/activity` returns event
envelopes ordered by execution: `event_type`, `created_at`, `is_error`,
`has_content`, and — for `tool_use` and `tool_result` only — `tool_name` and
`tool_use_id`. It accepts `attempt_id`, `cursor` and `limit` (1-200, default
100). Timestamps are when the event reached the control plane, so they do not
measure model latency, and one `message` is not one model request. Nothing in
this response is content: lifecycle `phase` is deliberately not an allowlisted
column, so a run's opening events all read as indistinguishable `lifecycle`
rows here.

**Content.** `POST /admin/agent/runs/{job_id}/events/{event_id}/content`
returns one event's stored payload — prompt text, tool arguments, tool output
or raw error text. It is audited before it is returned: the audit store records
`agent.run.event_content.read` with the acting administrator, their IP, the
task owner, and the job, event, attempt and sequence identity. **If the audit
write fails the route answers 503 and discloses nothing.** Responses are capped
at 256 KiB and set `truncated` when an oversized old record had to be
previewed.

Both routes require admin authorization. A task's owner does not use them: they
read their own events through the session routes, which need no audit because
the data is already theirs.

**Do not read `agent_job_events.payload` directly.** A `psql` query returns the
same content with no audit record, no size bound and no column allowlist, which
is the situation the content route exists to prevent. If reading events one at
a time through the API is too awkward for a real investigation, that is a
reason to improve the tooling, not a reason to go around the audit.

## MCP servers

An agent with no egress can still use the MCP ecosystem, because it never
reaches a server directly:

```text
sandbox ──[attempt-fenced mcp token]──▶ MCP-only relay ──[owner credential]──▶ MCP server
```

The MCP listener is a separate FastAPI app on port 8001; it does not expose the
job, host, session, admin, or inference APIs. The sandbox authenticates to
`/v1/agent/mcp` over its second fixed relay, so **`platform_only` stays
closed**: inference and MCP are the only agent-phase destinations. The upstream
address and the owner's credential stay in the control plane.

The converse holds too: the main API on 8000, which the public edge forwards
`/api/*` to, serves no tool route, so an `mcp` token carried out of a sandbox
cannot be replayed from the internet. Connection management, OAuth callbacks
and approvals share the `/v1/agent/mcp` prefix but stay on 8000, because they
are the browser's. Block tool calls by leaving the relay off 8000, never by
blocking the prefix at the edge.

There is no deployment registry and no in-code catalogue of servers. A person
adds a connection through `/v1/agent/mcp` — a remote URL, an imported
`mcpServers` snippet, or a stdio launch spec — and a row in
`agent_mcp_connections` holds their configuration and their credential, wrapped
with `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` under an MCP-specific domain
separator. What the server offers is discovered and stored as a capability
snapshot; templates only prefill a form.

MCP OAuth uses `AGENT_MCP_PUBLIC_BASE_URL` for the public API callback and
`AGENT_WEB_BASE_URL` for the browser's return to `/integrations`. For a shared
hostname deployment these can be `https://example.com/agents/api` and
`https://example.com/agents` respectively. The page reports the authorization
result, verifies a stored credential before showing success, and directs the
user to Test to discover the connection's tools.

The relay, connection API, discovery path and all five coding-agent adapters
are implemented and tested for remote MCP. The remaining stdio and optional
protocol capabilities are described below and in the
[conformance record](specs/2026-09-11-per-user-mcp-v1-conformance.md).

The relay follows these rules:

- **Nothing is reachable by name.** A URL path segment is a _binding alias_ —
  a name one conversation gave one connection — and a token names a job, so the
  relay resolves the alias against that job's own bindings. A connection
  somebody else added cannot be named into existence.
- **A session means exactly what it says.** Each capability carries a policy:
  `deny`, `confirm` or `allow`. An omitted capability is denied, including one
  the server adds after the session was configured, and a policy whose tool
  description or schema changed goes back to the owner rather than being
  treated as an aged approval.
- **`confirm` holds the original call.** The relay records a pending invocation
  and waits for the owner's decision in the conversation's Tools panel. Approval
  dispatches that request once under the current attempt and permission fence;
  the CLI receives the result without asking the model to retry. Denial, expiry
  or changed authorization prevents dispatch.
- **Credentials are per user and fail closed.** A missing encryption key,
  unreadable ciphertext, expired connection with no refresh, or disconnected
  connection removes that server from the owner's reachable set and says why on
  the Integrations page.
- **A connection is not a job.** Bindings are read fresh on every tool call
  rather than copied onto a job, so narrowing a session takes effect on its
  next call instead of at its next turn.
- **Two endpoints, one decision path.** `/v1/agent/mcp/<alias>` forwards to one
  connection and leaves the protocol to the CLI. `/v1/agent/mcp` — the bare
  prefix, which `/<alias>` cannot match — is the merged host: the platform
  answers `initialize`, presents every connection of that job as one catalogue
  with names namespaced `<alias>__<tool>`, and routes a call back to its own
  server. It exists because a runtime whose MCP entries are gated by a
  name-and-identity allowlist cannot be given a per-conversation alias at run
  time, and one fixed name and URL is the shape that fits. **Every adapter is
  given that endpoint and nothing else, as one entry named `session`**: an entry
  per connection would present this same merged catalogue once per connection,
  and the per-connection route is left for a client that wants exactly one
  connection's protocol passed through untouched — which no runtime asks for,
  because a legacy SSE server cannot initialize through a pipe at all. Both
  routes call the same `decide`, so an approval in the conversation's Tools panel releases the
  held request under the same policy on either surface.
  Routing on the merged surface is a lookup in the catalogue built from the
  bindings, never a split on the separator: an alias may itself contain `__`, so
  ambiguous or long names receive a stable bounded suffix. The same endpoint
  serves resources and prompts, resolved through that one catalogue and decided by the same function
  as a tool call — the design gives them their own _scopes_ so that they are not
  a way round tool authorization, and a separate code path is exactly how they
  would become one. A tool and a prompt are namespaced by alias because a bare
  name is ambiguous the moment two connections offer one. A resource uses
  `mcp-resource://<connection-id>/<original-uri>` at the merged endpoint so equal
  upstream URIs remain distinct. Reads remove only that prefix and preserve the
  upstream URI, including its percent encoding. A connection that cannot be resolved is
  skipped with a log rather than failing the listing — losing every other tool
  is a worse outcome than the failure that caused it — while the per-connection
  route still refuses loudly for the server that is actually broken.

**The whole chain has been run end to end**, against a real listener, a real
database and a real CLI rather than a fake at each seam. What was exercised:
the sandbox listener on its own port, `AGENT_DATABASE_URL` pointing at a
migrated database, one seeded connection with a static credential and a
discovery snapshot, one live job whose attempt holds the fence, and pi 0.73.1
driving the bridge at `/v1/agent/mcp`. The model was advertised exactly
`session__github__get_issue`; the upstream server saw `server/discover`,
`initialize` and `tools/call`, every one carrying the _connection's_ credential
rather than the sandbox token; the call reached it as `get_issue` with the
model's own arguments; and the tool result the model received was the upstream's
text. Two details that only a live run shows: the era probe falls back to the
handshake against a server that answers `-32601`, and the tool's _namespaced_
name never leaves the platform — the upstream is addressed by its own.

It is `tests/external/test_agent_mcp_end_to_end.py`, and it runs in seconds:
the two ends (the user's server and the model) are threaded HTTP stubs the test
starts itself, the relay is the real application on a real socket, and the CLI
is installed at the version the sandbox Dockerfile pins. It skips rather than
fails without Postgres, npm or the network, and it is in the external tier,
which is run deliberately rather than as part of the default gate — the seams
it crosses are exactly the ones no per-layer test does, so it is worth running
after any change to the merged endpoint, the host or the bridge.

**Connectors are configured, not served.** `/v1/agent/mcp/connectors` registers
one of the user's machines, returns the registration secret exactly once (hashed
at rest, like every other credential here), renames it, revokes it, and binds a
`user-connector` connection to it or unbinds one. A remote connection cannot be
bound: it is a server the platform reaches directly, and sending it the long way
round a user's laptop would be a detour with no purpose. Binding is the
authorization step and it moves both versions, because a connector decides
_where_ a local process runs and that is a change of target like any other.
Revoking detaches everything that routed through it in the same transaction — a
connector marked revoked while a connection still names it is a job resolving to
a target its owner has withdrawn.

No connector runtime ships with this service yet, so every connector view says
`runtime: "unavailable"` and a connection bound to one is refused at use with
that reason, in the same way a managed-worker connection is refused for having
no worker. The configuration is real and inspectable; the execution is not, and
nothing here claims otherwise.

**Deploying this, and backing it out.** Three things have to be true in order,
and the middle one is the one people skip.

_Order the schema first._ `0008_generic_mcp_connections` drops
`UNIQUE(user_id, server)`, renames `server` to `template`, adds the connection's
transport, location, endpoint / launch spec and auth profile, moves the legacy
`agent_threads.mcp_servers` list onto explicit bindings. It retains an empty,
write-constrained `mcp_servers` column and a generated, always-NULL `server`
column so the previous API can still list conversations, build the composer
configuration and accept ordinary queued submissions during the host rollout.
Neither column can authorize MCP access. Existing selections survive in the
new bindings; their execution requires the updated API and relay. New
connections are never offered to the old provider-name authorizer.

`0011_mcp_rolling_compat` adds the same inert columns to databases that already
applied the original, destructive `0008`. Apply migrations through head before
replacing the API. This is a forward repair: it preserves connections,
credentials and permissions, and does not require a database downgrade.

_Migrate, do not guess._ The revision maps a legacy name only where the thread's
owner has exactly one connection for that provider, and writes everything else
into `agent_mcp_legacy_bindings` with `no_connection` or `ambiguous_connection`.
Those rows are the list of sessions whose tools a human has to look at; they are
not cleaned up automatically, because silently dropping them and silently
guessing which connection a name meant are the same mistake from two sides.

Deploy the sandbox and runner artifacts from the same release. Codex receives
its root-owned system allowlist before its first MCP-enabled launch. The sandbox
contains Kilo 7.4.17 built from the source revision and patch recorded beside
`build-kilo.sh`; adding a different Kilo version requires a reviewed build.
The runner upgrades retained containers' adapter files and Kilo executables from
these trusted artifacts before launching members, preserving native session
files. Updates require no host bind mount or additional Linux capability.

MCP is enabled on every supported runtime through the normal deployment.
There is no feature switch or preview tier. Rollbacks must retain a schema-compatible
service and leave the migrated connection and binding data in place; fix forward
when a schema downgrade would discard those records.

Two things this feature deliberately does not add: no new dependency for model
inference (MCP and connectors sit beside the gateway path, not inside it), and
no second encryption root — connection credentials are wrapped with the same
`AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` under their own domain separator, so one
key to rotate and two credential kinds that cannot be read as each other.

**An adapter may claim MCP only when it can do both halves**: point the CLI at
the platform's relay, _and_ stop it reading any other MCP source. A runtime that
could be given servers but not stopped from picking up the repository's own
would be worse than one with no MCP at all, so the capability is declared per
runtime and checked against the pinned binary rather than inferred from the
family a CLI belongs to.

| Runtime       | Pin     | How the relay reaches it                                                                                                                     | Why the repository cannot add servers                                                                                                                                                                                                                                                                                                                                 |
| ------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude-code` | 2.1.220 | `--mcp-config` with the platform's servers                                                                                                   | `--strict-mcp-config`, verified against the CLI: without it a committed `.mcp.json` is loaded alongside ours with no approval step                                                                                                                                                                                                                                    |
| `opencode`    | 1.18.9  | the reviewed `opencode-freeinference` wrapper merges the relay endpoints into the `OPENCODE_CONFIG` it writes                                | the wrapper exports `OPENCODE_DISABLE_PROJECT_CONFIG`, which covers both `opencode.json` and `.opencode/`                                                                                                                                                                                                                                                             |
| `kilo`        | 7.4.17  | the reviewed wrapper configures the authenticated merged relay for run and serve; the live bridge verifies connection before reporting ready | the pinned build patches the legacy MCP loader to honor `KILO_DISABLE_PROJECT_CONFIG`, covering `.kilo/mcp.json` and `.kilocode/mcp.json` alongside the regular project configuration                                                                                                                                                                                 |
| `codex`       | 0.145.0 | exec and app-server share the merged relay URL and token environment reference; app-server verifies MCP startup                              | trusted guest-root bootstrap atomically installs the deployment's exact relay URL as the sole server identity in `/etc/codex/requirements.toml` before launch; repository and user configuration cannot expand that system allowlist                                                                                                                                  |
| `pi`          | 0.73.1  | the reviewed `pi-mcp-bridge.ts` shipped at `/usr/local/libexec/`, loaded with an explicit `-e`                                               | `--no-extensions` disables every other extension source — the project's `.pi/extensions/`, the global directory, and anything a settings file names — so the explicit path is the only way in. Verified against the pinned binary: a repository's own extension does not load, the relay's tools are registered as `<alias>__<tool>`, and a call reaches the upstream |

A job naming connections on a runtime that has not declared them is refused at
creation rather than run with the tools silently missing.

Retained containers receive the reviewed Kilo executable and adapter files
before launch. Codex's system policy is installed from the trusted deployment
address without a per-deployment image build. All five adapters use the single
`session` entry at `/v1/agent/mcp`; no feature switch or preview tier is required.

**The credential is referenced, not embedded, and the syntax is not portable.**
Claude Code expands `${AGENT_MCP_TOKEN}`; the OpenCode family substitutes
`{env:AGENT_MCP_TOKEN}` as a text pass over the raw config file before parsing
it. Both were checked against the pinned binaries, and the wrong one fails
silently: the literal placeholder is sent as the bearer token and every call is
refused with a 401 that reads as a broken server. The address is decided once in
`apps/backend/cloud_agent_host/runtimes.py` and the reference is written next to the CLI that
has to expand it, in `deploy/sandbox/`. Either way the token travels in the
environment, never in argv and never in the config file.

### What `--strict-mcp-config` is holding shut

The flag landed in hybridInference#1129 on the strength of the CLI's documented behaviour, and
`--mcp-config` now supplies the platform's servers alongside it. The pairing is
load-bearing in both directions: strict mode makes our config the _only_ source
of servers, and our config is passed even when empty so there is no gap.

What the flag prevents was measured rather than assumed, in exactly the
configuration a job runs in (headless `-p`, `bypassPermissions`, cwd = the
checkout). A directory containing

```json
{
  "mcpServers": {
    "evil_repo_server": { "command": "/bin/echo", "args": ["pwned"] }
  }
}
```

produces these two `system/init` lines, differing only in the flag:

| invocation                                | MCP servers reported by the CLI |
| ----------------------------------------- | ------------------------------- |
| `--mcp-config <ours>`                     | `evil_repo_server`, `github`    |
| `--mcp-config <ours> --strict-mcp-config` | `github`                        |

No prompt, no approval step: the permission mode the sandbox needs for its
_own_ work is exactly what removes the one gate that would have stopped this.
That is why the flag stays unconditional now that there is a real config to
pass beside it — a job with no MCP at all is the case where an injected server
would be least expected and least noticed.

The design's own rule about egress is what applies here: a boundary the agent
participates in is not a boundary.

## Before a job can run for real

Two things are outside the code and must be done by a human.

### 0. A closed agent network needs both narrow relays on its backplane

`platform_only` means the inference gateway and MCP-only listener, with no
caller-selected destination and no internet route. A remote deployment must
put both fixed tunnel aliases on the configured discovery backplane. The host
then creates one internal bridge per sandbox group and attaches only those two
relay containers plus that group's chat container. Docker's `internal: true`
blocks routing off a bridge but does **not** block containers on the same bridge
from reaching each other, and by itself still leaves the bridge gateway
reachable as a host address. Each chat bridge therefore also sets
`com.docker.network.bridge.inhibit_ipv4=true`, so Docker assigns no host-side
IPv4 address to it. The sandbox also sets `--dns 127.0.0.1`: Docker's embedded
DNS still resolves the relay aliases, while unknown queries are forwarded from
the sandbox namespace to its own loopback and fail instead of becoming a DNS
egress path (including on daemons affected by CVE-2024-29018). Using the compose
backplane directly for every chat, or an internal bridge without these guards,
would violate the isolation contract. If either relay alias is missing,
private-network creation fails before the Agent CLI starts.

The host deployment check probes both relays: it starts containers on the
agent discovery backplane and **makes requests**, requiring HTTP 200 from the
inference `/health` and MCP `/healthz` endpoints before it publishes the
exact-SHA ready marker. The runner's lower-level inference connectivity probe
still treats any HTTP response as reachability, but that weaker signal cannot
release a deployment. Every real chat additionally reconciles its own labelled
internal bridge and the exact relay owners before create/start. DNS alone is
never enough: a relay can own the expected name while its upstream is down,
which is exactly the cross-machine false positive the deployment check must
catch.

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

One more compose detail worth knowing: the overlay _declares_ `agent-egress` as
the relay-discovery backplane, so compose insists on creating it. A network of
that name created by hand beforehand is refused with a label mismatch — let
compose own it, or point the tier variables at names compose does not declare.
The `freeinference-chat-net-*` networks are different: Docker lifecycle code
derives their names and labels from the sandbox generation. It allocates one `/29` from
`AGENT_EGRESS_CHAT_NETWORK_POOL` (default `10.240.0.0/16`) and lets Docker
arbitrate concurrent claims. The setup bridge is deleted at setup→agent; the
current bridge is disconnected and deleted on idle/lost stop while Docker keeps
the container writable layer. Wake creates the correct setup or agent bridge
again. Broker reconciliation also removes labelled networks left by an
interrupted create or transition after a five-minute grace period. The grace
prevents status reconciliation from deleting a bridge between creation and
sandbox attachment while still reclaiming bridge interfaces and IPAM.

### 1. Only enrolled sandbox hosts run jobs

The old manually dispatched `agent-job-runner` Actions workflow is deleted. It
was a disposable one-shot substrate using the unisolated `process` backend and
a shared dispatcher credential; keeping it manual would still leave a second
executable topology behind the UI. There is no GitHub Actions fallback.

Deploy at least one host through `deploy/host/deploy.sh`, enroll it with the
control plane, and let its runner service poll `/v1/agent/worker/claim`. A
runner cannot claim until its exact replica attests the retained protocol,
container image/runtime pins, broker configuration, execution profile, and
host capacity.

Standalone, the host stack needs more than an enrolment credential. Start from
`deploy/host/.env.example`: `COMPOSE_PROJECT` and `AGENT_SANDBOX_IMAGE` are
required (`docker-compose.sandbox.yml:249,359` and `:236,345` interpolate both
with `:?`, so an unset value stops the render), alongside
`AGENT_WORKSPACE_BROKER_TOKEN`, the three tunnel destinations with their own
keys and known-hosts files, and the broker reverse-forward trio when the
control plane is on another machine.

### 2. Credentials

**Host credential** — mint one short-lived enrollment token for a named host:

```bash
# Admin API; the response shows the single-use token once.
POST /admin/agent/runner-hosts/enroll-token
```

Put that value in `AGENT_ENROLL_TOKEN` for first start. Registration replaces
it with a long-lived credential bound to that host and persists the credential
under `AGENT_HOST_STATE_ROOT`; every heartbeat and claim then authenticates
with it. A host name in a request is never accepted as identity by itself.

**Workspace broker credential** — generate one
`AGENT_WORKSPACE_BROKER_TOKEN` per environment and set the same value on the
control plane and every host broker. It authorizes file, terminal, Git, and
session-lifecycle calls. It is not a claim credential and never enters a chat
container.

**GitHub credential** — install a GitHub App and give the _control plane_ its private
key. Nothing needs to be minted or rotated by hand: the platform signs a
nine-minute App JWT (`github_app.py`; GitHub refuses more than ten) and
exchanges it for an hour-long token scoped to the installation covering the
repository being published to.

The key reaches the container through a read-only bind mount, at a fixed path.
Two variables, and they are not the same variable:

```bash
# The *host* file that holds the PEM. This is the mount source.
AGENT_GITHUB_APP_PRIVATE_KEY_FILE=/etc/cloud-agent/github-app.pem
```

```yaml
# compose: $AGENT_GITHUB_APP_PRIVATE_KEY_FILE ──▶ /etc/cloud-agent/github-app.pem:ro
```

`AGENT_GITHUB_APP_PRIVATE_KEY_PATH` names the location **inside** the
container and already defaults to that mounted path
(`docker-compose.yml:142`), so the Compose deployment sets only
`AGENT_GITHUB_APP_PRIVATE_KEY_FILE`. Overriding the path variable without
mounting anything there produces a service that starts, offers Integrations,
and fails at the first publish — which is exactly what
`tests/unit/test_control_plane_deploy.py` now pins. Put the file on the host,
owned by the service user and `chmod 600`. The control plane mounts nothing
else: there is no `config/`, `distributions/` or `var/data/` inside that
container (`Dockerfile:31,41-52`).

Alternatively `AGENT_GITHUB_APP_PRIVATE_KEY` takes the PEM inline (escaped
`\n` are accepted and unescaped) and skips the mount — at the cost of putting
the key in the process environment, where `docker inspect`, crash dumps and
log shippers can read it. Prefer the file.

The App needs `contents: write` and `pull_requests: write` and nothing else —
notably not `workflows`, so a patch touching `.github/` cannot be pushed even
if the gate were bypassed. Revocation is uninstalling the App.

For the _connect_ flow (a user authorizing their own repositories) the App
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

That same redirect is what _Configure repositories_ relies on, so the install
URL stays wired to a connected user's Manage menu: it is the only route to the
page where the App's repository selection changes, and GitHub picks the user
or organization settings page itself. Set `AGENT_GITHUB_APP_INSTALL_URL` to
the `/apps/<slug>/installations/new` form — pointing it at the OAuth endpoint
instead leaves a user with no installation looping through authorization.

The same App also supplies the runner's clone credential, and the two are
_not_ the same token. An installation token inherits every permission the App
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
AGENT_ENROLL_TOKEN=<single-use token for this host>
AGENT_WORKSPACE_BROKER_TOKEN=<same environment value as the control plane>
AGENT_SANDBOX_BACKEND=container  # explicit trusted-repository downgrade
```

The `AGENT_SANDBOX_BACKEND=container` line is a **deliberate downgrade**: sandbox
groups share the host kernel. Before enabling Kata for new groups, provision
the host and verify the guest runtime and relay paths with the checks below.
Existing retained runc containers need the separate migration treatment
described there.

One command per host makes the runner standing; compose's
`restart: unless-stopped` plus an enabled Docker daemon carries it across
crashes and reboots:

```bash
deploy/host/sandbox_host.sh up      # build images, tunnel + runners
deploy/host/sandbox_host.sh preflight # validate this host against an existing image
deploy/host/sandbox_host.sh check   # probe all relay/control paths
deploy/host/sandbox_host.sh status  # containers + recent log tail
deploy/host/sandbox_host.sh down    # stop them

# `up` in its two halves, for a deployer that has to put something between
# them. Nothing in `prepare` replaces a container that is serving.
deploy/host/sandbox_host.sh prepare  # pull or build, Kata smoke test, candidate check
deploy/host/sandbox_host.sh activate # drain, replace, prove
```

There is one script now, not two. The co-located `agent_runner.sh` was an
overlay on the gateway's own compose stack and did not move — see
MIGRATION.md's open-decisions register. A runner on the gateway's own machine
is this same standalone compose with the tunnel pointing at localhost, which
is a configuration rather than a second file. Standalone `preflight` checks
Docker, retained-mode configuration, host isolation, and the configured image;
`up` repeats the authoritative checks after building the image it will deploy.

### Provisioning Kata on a runner host

An ordinary container isolates processes; every sandbox still issues syscalls
straight at the host kernel. Kata gives each sandbox group a lightweight VM
with its own kernel, so a container escape has a hypervisor behind it. That is
what makes the difference between "fine for repositories we trust" and "fine
for a repository we have never read", because the sandbox runs whatever its build
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
the default Kata configuration and sets `runtime.disable_guest_seccomp=false`.
Kata's packaged default otherwise ignores the OCI seccomp filter even when
Docker accepted the container security options. That kernel is built with Kata's reviewed
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

The fixed observer and platform lifecycle helpers run as guest root. Agent CLIs and
repository code still run under their per-member UIDs with all capabilities
dropped and `no-new-privileges`; they cannot read, edit, or directly inject
records into the root-only journal. Claude's fixed non-root marker helper talks
to it over a credential-checked Unix socket. A same-UID tool can still execute
that helper, so exact per-tool association is diagnostic, never a security or
billing authority; the eBPF lifecycle census itself is independent of marks.
Kata containers retain `BPF`, `PERFMON`, `SETUID`, `SETGID`, `CHOWN`,
`DAC_OVERRIDE`, `KILL` and `SYS_PTRACE` in the bounding set for the fixed guest-root helpers:
tracing, the UID-filter self-test, socket ownership, member cleanup and verifying
the marker sender's `/proc/<pid>/exe` across member UIDs.
Kata privileged exec cannot acquire capabilities outside that original set.
Ordinary exec explicitly enters through `setpriv --no-new-privs`, because
Kata 3.32 does not inherit PID 1's setting for Docker exec. Preflight requires
non-root execution, zero effective/permitted capabilities, `NoNewPrivs=1`
and `Seccomp=2`, then runs the observer and marker self-tests and requires a
real non-root pre/post pair to reach the trusted observer journal. Privileged
helpers also verify the **inspected container's** Kata runtime and execute by
that verified container ID; changing a runner setting cannot promote an old
runc container into a guest-root target. A shared
container's periodic Docker statistics are group totals, so current member
turns deliberately report aggregate CPU/memory/I/O/network as unavailable
(`coverage=0`, `sampler_version=shared-member-v1`) instead of duplicating a
sibling's work. Process existence is separate, event-driven telemetry:
fork/exec/exit survives even
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
The per-turn table is therefore a member-UID lifecycle census of process starts observed
after that turn's cursor, not a point-in-time snapshot of resident processes
that were already running before the turn and not a whole-container total.

Docker's embedded DNS resolver (`127.0.0.11`) is outside the guest. Kata
activation and phase checks therefore refresh `/etc/hosts` with only the
configured relays' addresses on that chat's current private bridge. Unknown
names gain no resolver or additional egress path. The first setup-to-agent
transition stops and restarts the Kata guest after changing networks, before
launching a resident CLI: Docker's live network changes alone do not update
Kata 3.32's guest interfaces. The writable layer and container identity remain.
An already settled agent-phase activation does not restart a live guest.

When switching a host from runc, keep its retained runc groups on a compatible
broker or migrate them separately. A backend setting does not change the
runtime of a stopped Docker container; the privileged helper fence rejects
those old containers. Do not delete retained workspaces as part of enabling
Kata for new jobs.

Requirements: an x86_64 Linux host, bare metal or with nested virtualization
enabled. Kata cannot run without KVM.

**Installing the shim is necessary, not sufficient.** A working guest kernel
does not prove the sandbox can reach either relay. Before changing the backend
for new groups, check the provisioned runtime and the running stack from the
host checkout, using its `.env`:

```bash
AGENT_SANDBOX_BACKEND=kata deploy/host/sandbox_host.sh preflight
AGENT_SANDBOX_BACKEND=kata deploy/host/sandbox_host.sh check
```

`preflight` verifies the configured image and guest kernel. `check` starts relay
probes under Kata on `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` and requires HTTP 200
from inference `/health` and MCP `/healthz`. It also checks the live runner's
full sandbox preflight, broker, and control-plane paths. That compose-owned
closed network is the relay discovery backplane; retained groups get their
own private bridges during activation. The compose `..._internal` network is
the host services' network and cannot substitute for either check.

The Kata relay probes resolve only the configured relay endpoints through
Docker inspection and pass their addresses into the guest, matching the
runtime's fixed-host approach. A bare `alpine getent` probe depends on Docker's
embedded DNS and does not verify this path. A failed check needs diagnosis of
the reported guest, relay, or service path before switching new groups.

After those checks pass and existing runc groups have been accounted for, drop
`AGENT_SANDBOX_BACKEND=container` from the host's `.env` and redeploy.
`deploy/host/sandbox_host.sh up` repeats the checks for the image it builds:

- with the default `kata` backend, the shim must be installed, **and** one real
  container started from the sandbox image must report a kernel that is not the
  host's. A runtime the daemon accepts while the sandbox still lands on the host
  kernel fails this — that is the case every cheaper signal misses.
- there is no automatic fallback to a shared kernel. Accepting one stays
  possible and stays explicit: set `AGENT_SANDBOX_BACKEND=container`, and every
  deploy restates what it costs.

The control plane independently enforces the same fact at scheduling time.
`AGENT_DEFAULT_EXECUTION_PROFILE` is already `untrusted-kata-v1` in the
control-plane compose (`docker-compose.yml:117`), so this is the default rather
than an opt-in — and the consequence of that default is that **a deployment
whose hosts have not been through Kata provisioning claims no secure work at
all**, rather than falling back to a shared kernel. Set it explicitly
anyway so the value is visible in the deployment's `.env`, after at least one
enrolled Kata Host passes
preflight. A secure job is claimable only when all of the following are true in
the same claim transaction:

- the caller presents that Host's current enrollment-issued per-host
  credential; there is no shared dispatcher-token claim path;
- the exact runner replica reports `io.containerd.kata.v2`;
- Python preflight started the configured sandbox image under that runtime and
  observed a non-empty guest kernel different from the Host kernel.

The claim, not the Host's mutable heartbeat row, supplies the attempt's runtime,
image digest, CLI version and kernel stamp. This matters during rolling deploys,
when runc and Kata replicas can temporarily share one Host row. A missing probe,
an old runner, a custom runtime, a self-declared `kata` capability or a revoked
Host credential simply cannot claim the secure job; there is no fallback to
runc. `trusted-v1` selects the reviewed shared-kernel boundary for trusted
internal repositories; it does not select another workspace/session executor.
Every follow-up and fork inherits the execution profile stored on its thread.
Retained chats carry the original image digest in immutable container labels
and use that content-addressed image for a retry before Docker returned a
container id; a moved image tag cannot change the chat's actual image while its
database row still reports the old one.

### How many runners

The retained shared-session topology is unconditional. An ordinary chat is a
one-member group with one container/workspace and one live CLI transport. Fork
immediately invokes the pinned CLI's native fork-only surface and adds a second
chat/session/process to that same container. Parent and child may execute
concurrently without a group turn lock.

Each member has a distinct fixed UID. Before handing over a turn, the host
captures that UID's `(pid, /proc starttime)` census through the root-owned,
mode-0555 `member-process-reaper` installed in the sandbox image. Cleanup sends
that baseline over stdin to a fresh helper, rechecks identity immediately
before every signal, escalates TERM→KILL, rescans, and fails closed if anything
survives. The baseline is never a member-writable file, so repository code
cannot edit it to preserve a process or redirect cleanup at a reused PID.

`shared-sandbox-v1` is a claim attestation, not a feature switch. A host must
advertise it from the exact process that claims the job. A host's limit is at
least two, because a fork's sibling runs beside its parent; `AGENT_HOST_SLOTS`
(default three) is that limit until an admin sets one on the Servers page.

Historical Fork additionally requires `native-fork-at-anchor-v1` and an exact
match between the host's current sandbox image digest and the retained group's
pinned digest. A container kept from the previous image remains usable for
normal follow-ups after a rollout, but its older fork helper is never asked to
interpret a historical anchor.

The Docker socket (or reviewed mTLS Engine endpoint) is the only host
control/data interface for chat state. No repository, workspace, Agent CLI
home, or session file is mapped from the host. Historical rows without a
shared retained sandbox are non-runnable and fail closed, including an old
in-flight parent observed during rollout. A waiting tail inserted by the old
API before cutover is failed when that parent settles; it is never promoted
into a newly fabricated native session.

All submitted turns that have not started use `queued`, including messages
sent while an earlier turn is still running. A runner can claim a queued
turn only after every earlier turn in the same chat is terminal. Cancelling
an intermediate queued turn does not let later messages overtake an active
earlier turn. There is no separate promotion step or `waiting` job state.

### One sandbox-group inactivity clock (and separate safety watchdogs)

`AGENT_CHAT_IDLE_TIMEOUT_S` is the sandbox group's sole lifecycle timeout and
defaults to **1800 seconds (30 minutes)**. Once the latest activity across all
members is durably settled and no member/broker operation is active, the host
Shared Session Supervisor issues `docker stop`, disconnects and deletes the phase
bridge, but never issues
`docker rm`. A later acquire rebuilds the correct bridge, starts, and re-inspects
the same labelled container. Supervisor shutdown itself leaves all chat
containers untouched. At broker startup, reconciliation stops a still-running
orphan before admitting it again; resident member processes from an unproven
in-memory lease are therefore never silently adopted. The supervisor does not
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
expires, the system stops it, marks the context LOST, and fails the job; it
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
- no workspace-cleanup TTL addresses chat data. This phase never removes a
  retained sandbox; operators monitor Docker storage separately.

There is no retained/shared feature flag or runtime allowlist switch. Claude
Code, Codex, Pi, OpenCode, and Kilo are claimable only after exact
image/runtime/helper attestation. Compatible follow-ups send only the new
prompt to the same resident process; recovery resumes the pinned native
session in a replacement process. Fork from any successfully settled native
turn with a persisted native ordinal, matching image pin, and capable owner host
is immediate inside the same container/workspace. The UI offers it only on the
final assistant/tool boundary of that turn. Edit & rewind resolves the nearest
earlier such boundary for the selected user prompt and puts the replacement
text in the child composer without sending it. The frontend treats `NEW`,
`LIVE`, `RESTORED`, and `LOST` as context truth; hot/warm/cold/replay remains
diagnostic telemetry and never chooses another executor. Current retained
runners never emit `replay`; the API accepts it only so an attempt admitted by
the previous generation can finish during a parallel rollout, and historical
rows remain readable in the UI.

After a shared turn settles, its snapshot and draft-PR fields may still change
asynchronously. The detail view and recent-task list poll only while that
publication state is pending/capturing/captured/publishing, and stop at
published/failed; other terminal jobs do not acquire a background
polling loop.

Disk-pressure admission/alerting and a non-mutating pre-prompt context
fingerprint remain deployment hardening work. The local stack below is suitable
for functional development, not evidence that the production Kata/live-provider
and operational gates have passed.

### Complete local stack and retained database credentials

`deploy/local/dev.sh up` stores its generated Postgres password and matching
DSN in the gitignored `deploy/local/.env.local`. Preserve that file for as long
as the selected Compose project's `agent-postgres` volume is retained.
PostgreSQL ignores a new `POSTGRES_PASSWORD` after its data directory has been
initialized, so losing the file cannot be repaired by generating another
password. The script detects this state and exits without deleting the volume
or changing the database.

`up` builds the required images first. It and `start` then start only Postgres,
wait for `pg_isready`, authenticate through `AGENT_DATABASE_URL` from the
migration image, and run Alembic. Only then can the complete stack start. If
the probe fails, restore the original `AGENT_POSTGRES_PASSWORD` and
`AGENT_DATABASE_URL`; do not remove the volume unless discarding all local
agent state is intentional.

Terminal freezing is also member-concurrent: each protected job phase owns a
separate broker suspension token for the shared workspace. Completing or
recovering one sibling releases only that token; interactive PTYs are resumed
only when no sibling remains in a protected phase. The broker still accepts
the previous generation's token-less request shape during a rolling deploy,
but current control-plane requests always identify the owning job.

### Rolling deployment order

The host and control-plane Actions jobs start in parallel. The host builds the
replacement sandbox/runner stack and proves Kata before invalidating its ready
marker. It then stops the old runner container and lets its five-minute grace
drain admitted turns before replacing the broker; no new turn can be admitted
in that interval. After the isolation, tunnel, runtime and retained Docker-root
checks pass, it records `.deployed-sha` and only then publishes an exact
`shared-sandbox-deployment:<sha>` capability. In parallel, the control job
builds and migrates, then waits for a heartbeat newer than the start of that
check and reverse-probes every new-chat placement broker before `compose up`
can replace a running API. The claim route enforces that same exact deployment
capability. A pending runner registers and heartbeats its base protocol so the
host checks can inspect them, but do not poll; this also prevents the previous
API from assigning work before it knows the new claim field. A runner with a
pending/failed marker therefore cannot take work even during a cold-start API
bootstrap. When placement was moved between hosts,
it also probes every online host that still owns a retained group; changing the
pin must not make old chats disappear from rollout safety. `SHARED_READY_TIMEOUT`
controls that wait and defaults to 900 seconds.

The deploy user writes `.shared-live-ready` in the deployment checkout, separate
from the container-owned enrollment directory. Runners mount only that file
read-only at `/run/cloud-agent/shared-live-ready`. The host script preserves its
inode when changing `pending:<sha>` to `<sha>`, so existing file mounts see the
transition; an empty or partial write cannot advertise an exact target SHA.
No credential ownership or permission changes are needed for a rollout.

Both Actions jobs use the workflow commit's deployment orchestrator against a
separate target checkout. Online rollback therefore cannot execute a
pre-fence target's old scripts; commits without the current
`deploy/DEPLOY_PROTOCOL`, or with an incompatible protocol value, are refused
before changing Docker state. The current protocol deliberately excludes
ancestors that predate the rewritten schema baseline (`0001_schema_baseline`,
2026-09-10): their Alembic chain is one the database no longer records, so
they are not schema-compatible rollback targets.
Database changes are expand-first. Never downgrade columns underneath a
still-running new API; roll code back to a schema-compatible commit first and
treat any later contract migration as a separate reviewed operation.

`workflow_run` loads its workflow definition from the repository's default
branch. Synchronize this orchestrator change to `main` before relying on the
rollback fence, and do not manually dispatch an older default-branch workflow
against staging during that rollout window.

This fence is unconditional because retained/shared-live is the only executor.
It cannot enable a legacy path and it cannot be bypassed by stale
`AGENT_RESTARTABLE_*` or `AGENT_SHARED_LIVE_*` values. On failure, the existing
control plane remains running and the deploy stops for operator repair. If
either the API or MCP relay is missing, the host cannot complete all readiness
probes (a true cold start has neither). After migration, the control script
independently bootstraps only the missing service or services. A failed gate
stops only those started by that invocation, leaving a pre-existing peer
running for diagnosis. Web does not start until the same exact-SHA fence
succeeds. Once it does, the bootstrap peers belong to the desired target stack:
a later final API/web probe failure leaves that stack running for diagnosis but
does not write `.deployed-sha`.

### Preserving an old-chain database

An old Alembic revision cannot be upgraded by the rewritten graph. If the
history must survive, use `scripts/convert_legacy_database_copy.py` on a
**separately restored copy**, not the selected-state rebuild below. The converter
currently supports exactly `0025_retained_followup_gate`; other old revisions
need their own audited conversion. It keeps all original rows and columns in
`legacy_archive`, creates the real baseline with constraints enabled, copies the
supported live state, and applies the repository's additive migrations in one
transaction. It refuses active jobs, other clients and an already-converted copy.

1. Drain before the final snapshot and stop all Agent database writers. Keep a
   full custom-format PostgreSQL dump, its checksum, the current code/images and
   Agent configuration. Rehearse restoring the dump in an isolated PostgreSQL
   instance with no applications attached; do not use the live database as the
   conversion destination.
2. Name the restored database `agent_legacy_copy_<unique_name>`. Set
   `LEGACY_COPY_DATABASE_URL` privately to that copy; the tool deliberately never
   falls back to `AGENT_DATABASE_URL`. Use the intended target release checkout
   and its Python dependencies. Audit first, then convert, using new report paths:

   ```bash
   python scripts/convert_legacy_database_copy.py \
     --database agent_legacy_copy_rehearsal --report audit.json
   python scripts/convert_legacy_database_copy.py \
     --database agent_legacy_copy_rehearsal --report conversion.json --apply
   ```

   Reports contain counts/hashes, not user rows or credentials. Retain them
   privately with the backup. A failed transaction leaves the original schema
   intact. A report I/O error requires inspecting the copy before retrying.
3. Verify all archived rows, retained public data and sequence high-water marks.
   Exercise the target version's storage queries and a fresh write on the copy.
   Retired `replay` telemetry survives in the archive; its public continuity field
   becomes NULL, since it is not a native resume mode. Retired artifact tables
   remain available for operator recovery in the archive, not in the current API.
4. Establish the original Gateway issuer independently, then explicitly adopt
   that issuer on the converted copy using the procedure in
   [minimal-identity.md](minimal-identity.md). Conversion does not bind a namespace
   or make the copy ready for traffic. Preserve the existing host credential,
   owner identities and retained filesystem paths.
5. For cutover, repeat the proven procedure from a fresh, quiescent snapshot.
   Point only Agent at the converted database after verification. Keep ingress
   closed while validating both roles. Before new writes, recovery can restore
   the preserved source configuration/database. After new writes, do not point
   old code at the new schema or discard the new database: recovery must retain
   and reconcile the new data. Prefer a tested compatible artifact recovery.

The complete conversion contract is
[the legacy database copy plan](plans/2026-09-18-legacy-database-copy.md).

### Rebuilding the agent database

The schema baseline was rewritten on 2026-09-10 (`0001_schema_baseline`), and
a database stamped with any revision of the old chain fails
`alembic upgrade head` with "can't locate revision". Such a database is
converted on a copy as above, or rebuilt only when discarding task history is
intentional. The selected-state rebuild below is **not** "drop the database and deploy":
the runner keeps its host credential in a file and never re-enrols while
that file exists, so an empty `agent_runner_hosts` turns every heartbeat into
a 401, the runner logs `agent_host_credential_rejected` and stops claiming, and
the control-plane deploy waits for a fresh heartbeat that never comes. The
compose-effective location is
`${AGENT_HOST_STATE_ROOT:-/var/lib/hybridinference/agent-host}/credential`
(`docker-compose.sandbox.yml:307`); the code default
`AGENT_HOST_CREDENTIAL_FILE=/var/lib/hybridinference/agent-host-credential`
(`enrollment.py`) applies only to a runner started outside that compose file,
and `AGENT_HOST_STATE_ROOT` becomes mandatory once `SIBLING_COMPOSE_PROJECTS`
is set. The
host's registration must survive the rebuild. So must the users' source
control and MCP connections, which are credentials nobody can re-mint from
the database side, and the identity-namespace binding, which says which
Gateway minted the `user_id`s those rows are keyed by: `deploy.sh` runs
`python -m cloud_agent.identity_namespace --check` before either listener
starts, and that check refuses to start an API over owner data with no issuer
to attribute it to.

Rehearsed on a copy of the old schema (2026-09-10), including the ways the
naive version fails: a positional insert lands values in the wrong columns
because the rewritten baseline orders some differently, a host row restored
with its old `slots_used` never claims again because the attempts that held
those slots are gone, a restore that leaves the identity binding behind stops
the API at startup, and a restored `draining` host never claims again because
nothing undoes the drain. Every `docker compose` below is the
invocation `deploy/control-plane/deploy.sh` makes — project, env file,
compose file — and it runs in the deploy root with the target commit checked
out. The compose project below is staging's (`cloudagent`).

1. Record which hosts are taking work, then let them finish. The drain below
   is this rebuild's own and has to be undone in step 5, but only for the
   hosts it is applied to: a host that was already `draining`, `offline` or
   `disabled` before the rebuild must stay that way, and the heartbeat will
   not put one back online. So write the undo before making the change, then
   drain from the admin page (or
   `POST /admin/agent/runner-hosts/<host>/drain`), wait until the host holds
   no running attempts, and only then stop the services that hold database
   connections, leaving Postgres up:

   ```bash
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -Atc \
     "SELECT format('UPDATE agent_runner_hosts SET status = ''online'' WHERE host = %L;', host) \
      FROM agent_runner_hosts WHERE status = 'online'" \
     > cloudagent-undrain-$(date +%F).sql
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -Atc \
     "SELECT host, status, slots_used FROM agent_runner_hosts; \
      SELECT count(*) FROM agent_attempts WHERE status = 'running'"
   docker stop cloudagent-control-plane-1 cloudagent-mcp-relay-1 cloudagent-web-1
   ```

2. Take the forensic dump of everything, then the data-only dump of the state
   that survives — the host registrations, the three user connection tables,
   and the identity-namespace binding those `user_id`s belong to — then the
   placement policy as one statement. `--column-inserts` is load-bearing (see
   above), and `format('%L')` is what keeps an unpinned `active_host` a SQL
   `NULL` rather than an empty string the new foreign key would refuse:

   ```bash
   docker exec cloudagent-postgres-1 pg_dump -U cloudagent -Fc cloudagent > cloudagent-$(date +%F).dump
   docker exec cloudagent-postgres-1 pg_dump -U cloudagent --data-only --column-inserts \
     -t agent_runner_hosts -t agent_repo_grants -t agent_gitlab_connections \
     -t agent_mcp_connections -t agent_identity_namespace \
     cloudagent > cloudagent-state-$(date +%F).sql
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -Atc \
     "SELECT format('UPDATE agent_runner_policy SET active_host = %L, runtime_versions = %L::jsonb;', \
                    active_host, runtime_versions) FROM agent_runner_policy" \
     > cloudagent-policy-$(date +%F).sql
   ```

3. Build the target's `migrate` image **before** touching the database. The
   migrations are copied into the image at build time, not mounted from the
   checkout, so a stale image would rebuild the old chain into the empty
   database:

   ```bash
   export AGENT_DEPLOYMENT_SHA="$(git rev-parse HEAD)"
   docker compose -p cloudagent --env-file .env -f deploy/control-plane/docker-compose.yml config --services
   docker compose -p cloudagent --env-file .env -f deploy/control-plane/docker-compose.yml build migrate
   ```

4. Recreate the database, apply the baseline, and check what it is stamped
   with:

   ```bash
   docker exec cloudagent-postgres-1 psql -U cloudagent -d postgres \
     -c 'DROP DATABASE cloudagent' -c 'CREATE DATABASE cloudagent OWNER cloudagent'
   docker compose -p cloudagent --env-file .env -f deploy/control-plane/docker-compose.yml \
     run --rm --no-deps migrate
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -Atc \
     'SELECT version_num FROM alembic_version'
   ```

   The version must be the head of the rewritten chain (`0001_schema_baseline`
   or a later revision on it); an `00xx_…` from the old chain means step 3 ran
   the wrong image.

5. Restore the surviving state, reset the counter that described attempts
   that no longer exist, undo this rebuild's drain, restore the policy, and
   read back the identity binding:

   ```bash
   docker exec -i cloudagent-postgres-1 psql -U cloudagent -d cloudagent -v ON_ERROR_STOP=1 < cloudagent-state-$(date +%F).sql
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -c 'UPDATE agent_runner_hosts SET slots_used = 0'
   docker exec -i cloudagent-postgres-1 psql -U cloudagent -d cloudagent -v ON_ERROR_STOP=1 < cloudagent-undrain-$(date +%F).sql
   docker exec -i cloudagent-postgres-1 psql -U cloudagent -d cloudagent -v ON_ERROR_STOP=1 < cloudagent-policy-$(date +%F).sql
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -Atc \
     'SELECT gateway_issuer FROM agent_identity_namespace'
   ```

   `slots_used` counts running attempts on the host, and the rebuild kept
   none; a restored non-zero value would park every new task at "queued"
   once it reached `slots_total`, with a runner that authenticates and
   heartbeats normally.

   The drain needs the same treatment for the same reason. `draining` is
   written to the row and the heartbeat deliberately never clears it, so the
   restored host authenticates, heartbeats, and still claims nothing; step 6
   then fails readiness with `pinned host ... is not shared-live ready: ...
status is draining`. Replaying step 1's generated statements puts back
   exactly the hosts that were online, and leaves a host that was already
   drained or disabled alone.

   The last statement must print the issuer the old database was bound to. If
   it prints nothing, the old database had no binding: verify the original
   Gateway issuer before the rebuild, then adopt it with
   `python -m cloud_agent.identity_namespace --adopt-existing --issuer
<original-issuer>`. Do not adopt the currently configured issuer without
   that check, and do not drop the check — it is what stops the rebuild from
   silently handing one Gateway's `user_id`s to another.

6. Deploy the target commit as usual. `migrate` finds the database at head,
   the control plane starts, and the runner's next heartbeat authenticates
   against the restored `credential_hash`; readiness then sees a fresh
   heartbeat and the deploy completes. If the host stack was restarted
   meanwhile and died on `agent_host_credential_rejected`, restart it after
   step 5 — the credential it holds is valid again.

7. Prove it end to end before calling it done: create a task and confirm it
   is claimed (`SELECT state FROM agent_jobs` reaches `running`), then a
   follow-up, a cancel, a fork, a publish, and a restart of the host stack
   with a follow-up into the retained chat afterwards.

If the host registration cannot be restored, the fallback is a controlled
re-enrolment: remove the credential file on the host (the path named above),
mint an enrolment token
from the admin surface, set `AGENT_ENROLL_TOKEN` for one start, and confirm the
first heartbeat before enabling retained chats.

Conversation history is not carried over by design; the forensic dump is read
with a throwaway `postgres:16-alpine` when needed and never imported back.

### Applying migrations that are not backwards-compatible

`deploy.sh` migrates before it replaces the API, so a revision it applies has to
keep the still-running API working for the length of that window — the script
says so where it runs `migrate`. The revisions that follow the rewritten
baseline do not, and are applied in a maintenance window instead of by a rolling
deploy. (`0003_thread_level_config` and `0004_residency_on_membership` arrive in
the revisions stacked on this one; the window covers them as well.)

- `0002_request_keys_and_actors` drops the default on
  `agent_admin_audit_log.actor_id`. The API that predates it inserts audit rows
  without the column, and `log_admin_action` swallows the failure by design, so
  the window would silently lose administrator audit rows rather than fail.
- `0003_thread_level_config` drops `agent_jobs.repo`, `setup_script`,
  `execution_profile` and `mcp_servers`, which the older API selects on every
  job read.
- `0004_residency_on_membership` drops `agent_thread_residencies`, which the
  older API writes on every residency heartbeat.
- `0005_queued_follow_ups` converts `waiting` jobs to `queued` and removes
  `waiting` from the job-state constraint. The new scheduler leaves queued
  turns unclaimed until all earlier turns in the same chat are terminal.
  An older API can still insert `waiting` and claim queued turns without
  checking their predecessors, so it must be stopped before this migration.
  Downgrades also require stopped writers; the downgrade restores dependent
  queued turns to `waiting`. If a terminal intermediate turn hides an
  unfinished ancestor, it refuses the downgrade until those queued follow-ups
  are drained or cancelled because the older scheduler cannot preserve their
  execution order.
- `0008_generic_mcp_connections` empties and write-constrains the legacy
  `agent_threads.mcp_servers` column and turns
  `agent_mcp_connections` into a self-describing connection: transport,
  execution location, endpoint or launch spec and auth profile, with the
  provider name kept as an optional `template`. The legacy name list is mapped
  onto `agent_mcp_thread_bindings` where the owner has exactly one connection
  for that provider, and recorded in `agent_mcp_legacy_bindings` where they do
  not — an ambiguous mapping would let a connection created later inherit a
  session authorized against a different one. It follows `0007_attempt_turn_resources`,
  so the chain stays linear; `0009_mcp_refresh_lease`
  follows it with the lease that fences a credential refresh between workers.
  `0010_mcp_policy_revisions` adds the permission revision allocator, so saving
  or reattaching a connection cannot reuse an old approval's policy version.
  `0011_mcp_rolling_compat` restores inert compatibility columns on databases
  upgraded by the original `0008`, which dropped/renamed them. Apply through
  head before starting the updated control plane and MCP relay.

The runner holds no database connection — its heartbeats are HTTP calls to the
control plane — so stopping the control plane, the MCP relay and the web tier
leaves the database with no writer.

1. Drain the hosts, keep the generated `cloudagent-undrain-*.sql`, and wait for
   two counts to reach zero. Neither can be settled once the writers stop, so
   both are waited out here, as in step 1 of the rebuild above:

   ```bash
   docker exec cloudagent-postgres-1 psql -U cloudagent -d cloudagent -Atc \
     "SELECT count(*) FROM agent_attempts WHERE status = 'running'; \
      SELECT count(*) FROM agent_session_forks WHERE state IN ('preparing', 'materialized')"
   ```

   A fork still `preparing` or `materialized` is one its coordinator has not
   finished. `0004` refuses to run over it rather than tombstone a fork the
   reaper might still finalize, and the reaper runs inside the API — so it has
   to settle before anything is stopped. A stale `preparing` operation is failed
   after the stall window (15 minutes by default).

2. Stop the writers:

   ```bash
   docker stop cloudagent-control-plane-1 cloudagent-mcp-relay-1 cloudagent-web-1
   ```

3. Build and run the target's `migrate` image with nothing writing. As in step 3
   of the rebuild above, this runs in the deploy root with the target commit
   checked out, and the image is built before the database is touched:

   ```bash
   export AGENT_DEPLOYMENT_SHA="$(git rev-parse HEAD)"
   docker compose -p cloudagent --env-file .env -f deploy/control-plane/docker-compose.yml build migrate
   docker compose -p cloudagent --env-file .env -f deploy/control-plane/docker-compose.yml \
     run --rm --no-deps migrate
   ```

4. Undrain the hosts recorded in step 1, then deploy the target commit as usual.
   Its migration is a no-op, and readiness can now see a host that is allowed to
   claim:

   ```bash
   docker exec -i cloudagent-postgres-1 psql -U cloudagent -d cloudagent -v ON_ERROR_STOP=1 < cloudagent-undrain-$(date +%F).sql
   deploy/control-plane/deploy.sh "${AGENT_DEPLOYMENT_SHA}"
   ```

5. Run the acceptance sweep in step 7 of the rebuild above.

### Retained Docker storage on a host

Retained chat state is the Docker container writable layer, so durable
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

**Concurrency is a limit, set from the admin page.** The runner container runs
one service per host. It enrolls, heartbeats and samples capacity — the things
a machine does once — and it claims a turn only while it is under its limit,
starting a process for that turn as it claims it. That process runs the turn to
completion and exits. So the limit _is_ how many turns the host runs at once,
and everything else waits in the queue. Each turn's sandbox is capped at
4g / 2 cpus, so budget roughly that per concurrent turn.

Set it on the Servers page (_Execution slots_, the field under the numbers), or
with `PUT /admin/agent/runner-hosts/<host>/slots` and `{"slots": N}`; `null`
returns the host to its `AGENT_HOST_SLOTS` default. Nothing is started or
stopped to apply it: the host reads the number off its next heartbeat answer
and serves it from then on.

**Saving is not applying, and the page distinguishes them.** The heartbeat
reports the limit the host is serving back as `slots_total`, and the claim
admits against the lower of that and the saved number. Lowering therefore takes
effect immediately, before the host has heard about it, and turns already
running are untouched — they finish, and the host claims nothing more until it
is under the new limit; the page counts those as _finishing_. Raising takes
effect when the host confirms, one heartbeat away, and until then the row says
_applying N_. A host that is offline says _N not applied_, and one whose runner
predates remote limits says _redeploy to change_.

The bounds are two through 128. Two is the shared-live floor; 128 is a typo
guard the host checks again, and neither is a statement about what the machine
can carry — the CPU and memory on the same page are. The saved number survives
heartbeats, restarts and re-enrolment: it is a decision about the named
machine, and only _Host default_ returns it to the environment.

**A turn that never started goes back to the queue; one that started does
not.** When the host cannot start a turn's process, or that process exits
without taking delivery, the turn provably never began and the host returns it
with `POST /v1/agent/worker/jobs/<job>/release` — no attempt spent and no wait
for a lease nothing is holding. The control plane refuses that release once the
turn has been admitted to the CLI, because replaying an admitted prompt runs
the same instruction twice; from there the lease expires and the
retained-session rules decide, which can end a conversation rather than retry
it. Consecutive launch failures stop the host claiming, back off, and are
reported on the heartbeat, so the page can say why a machine with room is
taking no work.

`AGENT_HOST_SLOTS` is the limit until an admin sets one, and the only number
the deployment reads. There is no replica count any more: one runner container
per host, and scaling the service would enroll the same host twice. The
deployment check starts a turn process with nothing on its standard input and
requires it to preflight and then decline cleanly, which is the whole path a
turn takes up to the moment it would begin — so an image that cannot run turns
fails the deploy rather than the queue.

Queue order is global FIFO with no per-user fairness yet, so one user
submitting a batch can occupy every slot. Per-user concurrency quotas are
P1 work.

The script validates the required environment before starting and surfaces
the runner's own preflight verdict (gateway reachable, retained image/runtime
usable, Docker data-root correct, egress network resolvable) instead of
leaving it in a detached log. Equivalent by hand:

```bash
# Put a tested alternate version in the corresponding *_EXTRA_VERSIONS build
# argument, then select it in Admin. Default versions are shared protocol pins
# and change in source together with SHARED_SANDBOX_RUNTIME_PINS.
docker build -f deploy/sandbox/Dockerfile.agent-sandbox \
             --build-arg CLAUDE_CODE_EXTRA_VERSIONS=<exact-version> \
             -t freeinference-cloud-agent-job:latest .

AGENT_SANDBOX_IMAGE=freeinference-cloud-agent-job:latest \
AGENT_HOST_SLOTS=4 \
  docker compose -f deploy/host/docker-compose.sandbox.yml \
               up -d
```

`AGENT_SANDBOX_IMAGE` is required by the host compose rather than defaulted
(`docker-compose.sandbox.yml:236,345`), so name it explicitly; deployments tag
it per environment (`…-job:staging`, `…-job:prod`) so a rebuild on one side
cannot replace the other's image.

`claim_job` uses `FOR UPDATE SKIP LOCKED`, so every host shares one queue
with no leader and no sharding.

| Variable                                          | Default                     | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENT_SANDBOX_BACKEND`                           | `kata`                      | `process` (no isolation) refuses to start unless `AGENT_SANDBOX_ALLOW_UNISOLATED=1`. On the default `kata`, deploy refuses until the host has the shim and one real container proves it gets its own kernel; `container` is an explicit, and loudly restated, shared-kernel downgrade                                                                                                                                                                                                                                                                    |
| `AGENT_SANDBOX_IMAGE`                             | required                    | Image built from `Dockerfile.agent-sandbox` or an equivalent deployment-owned image                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `*_EXTRA_VERSIONS`                                | blank                       | Comma-separated tested versions to bake beside each source-reviewed default. Select one for new chats in Admin; retained chats keep the version they started with. The five defaults are not host settings because runner/control-plane capability admission attests them as one protocol unit                                                                                                                                                                                                                                                           |
| `AGENT_SANDBOX_NETWORK`                           | _no default_                | Legacy alias for the `platform_only` discovery backplane, honoured so an existing deployment keeps working. It deliberately has no default (`sandbox.py`: "AGENT_SANDBOX_NETWORK is not set. There is deliberately no default"), and a tier with no network is a preflight failure rather than a fall back to a more open one. Current setting: `AGENT_EGRESS_NETWORK_PLATFORM_ONLY`, which compose defaults to `agent-egress`. A chat is attached to its own derived `freeinference-chat-net-*` bridge, not directly to this shared network             |
| `AGENT_EGRESS_SETUP_TIER` / `_AGENT_TIER`         | `trusted` / `platform_only` | One of `platform_only` / `trusted` / `custom` / `full`, **per phase**. Dependency setup reaches the checked-in allowlist proxy; the model-driven Agent CLI phase stays on the closed relay-only network                                                                                                                                                                                                                                                                                                                                                  |
| `AGENT_EGRESS_NETWORK_*`                          | —                           | Network per tier. A tier with no network is an error when a phase selects it, never a fall back to a more open one                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `AGENT_EGRESS_CHAT_NETWORK_POOL`                  | `10.240.0.0/16`             | Non-overlapping private IPv4 pool split into daemon-arbitrated `/29` chat/phase bridges. Bridges are removed at phase transition or stop and rebuilt on wake                                                                                                                                                                                                                                                                                                                                                                                             |
| `AGENT_EGRESS_ALLOWLIST`                          | —                           | Checked at startup: it may not contain an agent vendor's telemetry domain, which would let a "closed" sandbox report on the repository it was given                                                                                                                                                                                                                                                                                                                                                                                                      |
| `AGENT_RUNNER_HOST`                               | host name                   | Enrolled execution environment, shared by every turn it runs — the unit the admin placement switch picks between. `deploy/host/sandbox_host.sh` fills it from the host's own name. Never derived inside the container, where the hostname is a container id. After enrolment the persisted credential is authoritative, so editing this renames nothing. Unset keeps the environment out of the pool and changes nothing else. Distinct from `AGENT_PHYSICAL_HOST`, which is the display-only inventory name grouping environments that share one server |
| `AGENT_PHYSICAL_HOST`                             | host name                   | Physical server this environment runs on, reported at enrolment as the `physical_host` label and used to group the admin inventory. `sandbox_host.sh` defaults it to `hostname -s`; set it explicitly when invoking Compose directly. Never a container hostname or a runtime suffix                                                                                                                                                                                                                                                                     |
| `AGENT_HOST_SLOTS`                                | `3`                         | Turns at once until an admin sets a number on the Servers page; at least two, at most 128. `AGENT_RUNNER_REPLICAS` is no longer read                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `AGENT_RETAINED_DOCKER_DATA_ROOT`                 | `/var/lib/docker`           | Expected daemon `DockerRootDir`; provision this root on durable storage. It is verified by the host and broker and is never mounted into a chat container                                                                                                                                                                                                                                                                                                                                                                                                |
| `AGENT_HOST_STORAGE_PROBE`                        | `overlay_root`              | Which probe measures the filesystem holding the sandbox writable layers, shown as the Servers page's Storage column. `off` stops measuring storage on that host and is the rollback; an unrecognized name reports `unsupported` rather than measuring something else. A reading is published only for a topology the host deployment has attested; see _How full is the sandbox storage?_                                                                                                                                                                |
| `AGENT_SANDBOX_UID` / `_GID`                      | `10001`                     | Only for a custom sandbox image; must match its user                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `AGENT_REPO_ALLOWLIST`                            | —                           | Comma-separated `owner/name`, or `owner/*`, for the single-tenant dogfood. Users who connect the App themselves do not need it; unset simply means the only entitlement is a user's own connection                                                                                                                                                                                                                                                                                                                                                       |
| `AGENT_GITHUB_APP_CLIENT_ID` / `_CLIENT_SECRET`   | —                           | The App's OAuth half. Only the user-facing connect flow needs it; minting installation tokens uses the private key alone                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `AGENT_GITHUB_APP_INSTALL_URL`                    | —                           | GitHub App installation URL, `https://github.com/apps/<slug>/installations/new`. Used when OAuth finds no accessible installation, and behind _Configure repositories_ for a connected user. The control plane adds a short-lived, user-bound `state`; set the App callback to `<frontend>/agents/connected?provider=github`                                                                                                                                                                                                                             |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` | —                           | GitLab.com OAuth application credentials, kept on the control plane. Configure only `read_user` and `read_api` scopes                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI`                 | —                           | Exact registered callback, normally `<frontend>/agents/connected?provider=gitlab`. HTTPS is required except for localhost development                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK`                | —                           | Accepts a non-`internal` sandbox network. Preflight refuses one otherwise, so a missing setting cannot quietly mean full egress                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `AGENT_SANDBOX_MCP_URL`                           | —                           | MCP-only relay URL as a sandbox sees it. The standalone host compose derives it from the MCP tunnel alias; never point it at inference or the full control API                                                                                                                                                                                                                                                                                                                                                                                           |
| `AGENT_MCP_TUNNEL_*`                              | —                           | Remote MCP-only listener destination and independent restricted SSH key/known-hosts; see `ENVIRONMENT.md` for the complete set                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `AGENT_GITHUB_TOKEN`                              | —                           | Control-plane fallback publisher credential; unset means the publisher idles                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `AGENT_PUBLISH_BASE_BRANCH`                       | `dev`                       | What a draft PR targets when its job named no branch of its own. A job started from a branch targets _that_ branch; this is only the fallback                                                                                                                                                                                                                                                                                                                                                                                                            |

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
GitLab projects do not appear in the Agent task composer, and the control plane does
not claim to read their source or publish GitLab merge requests yet.

### How full is the sandbox storage?

The Servers table has a **Storage** column beside CPU and Memory: how much of
the filesystem holding the retained sandbox writable layers is in use, in `df`
terms. It answers a question the sandbox count cannot — `25 stopped` says
containers are retained, not how much disk they hold — and it observes one
filesystem, not one chat: a repository, its dependencies, uncommitted changes
and the native CLI session all live in a container's writable layer, and
stopping a container does not release it.

The reading itself is cheap and local. A runner is an ordinary container under
the same Docker daemon and storage driver as the sandboxes, so OverlayFS's
`statfs` semantics make `os.statvfs("/")` inside it describe the upper layer's
filesystem — the disk the writable layers are on. No host mount, no Docker
query, no child process. The figures are `df`'s: `used` is `total - free`,
`available` is what an unprivileged writer may still use, and the percentage is
taken over `used + available`. The blocks the filesystem reserves for root that
are still free are in neither figure — which is why the two do not add up to
the total, and why `total - available` is not used space.

**Why a deployment has to attest it first.** Docker's `overlay2.size` (and a
container's `--storage-opt size=` on XFS with pquota) is implemented with project quotas,
and a quota is what `statfs` returns for a directory inside the project. A
container reading its own quota, and a host running `df <upperdir>`, would then
report the _quota_ — 10 GiB, say — with the same device number, so comparing
those two proves nothing. `sandbox_host.sh` therefore compares the container's
numbers against `df` of the **mount root** of the filesystem containing the
upper directory, which no container project covers, and writes the verdict to
`.storage-probe/verdict.json` beside `.shared-live-ready`:

- the mount root's size must equal the container's own `total_bytes` exactly —
  `size` does not move under writes, so this is the topology test;
- `df <upperdir>` and `df <mount_root>` must agree on size, and `daemon.json`
  / the `dockerd` command line must not mention `overlay2.size`; either
  disagreement is recorded as `unsupported` (project quota);
- `used` and `available` are compared within `max(1 GiB, 1% of size)` and only
  as a diagnosis, because builds and cleanup interleave and nothing pauses the
  world for monitoring.

A non-root deployment user may be unable to traverse Docker/containerd's
root-owned snapshot directories. When direct mount lookup fails, the deployment
uses the running replica's immutable image ID for a temporary metadata helper.
Docker resolves two non-recursive, read-only directory binds: the candidate
mount root and that replica's upper directory. The helper checks the actual
binds' device, filesystem root and type against the host mount-table plan before
reading counters. A guessed path or mismatched mount never becomes verified.

The helper has no network, capabilities, host PID namespace, Docker socket or
credentials. Its root filesystem is read-only; it only reads its own mount
metadata and calls `fstatvfs` on the two bound directories. It shares the
attestation's remaining deadline, has its own maximum five-second alarm, and is
removed on exit. Failed starts are cleaned up within the remaining budget. This
is a deployment-only mount exception; collectors and job containers do not get
these host views. No sudo rule or host directory permission change is needed.

Each runner reads that file every sampling round and publishes a number only for
its own upper directory, with a matching size. Anything else — no file, no
entry, an entry that says `unsupported`, a resized filesystem — is reported as
`unverified` / `unsupported` and the page says so instead of drawing a number.
A rebuilt container gets a new upper directory, so an old attestation stops
applying on its own; there is no time-based expiry.

Compose binds only the dedicated `.storage-probe/` directory read-only at
`/run/cloud-agent/storage-probe`. Each sample opens `verdict.json` by name, so
deleting it reports `unverified` on the next sample; `sandbox_host.sh check`
recreates it and sampling resumes without restarting replicas. Keep the bound
directory itself in place. The file inside it is written beside itself and
renamed over, never truncated first, so a replica sampling during a refresh
reads either the old document or the new one and never an empty one. Upgrade from the old `.storage-probe-verdict` file
bind with a normal `up`, which recreates runners with the new mount; `check`
alone cannot change an existing container's mount configuration. The legacy
file is ignored and is not used as a fallback.

Where the helper is used, it needs **Docker 25 or newer on the host**. It binds
its two directories non-recursively, which `--mount` spells
`bind-recursive=disabled` — parsed by the CLI itself, and added in 25. The
older spelling is not an alternative to fall back on: a current CLI refuses it
outright — 29.2.1 answers `bind-nonrecursive=true` by naming its replacement
and exiting 125 — so a build that reaches for it to support older hosts loses
every host that is not old. Below 25 the helper call fails, the host reports
`unverified`, and the warning quotes the CLI's own complaint. Hosts whose
deployment user can traverse the storage root never take this path at all.

**Storage verification never blocks a deployment.** A metric that cannot be
established is not a reason to keep a working host out of the pool: every
failure above is a warning and a verdict entry, and `up` continues. The two
operational consequences:

```bash
# Re-run the checks without redeploying — this also refreshes the verdict.
deploy/host/sandbox_host.sh check

# Turn the metric off entirely (the rollback). The `.env` is the deploy
# checkout root's, beside `.deployed-sha`, which is where every reader looks.
echo 'AGENT_HOST_STORAGE_PROBE=off' >> .env
deploy/host/sandbox_host.sh up
```

The `up` is not optional. A container's environment is fixed when it is
created, so replicas started before the edit keep measuring until they are
replaced; `up` replaces them. A `check` in between is still worth running — it
rewrites the verdict with no entries, which stops those replicas publishing a
figure on their next sample even though they are still measuring. The variable
takes `overlay_root` or `off` and nothing else: any other spelling is refused
at configuration time rather than leaving the script and the runner to disagree
about what it meant.

**What the number is not.** It is block space, so `available` does not promise
that any particular container can still write — inodes, quotas and a read-only
remount still fail. It includes every other writer on that filesystem, so it is
not the sum of Docker objects and it does not predict how many more sandboxes
fit. On the default host deployment the rollout also stops reporting if the
mount root itself is inside a size-limited project quota, which `df` cannot
distinguish; the configuration check is best effort. Rootless, remote-Docker,
btrfs/ZFS and devicemapper hosts are unverified: the probe reports
`unsupported` rather than guessing. On Docker Desktop the host shell cannot see
the VM's mounts, so local development shows `unverified`.

### Which machine runs the jobs

**Admin → Servers.** The list is every enrolled execution environment with
authenticated heartbeats, grouped by the physical server it reported, and the
**New chats** selector picks where a chat with no sandbox may be materialized.
Its default option is **Automatic**, which means whoever polls first takes a
new chat. Once a retained group exists, its turns can be claimed only by that
group's owning environment.

Runners _pull_, so this is a gate on the claim rather than a dispatch target —
the gateway cannot push a job at a machine, and the only moment it gets to say
"not you" is when a runner asks for work. Three consequences worth knowing
before using it:

- **The switch is not preemptive, and not a drain.** The new host starts
  claiming chats that do not have a sandbox yet; jobs already running on the
  old one keep running to the end, holding their leases. Existing retained
  chats stay hard-pinned to the host that owns their Docker writable layer, so
  later follow-ups can still be claimed there. There is no cross-host migration
  in this phase. The two hosts can therefore overlap after the switch — a drain
  would mean waiting for the old host to empty first, and this does not do that.
- **An unenrolled runner cannot claim.** The control plane derives the host
  from its persisted host credential; a request-body name cannot impersonate a
  pinned machine.
- **A machine joins through one-time enrollment.** `AGENT_ENROLL_TOKEN` is
  bound to the chosen host name and redeemed once. Replicas then share the
  persisted host credential and heartbeat independently of claim traffic.
- **There is no storage failover.** If the selected host stops, new chats wait
  rather than materializing somewhere else. Existing retained chats likewise
  wait when their own host is unavailable; changing the selection cannot move
  their Docker layer. The admin page warns when the selected host has not
  polled recently, but the authoritative operational signals are its enrolled
  heartbeat and broker probe, not whether one runner happened to claim recently.

Adding a _second_ machine is not only this switch. A sandbox reaches the
gateway over a per-chat network declared `internal: true`, which routes nowhere
off its own host — so a runner box that is not the gateway's host needs the
routable-network configuration from
[§0 above](#0-a-closed-agent-network-needs-both-narrow-relays-on-its-backplane) (or local
instances of both relays). Preflight refuses at startup rather than failing
jobs at their first model or MCP call, but it refuses _there_, on that machine,
not here.

### Running runners on another machine

A second machine is worth adding for capacity, or because it is the one with
the hardware. What makes it more than "run the script over there" is that a
sandbox's agent phase uses its own network declared `internal: true` — no route
off the host at all. A remote host therefore supplies two fixed-destination
relays on a discovery backplane; lifecycle code attaches those relays to each
chat-private network. A separate control-plane relay remains visible only to
the runner:

```text
runner host                                      service host(s)
┌──────────────────────────────────────┐
│ [sandbox, per-chat agent network]    │
│   ├→ gateway-tunnel ─────────────────┼──ssh──→ inference :8080
│   └→ mcp-tunnel ─────────────────────┼──ssh──→ MCP-only :8001
│                                      │
│ [runner, internal network]           │
│   └→ control-plane-tunnel ───────────┼──ssh──→ control API :8000
│                                      │
│ [sandbox, setup network] → proxy ────┼────────→ allowlisted registries
└──────────────────────────────────────┘
```

The sandbox's agent-phase world is exactly the inference and MCP relays. Each
tunnel is a relay, not a proxy — no CONNECT and no caller-chosen destination —
so nothing the agent sends can widen either route. Setup is a distinct phase
and reaches package registries only through the allowlist proxy described
below; the control-plane tunnel never joins either sandbox network.

**Why ssh rather than exposing the gateway's port.** Sandboxes carry scoped
inference grants and runners carry enrolled-host credentials. Binding either
service to the lab network puts those capabilities in cleartext on a shared
segment. The tunnel keeps each service listening on loopback.

```bash
deploy/host/sandbox_host.sh up 4     # relays, broker, proxy + 4 runners
deploy/host/sandbox_host.sh check    # probe relays and runner/broker paths
deploy/host/sandbox_host.sh status
deploy/host/sandbox_host.sh down
```

`deploy/host/docker-compose.sandbox.yml` is standalone and contains
only the runners, their workspace broker, three purpose-scoped tunnels, the
setup allowlist proxy/config renderer, and their private networks — no
`backend` service. That was the point of it in the source repository, where the
alternative overlay declared `depends_on: backend` and, pointed at a machine
with no gateway, did not fail but started one there. It is the only topology
this repository ships.

| Variable                                        | Notes                                                                                                                                                                                                                            |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENT_TUNNEL_*`                                | Inference relay destination, key, known-hosts file and ports; remote default `127.0.0.1:8080`                                                                                                                                    |
| `AGENT_MCP_TUNNEL_*`                            | MCP-only relay's independent destination and SSH material; remote default `127.0.0.1:8001`. Do not point it at the full control API on 8000                                                                                      |
| `AGENT_CONTROL_PLANE_TUNNEL_*`                  | Runner-only control API relay; remote default `127.0.0.1:8000`. It is deliberately absent from sandbox networks                                                                                                                  |
| `AGENT_*_TUNNEL_SSH_KEY`                        | Private key, mounted read-only. Use a separate restricted key per relay with `command="",restrict,port-forwarding,permitopen="127.0.0.1:<that relay's port>"`; no relay needs a shell or permission to open another relay's port |
| `AGENT_*_TUNNEL_SSH_KNOWN_HOSTS`                | Required host-key file for each relay. Host checking is not disableable: an unverified hop would hand scoped tokens and tunnel traffic to whatever answers there                                                                 |
| `AGENT_GATEWAY_HOSTNAME` / `AGENT_MCP_HOSTNAME` | The two names a sandbox resolves on the closed network, `agent-gateway` and `agent-mcp` by default. Do not replace them with routable service addresses                                                                          |
| `AGENT_CONTROL_PLANE_HOSTNAME`                  | Runner-only control relay name, `agent-control-plane` by default                                                                                                                                                                 |

Generate the known_hosts entry deliberately, and look at it:

```bash
ssh-keyscan -p 22 <gateway-host> > /etc/agent-tunnel/known_hosts
```

Then `sandbox_host.sh check` answers the only question that matters, from
the vantage points whose answers mean anything: sandbox-runtime containers on
the closed network must receive HTTP 200 from inference `/health` and MCP
`/healthz`; a live runner must also pass its full sandbox preflight and reach
the broker and control plane. Reaching those services from the host's own shell
proves nothing about the paths the deployed containers use.

Once it is up, the machine appears in **Admin → Servers** and can be selected
there.

#### The way back: the control plane reaching this host's broker

The tunnels above all point one way — out of this machine. One thing needs to
come the other way.

Every file listing, terminal keystroke and `git status` for a job goes **control
plane → workspace broker**, and the broker runs here, beside the runner whose
Docker daemon owns the retained workspace. The control plane resolves which
broker from the host row, which holds whatever the runner reported at
enrolment. The integrated local Compose stack shares a network, so
`http://workspace-broker:8092` works there. The supported standalone host stack
does not share a network with the control-plane stack—even when the two
projects run on one physical machine—so that service name resolves nowhere.
The control plane stores it, dials it, and gets a DNS failure it surfaces as
**503** — including
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

| variable                               |                                                                                                                                                                                                                                                                                                           |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENT_BROKER_REVERSE_BIND`            | The **control plane host's docker bridge gateway**. Not `127.0.0.1`, which its containers cannot see; not `0.0.0.0`, which publishes the broker to that machine's whole network behind one shared token. Find it with `docker network inspect <net> --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'` |
| `AGENT_BROKER_REVERSE_PORT`            | A free port on that host                                                                                                                                                                                                                                                                                  |
| `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` | `http://<bind>:<port>` — what the runner reports, and what the control plane will dial                                                                                                                                                                                                                    |

A non-loopback remote bind requires `GatewayPorts yes` or `clientspecified` in
the **control plane host's** sshd. Without it the bind is refused and, because
the tunnel runs with `ExitOnForwardFailure`, ssh exits and the container
restarts rather than sitting there having bound nothing.

Set all three. The supported standalone host stack never publishes its broker
and does not share a Compose network with the control plane, even when both
stacks happen to run on the same physical machine. An advertise URL with no
tunnel behind it is an address nothing answers on, which is the same 503 with a
longer explanation.

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
`Dockerfile.agent-runner`, which adds a Docker client to create/inspect the
retained chat container and `git` to prepare a credential-scrubbed source
archive. The gateway image has neither, and a runner built from it fails
preflight on every host.

**There is no host worktree mapping.** For the first turn, the trusted runner
uses a temporary directory only long enough to fetch the requested commit,
removes all repository credentials, normalizes ownership in a tar stream, and
imports that stream into `/workspace` in the retained container. The temporary
clone is then deleted. All subsequent edits, provider state, terminals, setup
results, and fork siblings remain in that same Docker writable layer.

**The Docker socket is the host authority.** Runner and broker can create,
start, stop, inspect, and exec in chat containers, so isolating those services
from users matters more than changing their Unix uid. The security boundary is
the retained sandbox (Kata for untrusted multi-tenant work; plain Docker only
for trusted repositories), not a host bind mount or a second per-turn
container.

### Dependency installation, and the proxy that allows it

A chat may carry a `setup_script`. It runs inside the retained container before
the first Agent CLI turn, under its own egress tier. Its files persist in that
container's Docker writable layer across follow-ups, idle stop/start, and host
service restarts; there is no host-side setup cache or portable setup archive.

The tier is where the work is. `setup` defaults to `trusted` and `agent` stays
`platform_only`, which in this deployment means three compose-level networks
plus one derived internal bridge per chat and selected phase:

| Network                    | Internal | Who is on it                                                                             |
| -------------------------- | -------- | ---------------------------------------------------------------------------------------- |
| `agent-egress`             | yes      | discovery backplane for inference and MCP relay containers; sandboxes are not left on it |
| `agent-egress-trusted`     | yes      | discovery backplane for the setup proxy; sandboxes are not left on it                    |
| `agent-egress-uplink`      | **no**   | the proxy, alone                                                                         |
| `freeinference-chat-net-*` | yes      | one sandbox group plus only the relay(s) or proxy required by that phase                 |

The supported host script derives project-specific names for the two new
Trusted/proxy networks, so staging and production on one Docker daemon do not
share them. The historical PlatformOnly backplane name is never renamed
automatically; a host with `SIBLING_COMPOSE_PROJECTS` must set its existing
unique `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` explicitly. On first activation
after this upgrade, a stopped retained chat still attached to that old shared
backplane is moved onto its deterministic private bridge before it starts. A
running legacy attachment fails closed until the chat is stopped; it is never
hot-detached.

Both backplanes and every derived sandbox network have no route of their own.
The difference is that `agent-egress-proxy` sits on the Trusted backplane with
a second uplink leg; it is also attached, under the same alias, to a chat's
private setup network. The sandbox is handed `http_proxy`/`https_proxy`
pointing at it. The environment variables are a convenience for package
managers, not the boundary: a setup script that ignored them finds a network
that cannot route anywhere.

With the supported `platform_only` agent tier, the agent turn — the part driven
by untrusted model output — still reaches only the fixed inference and MCP
relays. Opening the separate setup phase does not change that. Selecting an
agent-phase `trusted`, `custom`, or `full` tier is an explicit wider operator
policy, not a side effect of setup access.

The renderer's named volume contains only generated Squid configuration. It is
host infrastructure, not chat state: Docker inspection of every chat container
still reports no bind or volume mounts.

**Adding a domain.** `AGENT_EGRESS_ALLOWLIST` is _added to_ the built-in
registry list (PyPI, npm, crates, Go, RubyGems, Maven, github.com), never a
replacement for it. `*.example.com` covers subdomains. An agent vendor's
telemetry domain is refused outright — a sandbox that can phone home about a
private repository is not closed, whatever the tier is called. The list is
rendered into the proxy's config by `agent-egress-proxy-config`, a one-shot
service that runs before the proxy and fails the deploy if the list is
unusable. Editing that config by hand skips the validation that keeps a
hostile value out of it.

The allowlist and proxy port are part of the proxy service's Compose
configuration fingerprint. Changing either recreates the renderer/proxy instead
of leaving a running Squid on stale ACLs. The renderer fsyncs a temporary file
and atomically replaces `squid.conf`, so a restart can observe either the old
complete policy or the new complete policy, never a partially rewritten ACL.

**Three preflight failures and what each means.** The runner refuses to claim
jobs unless the setup tier is genuinely enforcing:

- _"the sandbox network … is not `internal`"_ — a tier variable points at a
  routable network. The setup phase would have the whole internet while the
  config still said `trusted`.
- _"egress proxy is unreachable"_ — the proxy is not running, or not on that
  network. Check `docker compose logs agent-egress-proxy`; a config Squid
  rejects shows up there as a parse error on the first line. This one stops the
  runner claiming **any** job, including jobs that install nothing — deliberate,
  because it means the deploy is broken, but if this host genuinely does not
  need dependency installation, `AGENT_EGRESS_SETUP_TIER=platform_only` skips
  the tier and its probe entirely.
- _"egress proxy answered … it is not enforcing an allowlist"_ — the proxy
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

The MCP suites live here with the catalogue, per-user registry, relay, filters,
API and runtime adapter. After changing that path, run at least:

```bash
uv run pytest tests/unit/test_mcp_connection_config.py \
  tests/unit/test_mcp_protocol_and_authorization.py tests/unit/test_mcp_proxy.py \
  tests/servers/test_mcp_relay.py tests/servers/test_agent_jobs_api.py \
  tests/unit/test_agent_runtimes.py
```

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

`deepseek-v4-flash` replied _"I've added a docstring to the `greet` function.
It now reads: ..."_ immediately after receiving a tool result with
`is_error: true` saying the write had been denied. Nothing had changed on
disk.

This is why the runner reports what the tools did rather than what the agent
says about them: it collects errored `tool_result` entries, emits them into
the owner's stream, and refuses to record a run as successful when it produced
no patch _and_ a tool failed. A model's own account of its work is not
evidence.

## What changed when this moved

Task H5 moved this page from the source repository's
`docs/developer/agent-sandbox-operations.md` at the freeze commit `764a6f97`.
Under the migration's move-as-is rule the initial edits were **paths and the
artifacts that did not move**. The table below records that baseline; later
feature work has intentionally brought the live operational sections above up
to date.

| Source                                                                                                                                                    | Here                                                       | Why                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ops/setup/setup_kata_runtime.sh`                                                                                                                         | `deploy/host/setup_kata_runtime.sh`                        | D7                                                                                                                                                                                                                        |
| `ops/deploy/sandbox_host.sh`                                                                                                                              | `deploy/host/sandbox_host.sh`                              | D7                                                                                                                                                                                                                        |
| `deploy/docker/Dockerfile.agent-sandbox`                                                                                                                  | `deploy/sandbox/Dockerfile.agent-sandbox`                  | D7                                                                                                                                                                                                                        |
| `ops/deploy/agent_runner.sh up/preflight/status/down`                                                                                                     | the standalone script's `up/preflight/check/status/down`   | The co-located overlay was dropped (MIGRATION.md, open decisions, decided 2026-08-04). The standalone preflight validates an already-built image; `up` builds the target image and then repeats the authoritative checks. |
| `docker compose -f docker-compose.yml -f docker-compose.agent-runner.yml`                                                                                 | `docker compose -f deploy/host/docker-compose.sandbox.yml` | Same decision. There is no base compose here and no `backend` service.                                                                                                                                                    |
| "Use that script, not `agent_runner.sh`"                                                                                                                  | a note that the standalone compose is the only topology    | Same decision.                                                                                                                                                                                                            |
| The staging deploy-pipeline paragraph (`deploy_staging.sh`, the same-invocation requirement, the overlay attaching `backend` to the agent-egress network) | removed                                                    | It describes the _gateway's_ deploy pipeline driving the overlay that did not move. Keeping it would be an instruction nobody here can follow.                                                                            |
| `AGENT_RUNNER_HOST` row naming `agent_runner.sh` and `deploy_staging.sh`                                                                                  | names the standalone script                                | Same.                                                                                                                                                                                                                     |
| `MCP_CONFIG_PATH` / `config/examples/mcp.example.yaml`                                                                                                    | initially marked gateway-side                              | The move-only baseline still used the old gateway registry. Subsequent extraction work replaced it with this service's in-code catalogue, per-user connections and isolated MCP relay.                                    |
| `pytest … test_agent_mcp.py test_agent_mcp_api.py`                                                                                                        | initially `pytest tests/unit/test_agent_runtimes.py`       | The move-only baseline left the proxy suites behind. They were subsequently ported and expanded in this repository.                                                                                                       |

### Historical statements that remain historical

- **The pre-split verification record.** Where a row explicitly describes an
  old live measurement, it remains evidence of that historical run rather than
  a claim that a new deployment gate has been exercised. Current component
  ownership and operating instructions use the post-split topology.
- **Cutover inventory.** Secret provenance and one-time migration steps remain
  in [cutover.md](cutover.md) and `ENVIRONMENT.md`; this page documents the
  resulting runtime topology.
