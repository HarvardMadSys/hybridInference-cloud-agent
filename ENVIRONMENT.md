# Environment inventory

Every variable the cloud agent stack reads, which deployment unit reads it, and
who provisions it.

Names carried over from HybridInference are kept **unchanged** so that moved
files work without edits (see the move-as-is rule in [AGENTS.md](AGENTS.md)).
Rows marked **NEW** are introduced by the split.

> **This file is public. It names variables, never values.**
>
> No identifiers, hostnames, ports, tunnel topology, or app registrations belong
> here — not because they are secret in the cryptographic sense, but because a
> published inventory of a deployment's internals is a gift to anyone probing it,
> and this repository ships to everyone who clones it. Real values live in the
> operator's private deployment overlay.
>
> A row that needs a concrete example uses an obvious placeholder
> (`agents.example.org`). If you find yourself pasting something from a running
> deployment, it goes in the overlay instead.

## The one rule

> The gateway's `API_KEY_SECRET` and `JWT_SECRET_KEY` must never appear in this
> repository, in its configuration, or on any host that runs it.

Inference grant tokens are minted *and* verified by the gateway, so the secret
stays there. This service verifies identity tokens with a **public** key from
JWKS, and signs its own tokens with its own secrets. If a task ever seems to
need a gateway secret here, the design is wrong — stop and escalate.

## Provisioning status

| | |
|---|---|
| Staging values provisioned | ✅ 2026-08-06 (`docs/cutover.md`, H2) |
| Production values provisioned | ✅ 2026-08-07 (`docs/cutover.md`, H3) |
| Secret storage | deploy host environment / unit env files, and read-only file mounts for keys. **Never** in git. |

Both environments have been serving traffic since those dates: `README.md`,
`docs/cutover.md` and `deploy/control-plane/README-cd.md` are the deployment
record. This table is about the *values* being in place, not about a particular
release being deployed — a pending rollout belongs in that record, not here.
What is still missing on the production side is spelled out in
`deploy/control-plane/README-cd.md` § *Production — what is still missing*.

## Control plane (`cloud_agent`)

### Local and CI permission regression

`AGENT_PERMISSION_TEST_IMAGE` enables the opt-in Linux UID regression in
`tests/external/test_shared_member_permissions.py`. It names an already-pulled
Docker image with Python 3 and GNU `find`/`stat`, such as `python:3.12-slim`.
Run `AGENT_PERMISSION_TEST_IMAGE=python:3.12-slim uv run pytest -q -m external tests/external/test_shared_member_permissions.py` after pulling the image.
The test uses disposable containers without network access or mounts and runs
the production normalizer as separate member UIDs without capabilities. CI runs
it before deployment; it needs no model API key or deployed-service setting.

### Database

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_DATABASE_URL` | **NEW** | Postgres DSN for the agent-owned database. Must be a database this service owns outright — sharing one with the gateway would reintroduce the coupling the split exists to remove. |
| `TEST_DB_HOST` / `TEST_DB_PORT` / `TEST_DB_NAME` / `TEST_DB_USER` / `TEST_DB_PASSWORD` | **NEW** | Read by the `dbtest` tier — `make test-db` locally, and the same tier run directly in CI. The fixtures read these five, not a DSN. Defaults: `localhost` / `5432` / `hybridinference_test_db` / `postgres` / `postgres`. `TEST_DB_NAME` must contain `_test_`; the fixtures refuse to run otherwise, so they cannot be pointed at a real database by a stray environment. Under `pytest -n`, each xdist worker appends its own id and needs that database to already exist — which is why CI runs this tier serially. |

### Gateway contracts

| Variable | New? | Value / who provisions |
|---|---|---|
| `GATEWAY_BASE_URL` | **NEW** | Origin of the HybridInference gateway this deployment federates with, e.g. `https://gateway.example.org`. |
| `GATEWAY_JWKS_URL` | **NEW** | `${GATEWAY_BASE_URL}/v1/identity/jwks` (task C1). Publishes the current signing key plus any retiring ones, so cache by `kid` and re-fetch on a miss rather than pinning a single key. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | **NEW** | Bearer token authorizing this service to mint, renew, revoke and read usage for inference grants. Random 32 bytes, the same value on both sides, and distinct per environment — one shared across staging and production makes a staging compromise a production one. |
| `GATEWAY_GRANT_BASE_PATH` | optional | Grant service route prefix, default `/internal/agent-grants`; applies to mint, renew, revoke and usage queries. Use an absolute URL path without a query or fragment. Reload the control plane after changing the gateway binding. |
| `GATEWAY_GRANT_BINDING_ID` | optional; recommended before the first ledger-enabled deployment | Nonsecret identity of the logical gateway installation. When set, usage snapshots pin this identity across URL, grant-prefix and credential migrations. Change it when switching installations, including at the same URL. Without it, the fingerprint uses `GATEWAY_BASE_URL` and `GATEWAY_GRANT_BASE_PATH`, so changing either prevents refreshing old snapshots. Credential rotation always preserves the binding. Configure this only in the control plane; service credentials never enter snapshots or the browser. |
| `GATEWAY_ISSUER` | **Required for Gateway access** | Exact `iss` signed by the Gateway. It identifies the user namespace and need not equal the HTTP origin. Recorded in the Agent database; changing it requires a separate database or an explicit ownership migration. See [minimal identity migration](docs/minimal-identity.md). |
| `GATEWAY_AUTHORIZE_URL` | **NEW** | `${GATEWAY_BASE_URL}/authorize` (task C4). The consent page a sign-in starts at. **Server-side now**: the control plane assembles the whole PKCE round trip, so the web app no longer carries its own copy. Unset means this deployment cannot sign anyone in, and it says so instead of redirecting to a half-built URL. |
| `AGENT_SESSION_REDIRECT_URI` | **NEW** | The public URL of this service's `GET /v1/session/callback`, e.g. `https://agents.example.org/api/v1/session/callback`. **Must appear verbatim in the gateway's `IDENTITY_ALLOWED_REDIRECTS`** — that allowlist is matched as an exact string, so a byte of difference is a refusal at `/authorize` before anyone reaches consent. Configured rather than derived from `Host`/`X-Forwarded-*`, because deriving it would let whatever sits in front of this service decide where sign-in returns to. |
| `AGENT_WEB_BASE_URL` | **NEW** | Where the browser lands once the session cookie is set: the web app's public origin including any base path, e.g. `https://agents.example.org/agents`. Also where an unconfigurable sign-in sends the visitor, with `?sign_in_error=unavailable`, so a misconfigured deployment says so rather than looping. |
| `AGENT_GATEWAY_URL` | existing | Gateway URL as seen *from inside the sandbox* — the relay address, not the public URL. Differs from `GATEWAY_BASE_URL` by design. |

### Own secrets

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | **NEW** | Wraps stored GitLab/GitHub credentials, **and each user's MCP connection credentials** — one key to deploy and rotate, with the two kinds derived under different domain separators so a ciphertext from one table cannot be read or planted as the other's. Unset leaves both unconfigured rather than storing anything in the clear: MCP then offers an empty picker instead of failing the page. Replaces the gateway-derived key those secrets were encrypted with before the split, which is why migrating them needs a re-wrap step and not a copy (task H1). Generate random 32 bytes, unique per environment. |
| `AGENT_CONTROL_TOKEN_SECRET` | **NEW** | Signs host/worker control tokens. Replaces the old derivation from the gateway's `API_KEY_SECRET` (task E3). Generate random 32 bytes, unique per environment. |
| `AGENT_SESSION_SECRET` | **NEW** | Signs the web session cookie (task E8). Generate random 32 bytes, unique per environment. |
| `AGENT_DEFAULT_EXECUTION_PROFILE` | **NEW** | Server-owned policy for new root threads. `untrusted-kata-v1` requires an enrolled Host whose current credential still matches and whose exact claiming replica attests `io.containerd.kata.v2` with a guest kernel different from the Host. `trusted-v1` permits the retained container to use runc for trusted internal/dev repositories. Follow-ups and forks inherit the stored profile; callers cannot select it. |
| `AGENT_ONCALL_DISPATCH_TOKEN` | **NEW** | Authenticates the gateway alert relay on `/v1/agent/service/oncall/*` — one credential, one door. Random 32 bytes, same value in the relay's `.env.oncall`, distinct per environment. Unset opens nothing (the door then answers only to `ADMIN_TOKEN`). |
| `AGENT_ONCALL_USER_ID` | **NEW** | Gateway user id every on-call job runs as. Its role decides which models the job's grant may carry (`glm-5.2` needs `internal`+), and its quota is what the analysis spends. Unset: the on-call door answers 503, the rest of the service is unaffected. |
| `AGENT_RESIDENCY_TTL_S` | **NEW** | How long a member's independently fenced live-process residency and short-lived inference grant survive between host heartbeats. Default 1800. Shared-live uses one residency per chat membership. This is credential/process ownership, not a second chat lifecycle clock: `AGENT_CHAT_IDLE_TIMEOUT_S` alone decides when a settled retained sandbox group is stopped. |
| `ADMIN_TOKEN` | existing | The machine-to-machine admin credential (task E7). **Unset opens nothing** — `verify_admin_access` refuses an empty configured value rather than matching an empty presented one — so a deployment that skips it has no token door, only the admin-session one. |

### Behaviour switches read by the control plane

| Variable | New? | Value / who provisions |
|---|---|---|
| `COMPOSE_PROFILES` (`edge`) | **NEW** | Compose-level, read by `docker compose` not by this service. `edge` starts the nginx edge for a deployment that owns its own hostname: `/api/*` to the control plane with the prefix stripped, everything else to the web app, on one loopback port for the tunnel to point at. Unset under the gateway's hostname, where the console does that job. |
| `AGENT_EDGE_BIND` / `AGENT_EDGE_PORT` | **NEW** | Where the edge listens; defaults `127.0.0.1` / `3003`. Only meaningful with the `edge` profile. |
| `AGENT_SESSION_COOKIE_SECURE` | **NEW** | Defaults to on. Set to `false` **only** for a developer running the whole stack on plain `http://localhost`; a session cookie without `Secure` is one anyone on the path can take. |
| `AGENT_TRUST_PROXY_HEADERS` | **NEW** | Defaults to off. Set to `true` only when a proxy this deployment controls overwrites `X-Forwarded-For`; otherwise the admin audit trail records whatever address the caller typed. |
| `AGENT_DEPLOYMENT_SHA` | **NEW** | Exact commit this API serves. Required by the production control-plane Compose stack and exported by `deploy/control-plane/deploy.sh`; do not hand-edit it during a rollout. New retained-group claims require the polling replica to advertise the matching completed host marker, so a pending or failed host deployment cannot claim through a cold-start API. The local stack generates its own stack-scoped marker. |

### Source control

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_GITHUB_APP_ID` | existing | The GitHub App backing repository access. Whether to reuse an existing App or register a new one is decision DR2, recorded in the operator's overlay. Either way its callback and setup URLs must list the agent domains before F5. |
| `AGENT_GITHUB_APP_CLIENT_ID` | existing | |
| `AGENT_GITHUB_APP_CLIENT_SECRET` | existing | Rotate when the App's audience changes — moving to a new domain is a good moment to retire whatever the old value was exposed to. |
| `AGENT_GITHUB_APP_PRIVATE_KEY_PATH` | existing | **Preferred.** Path to a read-only mounted key file. A private key in an environment variable is readable by anything that can list the process environment, and lands in `docker inspect`, crash dumps, and most log shippers. |
| `AGENT_GITHUB_APP_PRIVATE_KEY` | existing | Inline PEM. Supported by the code, and **deliberately not offered by `deploy/control-plane/docker-compose.yml`** — that stack mounts the key as a read-only file, so setting this in its env file does nothing. Use the `_PATH` form there; the inline form is for deployments that do not use this compose stack. |
| `AGENT_GITHUB_APP_INSTALL_URL` | existing | |
| `AGENT_GITHUB_API` | existing | Defaults to `https://api.github.com`; override for GHE. |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` | existing | |
| `AGENT_GITLAB_OAUTH_CLIENT_SECRET` | existing | |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | existing | Must move to the new agent domain at F5. |
| `AGENT_REPO_ALLOWLIST` | existing | |
| `AGENT_GIT_AUTHOR_NAME` / `AGENT_GIT_AUTHOR_EMAIL` | existing | Commit identity for published work. |
| `AGENT_GITHUB_TOKEN` | existing | Fallback git credential for deployments that publish without a GitHub App. The only component holding a git credential is this service — which is what keeps the sandbox credential-free. Unset means this deployment never publishes; it starts anyway. |
| `AGENT_PUBLISH_BASE_BRANCH` | existing | Branch a published PR targets, default `dev`. **Never pass this as an empty string.** Compose treats a blank value as *set*, and `os.getenv(name, "dev")` then returns `""` rather than the default — a PR opened against a branch named `''`. The compose file writes `:-dev` for this reason. |

### MCP

MCP belongs to this service: it owns the catalogue, each user's credential and
the attempt fence, so it is the only place that can decide a tool call. The
gateway serves models and answers nothing about MCP.

**Which servers exist is not configuration.** The catalogue is a reviewed,
in-code list (`cloud_agent/mcp_catalogue.py`) and a user connects their
own account to an entry in it; there is no variable that adds a server, and no
variable that grants one to somebody. Credentials live per user in
`agent_mcp_connections`, wrapped with
[`AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`](#own-secrets). The variables below
are all about *reaching* the relay, never about what it may reach.

| Variable | New? | Value / who provisions |
|---|---|---|
| ~~`AGENT_MCP_CONFIG`~~ | **removed** | There is no deployment-wide MCP registry. It named a file of remote servers with their credentials, and every user who could create a job could use them under that one identity — the team-MCP shape this product does not offer. Per-user connections replace it; the relay, the proxy and the sandbox boundary stay. Listed here rather than deleted because an existing deployment has this set, and a variable that silently stops meaning anything is worse than one recorded as gone. |
| `AGENT_SANDBOX_MCP_URL` | **NEW** | The MCP relay **as a sandbox reaches it** — a host-local relay alias, not this service's own bind address, which is why it is configured rather than derived. Handed to the runner at claim time and passed into the sandbox beside `AGENT_GATEWAY_URL`. Unset means jobs get no MCP, and a runtime asked for servers anyway refuses rather than resolving them against the inference gateway. |
| `AGENT_MCP_TUNNEL_*` | **NEW** | The remote-runner host's second relay: `SSH_DESTINATION`, `SSH_KEY`, `SSH_KNOWN_HOSTS`, and the optional `SSH_PORT` / `REMOTE_HOST` / `REMOTE_PORT` / `LISTEN_PORT`. Its own key, restricted the same way as the gateway tunnel's — one key that opens both would make the two-destination split cosmetic. |
| `AGENT_MCP_HOSTNAME` | **NEW** | The relay's alias on both container networks. Defaults to `agent-mcp`. |
| `AGENT_CONTROL_PLANE_TUNNEL_*` | **NEW** | The remote-runner host's **third** relay, and the only one the sandbox cannot reach: `SSH_DESTINATION`, `SSH_KEY`, `SSH_KNOWN_HOSTS`, plus the optional `SSH_PORT` / `REMOTE_HOST` / `REMOTE_PORT` / `LISTEN_PORT`. Its far end is the control plane's main listener (8000) — jobs, host registration, sessions, admin — which is why it is aliased on the runner's network only. Its own key again: a key that also opened the MCP tunnel would put 8000 one compromise away from the closed network. |
| `AGENT_CONTROL_PLANE_HOSTNAME` | **NEW** | That tunnel's alias, on the runner network only. Defaults to `agent-control-plane`. |
| `AGENT_MCP_TOKEN` | **NEW** | **Not operator-set — listed so nobody provisions it.** The name the generated sandbox runtime config *references* (`${AGENT_MCP_TOKEN}`) for the per-attempt relay credential the runner injects at launch. A value put here by hand would be a static tool credential shared by every job, which is the thing the per-attempt token exists to avoid. |

### Scheduling

| Variable | New? | Notes |
|---|---|---|
| `AGENT_HOST_CREDENTIAL` | **NEW** | What `/v1/agent/host/register` hands back. Authenticates this machine's heartbeats and claims and names it; there is no shared claim credential or request-body host fallback. Revoked by removing the host. The runner normally reads the persisted file instead of setting this variable. |
| `AGENT_LEASE_TTL` | existing | Attempt lease horizon (default 120 s). **Not** the source of the grant TTL, which this row used to claim: the grant TTL is derived from the renewal cadence — `grants.GRANT_TTL_S = RENEWAL_INTERVAL_S × RENEWAL_HEADROOM_PASSES` = 30 × 4 = 120 s — and the reaper's pass *is* `RENEWAL_INTERVAL_S`. The two are independent clocks that happen to agree; changing this one shortens the attempt lease and does not touch how long a grant lives. |
| `AGENT_TIMEOUT_S` | existing | Hard watchdog for one actively executing Agent CLI turn (default 3600 seconds). Hitting it fails/fences that turn; it does not mean the chat was idle and is not an alternate sandbox lifecycle timeout. |
| `AGENT_RUNNER_HOST` | existing | Execution environment name redeemed by `AGENT_ENROLL_TOKEN` on first registration and shared by its runner replicas. After enrollment, the credential is authoritative; changing this value does not rename the environment. Use `AGENT_PHYSICAL_HOST` to group environments for display without changing existing identities. |

## Host agent (`cloud_agent_host`)

### The stack's own identity

Read by `deploy/host/deploy.sh` and `deploy/host/sandbox_host.sh` rather than by
compose, because they name the stack rather than configure it.

| Variable | New? | Value / who provisions |
|---|---|---|
| `COMPOSE_PROJECT` | **NEW** | This stack's compose project — the prefix on every container, image and network it creates, and therefore what keeps two environments on one machine apart. **Required, no default.** Sharing it is silent: the second `up` does not refuse, it adopts the first stack's containers and recreates them with its own configuration. `freeinference-cloud-agent-sandbox-staging` / `-prod`. |
| `SIBLING_COMPOSE_PROJECTS` | **NEW** | Comma-separated projects on this machine that are the *other* environment rather than a stray copy of this one. Setting it makes `AGENT_HOST_STATE_ROOT` required: sharing that host path means sharing an enrolment credential. Chat workspaces are Docker writable layers and have no host directory. The deploy's guard reports containers of this stack's shape under any project it does not know; a legitimate sibling must be named or it trips that guard forever. |
| `SHARED_READY_TIMEOUT` | **NEW** | Deployment-script wait for the two parallel rollout halves, default 900 seconds. The host uses the same bound while waiting for cold-start inference/MCP/control endpoints; the control deploy uses it while waiting for a fresh exact-SHA heartbeat and reverse broker probes. This is a rollout watchdog, not a chat/session timeout. |

| Variable | New? | Notes |
|---|---|---|
| `AGENT_WORKSPACE_BROKER_URL` | existing | How a runner dials its broker — normally the Compose service URL. The runner advertises `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` (or this value only when the control plane truly shares that network) at enrollment. The control plane always resolves brokers through the attempt's enrolled host row and never treats this as a global fallback. |
| `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` | NEW | What the runner *reports* at enrolment when that differs from what it *dials*. It may be omitted in the integrated local stack, where the control plane and broker share a network. It is required by `deploy/host/docker-compose.sandbox.yml`, including when its project happens to be on the same physical machine as the control-plane project: separate Compose networks cannot resolve the broker service name. Set it to the far end of the reverse forward (`AGENT_BROKER_REVERSE_BIND`/`_PORT`) and never without that tunnel; otherwise every terminal, file listing and Git status answers 503. |
| `AGENT_WORKSPACE_BROKER_TOKEN` | existing | **Still global after phase G, and that means the same literal value on the control plane and on every host.** Host rows carry a broker *URL*, not a credential, so every broker in the pool authenticates this one value — a compromised host's broker token opens every other host's broker. Making it per-host is secret distribution (issue at enrollment, rotate, deliver), not routing, and is deliberately not done here. **Nothing detects a mismatch.** Two hosts provisioned independently get two secrets, both valid-looking, and the pool then answers 401 to every file listing, terminal and git status for jobs on the odd one out — which reads as a broken tunnel, not as a credential. This has already happened once (staging, 2026-08-06). Compare `sha256` of the value on both machines when provisioning a host, and never the value itself. |
| `AGENT_BROKER_URL_ALLOWED_HOSTS` | **NEW** | Comma-separated hostnames a registering machine may name as its broker. **Read by the control plane, not the host** — it is listed here because it is the other half of the token above: a host proposes its own broker address and is then sent the pool's shared credential, so an unfiltered address is that credential handed to anything a compromised or mistyped registration names. Names, not patterns; a pool's machines are few and known. Unset means any host, which is what this did before the check — opt-in so an upgrade does not break an existing pool. |
| `AGENT_CONTROL_PLANE_URL` | **NEW** | Where this runner claims work, registers this host and heartbeats. Split from `AGENT_GATEWAY_URL` because the two are the same origin only when control plane and gateway are co-located; a remote runner reaches each through its own tunnel alias. On the standalone stack it defaults to the control-plane tunnel (`AGENT_CONTROL_PLANE_HOSTNAME`), **not** the MCP one — those were briefly the same relay, and pointing here at MCP sends every claim to a listener that serves only MCP, so jobs are never taken and the 404 names a path rather than a port. |
| `AGENT_WORKER_ID` | existing | |
| `AGENT_REPO_CLONE_BASE_URL` | **NEW** | Operator-selected base used by the trusted runner for source clone URLs; defaults to `https://github.com`. The complete local stack points it at its private `git://local-repo` fixture so no external GitHub checkout is needed. This is deployment configuration, never a value supplied by a job. `http`, `https`, and `git` schemes are accepted for trusted local mirrors, but a repository clone token is refused unless the scheme is HTTPS so credentials cannot cross plaintext transport. |
| `AGENT_TERMINAL_IDLE_TTL_SECONDS` | existing | Reaps one stale interactive PTY after no input/output activity (default 1800 seconds). A retained PTY uses the earlier terminal/chat limit and hands its last activity time to the supervisor, so cleanup does not start a second inactivity window. This setting never stops the chat container directly. |
| `AGENT_TERMINAL_HARD_TTL_SECONDS` | existing | Absolute lifetime guard for one PTY (default 8 hours), even if it remains active. This bounds a resource and credential exposure window; it is not a chat lifecycle state. |
| `AGENT_TERMINAL_MAX_SESSIONS` | existing | |
| `AGENT_RETAINED_DOCKER_DATA_ROOT` | **NEW** | Exact daemon-side `DockerRootDir` expected by a restartable host (default `/var/lib/docker`). At broker startup, `docker info` must report this value or retained mode fails closed. For same-host disk durability, configure the **Docker daemon** itself to put `data-root` on the host's mounted durable block volume (for example `/mnt/agent-docker`) and set this variable to that same path. The broker checks configuration only; host bootstrap must separately prove the path is a mounted durable device (`findmnt --target …`). The path is never mounted into the broker or a sandbox, and it is not a per-chat directory API. Docker remains the only reader/writer of container layers. This preserves stop/start across service and host reboots, but is not the deferred cross-host checkpoint/cold-storage protocol. |
| `AGENT_SHARED_RESIDENT_CAPACITY` | **NEW** | Number of credential-isolated member UID/private-tmpfs slots built into each new shared container. Default 8; accepted range 2–128. Fork reserves a free slot before any native mutation. This bounds resident chats in one group, while `AGENT_HOST_SLOTS` separately bounds simultaneously claimed turns on the machine. |
| `AGENT_CHAT_IDLE_TIMEOUT_S` | **NEW** | The single product-level inactivity clock for a retained chat group. Defaults to 1800 seconds (30 minutes). Turn/file/Git completion and terminal activity advance the same group deadline; activity tokens prevent a mid-operation stop without stacking another full timeout. When the settled clock expires, the host issues `docker stop`, releases that group's phase bridge, and retains the same container/writable layer. It never authorizes `docker rm`. Wake recreates the correct phase bridge before `docker start`. `AGENT_TIMEOUT_S` and terminal TTLs are resource watchdogs, not alternate chat lifecycle clocks. |
| `AGENT_ENROLL_TOKEN` | **NEW** | One-time host enrollment token (task G2). Minted at `POST /admin/agent/runner-hosts/enroll-token`, redeemed once at `POST /v1/agent/host/register`, then replaced by the returned host credential. 15-minute TTL, hashed at rest, and shown exactly once — losing it means issuing another, not looking it up. **The token names the host it may register**: the admin minting it supplies `host`, and registration under any other name is refused without spending the token, so a typo cannot revoke a live machine's credential. The runner-side enrollment client is implemented: it serializes first registration across replicas, writes the host credential mode 0600 at `AGENT_HOST_CREDENTIAL_FILE`, and uses authenticated heartbeats thereafter. |
| `AGENT_HOST_CREDENTIAL_FILE` | **NEW** | Where the host credential is kept between restarts, written mode 0600 before any byte reaches it — this machine runs untrusted repository code next door. The code default is `/var/lib/hybridinference/agent-host-credential`, but the supported host Compose overrides it to `${AGENT_HOST_STATE_ROOT:-/var/lib/hybridinference/agent-host}/credential`, so **on a deployed host the file lives under `AGENT_HOST_STATE_ROOT`** and that is the path the rebuild runbook and any re-enrolment step must name. `AGENT_HOST_CREDENTIAL` in the environment wins over the file, for deployments that inject it another way. |
| `AGENT_DEPLOYMENT_SHA` / `AGENT_SHARED_LIVE_READY_FILE` | **NEW** | Deployment-script-managed rolling fence, not operator-selected topology. The control plane requires the same exact SHA described above. `deploy/host/deploy.sh` supplies it and `sandbox_host.sh` keeps the marker under `AGENT_HOST_STATE_ROOT`. A valid runner keeps advertising the base shared-sandbox/native-fork protocol while the marker is pending so host readiness can inspect it, but it does not poll for work until the exact `shared-sandbox-deployment:<sha>` marker is complete. This runner-side pause also protects the previous API, which does not know the new exact-SHA claim field during a parallel rollout. Do not set either to choose behavior: retained/shared-live is unconditional. |
| `AGENT_HOST_SLOTS` | **NEW** | Concurrent attempts this machine accepts, reported at registration and on every heartbeat. Defaults to 3, must be at least 2, and cannot exceed `AGENT_RUNNER_REPLICAS`, because one runner process executes one claimed turn synchronously. |
| `AGENT_RUNNER_REPLICAS` | existing | Number of synchronous runner executors started by the host Compose stack (default 3). Shared-live requires at least two, passes this value into each runner's claim attestation, and verifies that the configured number of containers are actually running before deployment succeeds. |
| `AGENT_RUNNER_VERSION` | **NEW** | Free-text version for the pool listing. Operator-facing only — never a compatibility gate, and a missing value must not stop a host enrolling. |
| `AGENT_HOST_CAPACITY_ENABLED` | **NEW** | Whether a runner samples its Docker host's CPU, memory and retained-container count for the admin Servers page. Defaults to enabled; `0`/`false`/`no`/`off` turns it off, and the server is then shown as not measured rather than as idle. The host Compose stack bind-mounts the host's `/proc/stat`, `/proc/meminfo` and `/proc/sys/kernel/random/boot_id` read-only under `/run/host-metrics` for it — three named files, never all of `/proc` and never `pid: host`, because a container's own `/proc/stat` describes the container and a shared pid namespace would show untrusted sandbox neighbours every process on the machine. Sampling is best effort in both directions: a missing file yields a missing measurement, never a fallback read of an unscoped `/proc`, and an unscoped or malformed report is dropped by the control plane without refusing the heartbeat that carried it. |
| `AGENT_LOG_LEVEL` | **NEW** | Host process log level (default INFO). Read by `configure_process_logging()` at the runner entrypoint; replaces the source repo's import-side-effect logging setup (task D6). |

## Sandbox isolation (host agent reads, sandbox inherits)

| Variable | New? | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | existing | Defaults to `kata`. `container` is the explicit trusted-repository downgrade. `process` is a non-claimable test seam and cannot serve retained chats. |
| `AGENT_SANDBOX_RUNTIME` | existing | Unset or `runc` for trusted internal repos. The enforced public/untrusted profile currently requires exactly `io.containerd.kata.v2`; gVisor needs its own future execution profile and is not accepted as Kata evidence. |
| `AGENT_SANDBOX_IMAGE` | existing | Tag it per environment (`freeinference-cloud-agent-job:staging` / `:prod`) on a machine that runs both, or a rebuild on one side replaces the image the other is running. `-job` and not `-sandbox`, so it cannot be read as `…-sandbox-<env>-runner`, the image the runner container itself is built from. |
| `AGENT_SANDBOX_NAMESPACE` | **NEW** | Stable deployment namespace stamped onto retained containers and private bridges so sibling Compose stacks on one Docker daemon cannot discover, stop, or prune each other's chats. The host stack requires its unique Compose project; the complete local stack derives it from `LOCAL_PROJECT_NAME`. Direct embeddings default to `default` and must override it when sharing a daemon. |
| `AGENT_SANDBOX_NETWORK` | existing | Legacy alias for the `platform_only` discovery backplane, honoured so an existing deployment keeps working; the current setting is `AGENT_EGRESS_NETWORK_PLATFORM_ONLY`. It has **no default on purpose** — an unset value used to mean the Docker bridge, i.e. unrestricted egress for untrusted repository code — so a tier that resolves to no network is a preflight failure rather than a fall back to a more open one. |
| `AGENT_SANDBOX_DOCKER` | existing | |
| `AGENT_SANDBOX_HOME` | existing | |
| `AGENT_SANDBOX_UID` / `AGENT_SANDBOX_GID` | existing | Non-root inside the sandbox. |
| `AGENT_SANDBOX_EXTRA_ARGS` | existing | |
| `AGENT_MEMBER_PROCESS_REAPER_SOURCE` | **NEW (internal)** | Trusted runner-side path to the root-owned `member-process-reaper` helper copied into a retained container created by an older compatible image. The runner image supplies `/usr/local/libexec/member-process-reaper`; operators normally never set this. It is infrastructure compatibility material, not chat data or a host workspace mount. |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK` | existing | ⚠️ Isolation escape hatch. Must be unset in any environment reachable by untrusted repositories. |
| `AGENT_SANDBOX_ALLOW_UNISOLATED` | existing | ⚠️ Isolation escape hatch, and narrower than the row above: it only opens the `process` backend, which is a non-claimable test seam and can never serve a retained chat. Must be unset in any environment reachable by untrusted repositories. |
| `AGENT_EGRESS_SETUP_TIER` | existing | Egress during dependency setup. The standalone host defaults to `trusted`, backed by its checked-in Squid allowlist proxy. Set `platform_only` only when setup needs no registry access. |
| `AGENT_EGRESS_AGENT_TIER` | existing | Egress during the agent run. `platform_only` is the required safe production default. `trusted` adds the package allowlist proxy, `custom` delegates reachability to an operator network, and `full` is unrestricted; all three widen the reviewed agent-phase boundary. |
| `AGENT_EGRESS_ALLOWLIST` | existing | Extra domains added to the built-in Trusted package-registry list. Malformed entries and agent-vendor telemetry domains fail closed before Squid starts. |
| `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` | existing | Internal discovery backplane carrying the fixed inference/MCP relays. Each chat gets a derived private bridge populated from it; chats are not left together on this network. On a host running sibling staging/production stacks this must be each stack's explicit unique name. |
| `AGENT_EGRESS_NETWORK_TRUSTED` | existing | Internal discovery backplane carrying the setup proxy. Each setup phase gets a derived chat-private bridge. The supported host script derives a Compose-project-specific name when unset. |
| `AGENT_EGRESS_NETWORK_UPLINK` | **NEW** | Routable network joined only by the allowlist proxy. The supported host script derives a Compose-project-specific name when unset. It must differ from both discovery backplanes. |
| `AGENT_EGRESS_CHAT_NETWORK_POOL` | **NEW** | Canonical RFC1918 IPv4 CIDR from which Docker atomically allocates one `/29` per active chat generation/phase bridge (default `10.240.0.0/16`, 8192 bridges). Choose a range that does not overlap Docker, VPN, or private-service routes on the host. Collision retries are daemon-arbitrated across broker/runner processes. Setup→agent and idle/lost stop remove bridges immediately; broker reconciliation removes stale interrupted-create/transition bridges after a five-minute grace period so it cannot delete one between creation and sandbox attachment. Stopped retained layers therefore do not consume IPAM forever. |
| `AGENT_EGRESS_NETWORK_CUSTOM` | existing | |
| `AGENT_EGRESS_NETWORK_FULL` | existing | |
| `AGENT_EGRESS_PROXY_URL_TRUSTED` | existing | The proxy a Trusted-tier sandbox is pointed at — the one enforcing the built-in registry list plus `AGENT_EGRESS_ALLOWLIST`. The standalone Compose supplies this from its private service name and `AGENT_EGRESS_PROXY_PORT`; operators do not need to set it there. |
| `AGENT_EGRESS_PROXY_URL_CUSTOM` | existing | Same for the Custom tier, which supplies its own proxy by definition; nothing here renders a config for it. |
| `AGENT_EGRESS_PROXY_PORT` | existing | Port the rendered Squid config listens on. A non-numeric value is refused at render rather than defaulted, since a proxy silently on the wrong port is a sandbox with no egress filtering at all. |
| `AGENT_LIVE_SESSION_ID` / `AGENT_LIVE_WORKDIR` | **NEW (internal)** | Generated by the runtime adapter for the private OpenCode/Kilo stdio bridge. Operators must not set these. They select the already-authorized native session and fixed `/workspace` path inside one member process; neither is a credential or host path. |

### Complete local stack database recovery

`deploy/local/dev.sh` stores the local Postgres password and matching
`AGENT_DATABASE_URL` in the gitignored `deploy/local/.env.local`. Back up that
file with any retained local Docker volume. If the file is missing while the
selected Compose project still owns `agent-postgres`, the script refuses to
generate a replacement password: PostgreSQL applies `POSTGRES_PASSWORD` only
when initializing an empty data directory, so a new value cannot unlock the
old volume.

`up` builds the required images first. It and `start` then bring up only
Postgres, wait for readiness, authenticate from the control-plane image using
the configured DSN, and run Alembic before starting the rest of the stack. On a
mismatch, restore the original
`AGENT_POSTGRES_PASSWORD` and `AGENT_DATABASE_URL`. The script never deletes the
volume or resets its password automatically.

### Agent CLI version pins (image build)

The five default versions live in both
`deploy/sandbox/Dockerfile.agent-sandbox` and
`cloud_agent_host/shared_protocol.py`. They are one reviewed protocol unit:
the image installs and labels them, while every runner and the control plane
use the matching constants to decide whether `shared-sandbox-v1` may be
advertised or claimed. They are therefore source pins, not per-host `.env`
settings. Change both locations together and re-run the agent-loop conformance
suite when intentionally changing a default.

The Dockerfile still verifies every exact version against the installed binary
at build time. A dist-tag (`latest`) or range (`^2.1`) fails the build, so its
labels cannot claim a different binary than the image actually contains.

Each runtime takes a `*_EXTRA_VERSIONS` deployment setting — comma-separated,
blank by default — naming **additional** versions the same image carries beside
the reviewed default. That list is the *menu*: build time decides what the image
holds, and an admin picks which item new work runs from the runner-hosts page
(`agent_runner_policy.runtime_versions`). Blank leaves an image holding exactly
its defaults, which is where every deployment starts.

The same exactness rule applies, and so does the parser one, harder: putting a
version on the menu is a claim that `runtimes.py` can read its event stream.

`tests/external/test_runtime_stream_conformance.py` is the gate for that claim,
and the way to point it at a candidate is to **put the version on the
Dockerfile's `ARG` line**:

```bash
# deploy/sandbox/Dockerfile.agent-sandbox, beside the reviewed default
ARG CLAUDE_CODE_VERSION=2.1.220
ARG CLAUDE_CODE_EXTRA_VERSIONS=2.2.0

# then, from the checkout — no image build and no deployment needed
uv run pytest -m external tests/external/test_runtime_stream_conformance.py
```

The suite derives its parametrization from those `ARG` lines
(`test_runtime_stream_conformance.py::_menu`, `_DOCKERFILE`), not from an
environment variable, not from a built image and not from the deployed `.env`:
setting `CLAUDE_CODE_EXTRA_VERSIONS=2.2.0` in the shell changes nothing about
what runs. It then fetches each version with `npx` at exactly that version and
drives it against a fake gateway, so the measurement is of the real binary — but
it is a measurement of the **repository's** Dockerfile, so the candidate must be
on that `ARG` line first.

Two limits worth knowing before trusting a green run:

- **`claude-code` only.** This is the one runtime with a stream-conformance
  suite; the other four have contract fixtures and their adapters' unit tests,
  and no equivalent gate. Adding a version for them is currently a review
  obligation rather than a test.
- **It proves the parser, not the option.** A pass means the version's event
  shape is intelligible to our normalizer. It does not exercise the deployed
  image, the sandbox, or the dropdown.

A version nobody checked, offered in a dropdown, is one click from a job that
dies at its first turn.

| Variable | New? | Notes |
|---|---|---|
| `CLAUDE_CODE_EXTRA_VERSIONS` | **NEW** | Extra `@anthropic-ai/claude-code` versions. ~300 MB each. |
| `CODEX_EXTRA_VERSIONS` | **NEW** | Extra `@openai/codex` versions. |
| `PI_EXTRA_VERSIONS` | **NEW** | Extra `@mariozechner/pi-coding-agent` versions. |
| `OPENCODE_EXTRA_VERSIONS` | **NEW** | Extra `opencode-ai` versions. |
| `KILO_EXTRA_VERSIONS` | **NEW** | Extra `@kilocode/cli` versions. |

## Web (`web/`)

Two classes of configuration, and the difference is what changing one costs.
Both are **public** — one is compiled into a bundle a browser downloads, the
other is read by a container a browser reaches — so nothing secret may appear
in either.

### Build-time: the image variant

A `NEXT_PUBLIC_*` variable is substituted into the bundle during `next build`.
Each one must therefore be an `ARG` in `deploy/control-plane/Dockerfile.web`
*and* a key under the `web` service's `build.args`: a row here without both is a
variable an operator can set everywhere and change nothing. Every row names its
compose argument, and `test_web_build_arguments_are_wired` fails on a row that
has none. Changing one is a rebuild.

The two are not independent. Every deployment that serves the app and the API
from one origin has `AGENT_WEB_API_URL` equal to `<AGENT_WEB_BASE_PATH>/api`,
and `next build` refuses a *relative* value that disagrees with the mount point
rather than shipping an image whose every request misses the reverse proxy —
a 404 on a page that looks correct. An absolute value is the cross-origin shape
(a deployment's own CORS decision) and is left alone, as is an empty one, which
is the same-origin shape for a deployment that owns its hostname; compose is
what requires the value under a mount prefix. `web/src/config/variantPairing.test.ts`
runs the real config file, so the rule is pinned where it is written.

| Variable | New? | Compose build argument | Notes |
|---|---|---|---|
| `NEXT_PUBLIC_BASE_PATH` | | `AGENT_WEB_BASE_PATH` | Prefix the app is mounted under. `/agents` serves it inside the gateway's own hostname — same origin, so the session cookie needs no CORS and no DNS record is involved, and a rollback is a reverse-proxy edit rather than a DNS change waiting out a TTL. Empty for a deployment that owns its hostname. The redirect URI the gateway allowlists is no longer derived from this: sign-in returns to the control plane's `AGENT_SESSION_REDIRECT_URI`, not to a page in this app. This value still has to match `AGENT_WEB_BASE_URL`, which is where the control plane sends the browser afterwards. |
| `NEXT_PUBLIC_AGENT_API_URL` | | `AGENT_WEB_API_URL` | Control plane base URL. Empty means same-origin, which is the DR4 shape (one host, API under `/api`). Under a base path, the same-origin value is a relative prefix — `/agents/api` — which the reverse proxy strips before forwarding. A cross-origin value additionally needs CORS-with-credentials on the control plane, because the session is a cookie. |
| `NEXT_PUBLIC_BUILD_SHA` | | `BUILD_SHA` | Optional build identity, baked in by the deploy script. |
| `NEXT_PUBLIC_BUILD_TIMESTAMP` | | `BUILD_TIMESTAMP` | Optional build identity. |

### Runtime: the deployment's identity

Read by the web server on every request (`web/src/config/site.server.ts`), so
the same built image serves any deployment and changing one is a restart, not a
rebuild. Each must be a key under the `web` service's `environment:`;
`test_web_runtime_variables_are_wired` fails on one that is not.

| Variable | New? | Notes |
|---|---|---|
| `AGENT_WEB_APP_NAME` | **MOVED** | Product name in the header and the document title. Empty shows `Cloud Agent`. |
| `AGENT_WEB_ORG_NAME` | **MOVED** | Optional organisation name beside the product name. Empty hides it. |
| `AGENT_WEB_ORG_URL` | **MOVED** | Link target for `AGENT_WEB_ORG_NAME`. |
| `AGENT_WEB_STATUS_URL` | **MOVED** | Status page, opened in a new tab from the header. Empty hides the link — an empty `href` resolves to the current page, so a Status link that is rendered anyway reopens the page the user is standing on. |
| `AGENT_WEB_GATEWAY_URL` | **MOVED** | `${GATEWAY_BASE_URL}`. The gateway's origin, for the links this app does not host itself — nothing to do with sign-in. It replaced `NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL`, which the browser needed only while it built the authorize URL and redeemed the code itself; both are the control plane's job now (`GATEWAY_AUTHORIZE_URL`). A path in this value is ignored; empty hides the links. |
| `AGENT_WEB_FEATURE_RAG` | **MOVED** | `true` shows the gateway's Docs Assistant link. `false` or empty hides it; any other value is refused. The page belongs to the gateway, not to this service. |

These moved out of the build: an operator renaming a deployment should not have
to rebuild the same image. An illegal value — a non-URL, or a boolean that is
not `true`/`false` — is refused at render time with a message naming the
variable, rather than read as "unset"; the previous reader turned a typo into a
missing link and said nothing.

The source repo's frontend fetched `/site-config` from the gateway at runtime so
one bundle could serve several deployments. That endpoint is the gateway's and
is not part of either contract this service consumes, so the deployment's
identity is configuration instead — runtime for the identity, build-time only
for where the app is mounted.

## Set on the HybridInference side — not here

These are provisioned in the gateway's environment. Listed so the contract has
one place to be read from end to end.

| Variable | Task | Notes |
|---|---|---|
| `IDENTITY_JWT_PRIVATE_KEY` | C1 | RS256 private key, PEM. Public half is served at `/v1/identity/jwks`. |
| `IDENTITY_JWT_RETIRING_PUBLIC_KEYS` | C1 | Optional. Public PEMs of previously-active signing keys, concatenated and published alongside the current key so tokens issued before a rotation still verify. Keep a key here for at least the maximum token lifetime plus the JWKS cache window, then drop it. Public halves only — a retiring key must not be able to sign. |
| `IDENTITY_ISSUER` | C3 | The `iss` claim in identity tokens. Falls back to the gateway's `base_url`; unset means issuance is switched off rather than tokens minted with an empty `iss`. Must equal the value this service verifies against. |
| `IDENTITY_ALLOWED_REDIRECTS` | C2 | Comma-separated exact-match redirect URIs. Matching is equality — no prefix, no suffix — so the agent web callback must be listed verbatim before F5. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | C5 | Same value as the control plane's copy above. |

## Infrastructure (not environment variables)

Recorded here as *what has to be decided*, not as anyone's actual answer.

- **Domains** (DR4): one host for the web app and the control-plane API, the API
  under `/api` — e.g. `agents.example.org` and a staging sibling. The exact names
  go in the overlay, and must match `IDENTITY_ALLOWED_REDIRECTS` on the gateway
  exactly.
- **Reaching the gateway**: the control plane needs a route to `GATEWAY_BASE_URL`.
  The sandbox needs one to the relay and, during the setup phase only, one to
  the allowlist proxy — nothing else, in either phase. If a deployment fronts
  either through a tunnel, the port allocation is overlay detail.
- **Sandbox image registry**: where `AGENT_SANDBOX_IMAGE` is pulled from. Decide
  before D7.

## Adding a variable

1. Add a row here, in the right section, marked **NEW**.
2. Note who provisions it and whether it differs per environment.
3. If it is a secret, say where it is stored — never the value itself.
4. The PR checklist enforces this; a new variable without a row is a review stop.

### Physical servers and execution environments

`AGENT_RUNNER_HOST` identifies one enrolled execution environment. When two
stacks on the same physical server talk to one control plane, they need distinct
names, credentials, state roots and broker URLs. Do not rename or re-enroll an
existing environment to combine its retained chats with another one.

`AGENT_PHYSICAL_HOST` names the physical server for the admin inventory. Give
all environments on that server the same name, unique within the control plane.
`deploy/host/sandbox_host.sh` defaults it to the host's short hostname; direct
Compose deployments must set it explicitly. Runners send it at registration and
on each heartbeat, stored as `labels.physical_host`. Older runners can omit it
without erasing existing metadata. No database migration is required.

The Servers table shows the executor matching the new-chat execution policy
on each physical server. Production Compose defaults to `untrusted-kata-v1`;
the local Compose overlay explicitly keeps `trusted-v1`. Legacy executors remain
in the API for retained-session ownership but are not offered for new chats.
Keep one placement executor per physical server; extra old deployments must be
retired after their retained conversations are no longer needed.

With one available server there is no placement selector. With multiple servers,
the persistent New chats selector shows Automatic or the selected server; its
value does not disappear after changing it. An unavailable saved selection is
always shown with a recovery action, even if only one server remains available.
Disabled servers are listed in a collapsed Unavailable servers section, not
counted as choices. Slots are the displayed executor's concurrency limit, not
GPU counts or a sum of old and current deployments. Grouping never changes
credentials, broker routing, or retained sandbox ownership. Server names are
reported metadata; runtime suffixes are never guessed.

For an existing deployment, update the control plane and web first, then set
`AGENT_PHYSICAL_HOST` and roll each runner environment when its active turns
have settled. Existing host credentials and retained sandboxes stay in place.
