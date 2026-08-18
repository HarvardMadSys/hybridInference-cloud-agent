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
| Staging values provisioned | ☐ not yet |
| Production values provisioned | ☐ not yet |
| Secret storage | deploy host environment / unit env files, and read-only file mounts for keys. **Never** in git. |

## Control plane (`backend/cloud_agent`)

### Database

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_DATABASE_URL` | **NEW** | Postgres DSN for the agent-owned database. Must be a database this service owns outright — sharing one with the gateway would reintroduce the coupling the split exists to remove. |
| `AGENT_TEST_DATABASE_URL` | **NEW** | Used by `make test-db` only. Local/CI throwaway database. |

### Gateway contracts

| Variable | New? | Value / who provisions |
|---|---|---|
| `GATEWAY_BASE_URL` | **NEW** | Origin of the FreeInference gateway this deployment federates with, e.g. `https://gateway.example.org`. |
| `GATEWAY_JWKS_URL` | **NEW** | `${GATEWAY_BASE_URL}/v1/identity/jwks` (task C1). Publishes the current signing key plus any retiring ones, so cache by `kid` and re-fetch on a miss rather than pinning a single key. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | **NEW** | Bearer token authorizing this service to mint and revoke inference grants (task C5). Random 32 bytes, the same value on both sides, and distinct per environment — one shared across staging and production makes a staging compromise a production one. |
| `GATEWAY_ISSUER` | **NEW** | Optional. The `iss` an identity token must carry, checked only when set. Left optional on purpose: a deployment may front the gateway at a different name than it mints under, and pinning it wrongly locks every user out. |
| `AGENT_GATEWAY_URL` | existing | Gateway URL as seen *from inside the sandbox* — the relay address, not the public URL. Differs from `GATEWAY_BASE_URL` by design. |

### Own secrets

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | **NEW** | Wraps stored GitLab/GitHub credentials, **and each user's MCP connection credentials** — one key to deploy and rotate, with the two kinds derived under different domain separators so a ciphertext from one table cannot be read or planted as the other's. Unset leaves both unconfigured rather than storing anything in the clear: MCP then offers an empty picker instead of failing the page. Replaces the gateway-derived key those secrets were encrypted with before the split, which is why migrating them needs a re-wrap step and not a copy (task H1). Generate random 32 bytes, unique per environment. |
| `AGENT_CONTROL_TOKEN_SECRET` | **NEW** | Signs host/worker control tokens. Replaces the old derivation from the gateway's `API_KEY_SECRET` (task E3). Generate random 32 bytes, unique per environment. |
| `AGENT_SESSION_SECRET` | **NEW** | Signs the web session cookie (task E8). Generate random 32 bytes, unique per environment. |
| `AGENT_DISPATCHER_TOKEN` | existing | Authenticates host agents claiming work. Existing deployments may hold this outside this service's own configuration; confirm where it is actually read from before cutover rather than assuming. |
| `AGENT_RESIDENCY_TTL_S` | **NEW** | How long a hot sandbox's residency lease lives between host heartbeats (session-resident threads, design §7). Granted at establish and on every beat; the reaper lapses what outlives it, demoting the thread to a COLD start off its checkpoint. Default 1800. Also the cap on how long an idle resident process is kept at all, and the TTL of the residency's thread-attributed grant (design §14) — the idle process holds a live credential, renewed by the same beats and revoked with the residency, so this number bounds its lifetime too. |
| `ADMIN_TOKEN` | existing | The machine-to-machine admin credential (task E7). **Unset opens nothing** — `verify_admin_access` refuses an empty configured value rather than matching an empty presented one — so a deployment that skips it has no token door, only the admin-session one. |

### Behaviour switches read by the control plane

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_SESSION_COOKIE_SECURE` | **NEW** | Defaults to on. Set to `false` **only** for a developer running the whole stack on plain `http://localhost`; a session cookie without `Secure` is one anyone on the path can take. |
| `AGENT_TRUST_PROXY_HEADERS` | **NEW** | Defaults to off. Set to `true` only when a proxy this deployment controls overwrites `X-Forwarded-For`; otherwise the admin audit trail records whatever address the caller typed. |

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
in-code list (`backend/cloud_agent/mcp_catalogue.py`) and a user connects their
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
| `AGENT_HOST_CREDENTIAL` | **NEW** | What `/v1/agent/host/register` hands back (task G2). Authenticates this machine's heartbeats and its claims, and names it — unlike the shared `AGENT_DISPATCHER_TOKEN`, which says "a runner" and not "which". Revoked by removing the host. Read by the same not-yet-written host client as `AGENT_ENROLL_TOKEN`. |
| `AGENT_LEASE_TTL` | existing | Attempt lease horizon. Grant TTL is derived from this (task E9) — changing one without the other creates either dead grants or grants outliving their attempt. |
| `AGENT_TIMEOUT_S` | existing | |
| `AGENT_RUNNER_HOST` | existing | The name this runner reports at claim time. **Only until the host enrols.** After enrolment the control plane reads the host from the runner's credential, not from the claim body, so editing this renames nothing — what keeps two stacks on one machine apart is a separate `AGENT_HOST_STATE_ROOT` each, since the credential lives there. Phase G extends the `agent_runner_hosts` row it creates with status, capacity and capabilities rather than replacing it with a second table, so a runner that only sets this keeps working exactly as before — unmetered, online, and claimable. |

### Cutover migration (`deploy/migrate/`, read once and never again)

Set only for the length of one run of `migrate_connections.py` (task H1), and
by a human at a shell. **Nothing in the deployed service reads these**, and
neither belongs in any unit file, `.env`, or image.

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_MIGRATION_SOURCE_URL` | **NEW** | DSN of the *gateway's* database, read-only for the length of the import. Env rather than a flag because a DSN carries a password and `argv` is readable by every process on the host. |
| `AGENT_MIGRATION_OLD_KEY_FILE` | **NEW** | **Path**, not a value: the file holding the gateway's old key material, which the script reads once to decrypt credentials before re-encrypting them under `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`. Deleted immediately afterwards. The script deliberately refuses to read `API_KEY_SECRET` from its own environment — see [the one rule](#the-one-rule); this is the narrowest shape that still lets a re-key happen at all, and MIGRATION.md's open-decisions register says which machine may run it. |

## Host agent (`host/cloud_agent_host`)

### The stack's own identity

Read by `deploy/host/deploy.sh` and `deploy/host/sandbox_host.sh` rather than by
compose, because they name the stack rather than configure it.

| Variable | New? | Value / who provisions |
|---|---|---|
| `COMPOSE_PROJECT` | **NEW** | This stack's compose project — the prefix on every container, image and network it creates, and therefore what keeps two environments on one machine apart. **Required, no default.** Sharing it is silent: the second `up` does not refuse, it adopts the first stack's containers and recreates them with its own configuration. `freeinference-cloud-agent-sandbox-staging` / `-prod`. |
| `SIBLING_COMPOSE_PROJECTS` | **NEW** | Comma-separated projects on this machine that are the *other* environment rather than a stray copy of this one. Setting it also makes `AGENT_HOST_STATE_ROOT`, `AGENT_WORKDIR_ROOT` and `AGENT_SNAPSHOT_ROOT` required: they are host paths that default to the same directories for every stack, and sharing them means shared enrolment credentials and shared job workspaces. The deploy's guard reports containers of this stack's shape under any project it does not know — which is what caught six runners and two brokers here once — and a legitimate sibling has to be named or it trips that guard forever. |

| Variable | New? | Notes |
|---|---|---|
| `AGENT_WORKSPACE_BROKER_URL` | existing | Single-host global. Phase G resolves this per host from the database; the env var stays as the fallback when an attempt has no `host_id`. On a runner host it is also **how that runner dials its own broker** — a compose service name, resolvable there and nowhere else. |
| `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` | NEW | What the runner *reports* at enrolment, when that differs from what it *dials*. Unset, the two are one address, which is correct for every co-located deployment. Required when the control plane is on another machine: the service name above means nothing there, so the stored `broker_url` is unreachable and every terminal, file listing and git status answers 503 — jobs die at the first one, every attempt. Set it to the far end of the reverse forward (`AGENT_BROKER_REVERSE_BIND`/`_PORT`) and never without it. |
| `AGENT_WORKSPACE_BROKER_TOKEN` | existing | **Still global after phase G.** Host rows carry a broker *URL*, not a credential, so every broker in the pool authenticates this one value — a compromised host's broker token opens every other host's broker. Making it per-host is secret distribution (issue at enrollment, rotate, deliver), not routing, and is deliberately not done here. |
| `AGENT_BROKER_URL_ALLOWED_HOSTS` | **NEW** | Comma-separated hostnames a registering machine may name as its broker. **Read by the control plane, not the host** — it is listed here because it is the other half of the token above: a host proposes its own broker address and is then sent the pool's shared credential, so an unfiltered address is that credential handed to anything a compromised or mistyped registration names. Names, not patterns; a pool's machines are few and known. Unset means any host, which is what this did before the check — opt-in so an upgrade does not break an existing pool. |
| `AGENT_CONTROL_PLANE_URL` | **NEW** | Where this runner claims work, registers this host and heartbeats. Split from `AGENT_GATEWAY_URL` because the two are the same origin only when control plane and gateway are co-located; a remote runner reaches each through its own tunnel alias. On the standalone stack it defaults to the control-plane tunnel (`AGENT_CONTROL_PLANE_HOSTNAME`), **not** the MCP one — those were briefly the same relay, and pointing here at MCP sends every claim to a listener that serves only MCP, so jobs are never taken and the 404 names a path rather than a port. |
| `AGENT_WORKER_ID` | existing | |
| `AGENT_WORKDIR_ROOT` | existing | Workspace root. Reused as-is at cutover (H2) so in-flight checkouts survive. |
| `AGENT_SNAPSHOT_ROOT` | existing | |
| `AGENT_SNAPSHOT_TTL_S` / `AGENT_WORKSPACE_TTL_S` | existing | |
| `AGENT_TERMINAL_IDLE_TTL_SECONDS` | existing | |
| `AGENT_TERMINAL_HARD_TTL_SECONDS` | existing | |
| `AGENT_TERMINAL_MAX_SESSIONS` | existing | |
| `AGENT_GENERIC_COMMAND` | existing | |
| `AGENT_ENROLL_TOKEN` | **NEW** | One-time host enrollment token (task G2). Minted at `POST /admin/agent/runner-hosts/enroll-token`, redeemed once at `POST /v1/agent/host/register`, then replaced by the returned host credential. 15-minute TTL, hashed at rest, and shown exactly once — losing it means issuing another, not looking it up. **The token names the host it may register**: the admin minting it supplies `host`, and registration under any other name is refused without spending the token, so a typo cannot revoke a live machine's credential. **The host-side client that reads this does not exist yet**; see MIGRATION.md's deferred register. |
| `AGENT_HOST_CREDENTIAL_FILE` | **NEW** | Where the host credential is kept between restarts. Default `/var/lib/hybridinference/agent-host-credential`, written mode 0600 before any byte reaches it — this machine runs untrusted repository code next door. `AGENT_HOST_CREDENTIAL` in the environment wins over the file, for deployments that inject it another way. |
| `AGENT_HOST_SLOTS` | **NEW** | Concurrent attempts this machine accepts, reported at registration and on every heartbeat. Unset means unmetered, which is the pre-pool behaviour. |
| `AGENT_RUNNER_VERSION` | **NEW** | Free-text version for the pool listing. Operator-facing only — never a compatibility gate, and a missing value must not stop a host enrolling. |
| `AGENT_LOG_LEVEL` | **NEW** | Host process log level (default INFO). Read by `configure_process_logging()` at the runner entrypoint; replaces the source repo's import-side-effect logging setup (task D6). |

## Sandbox isolation (host agent reads, sandbox inherits)

| Variable | New? | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | existing | |
| `AGENT_SANDBOX_RUNTIME` | existing | `runc` for trusted internal repos; Kata/gVisor required for public multi-tenant use. |
| `AGENT_SANDBOX_IMAGE` | existing | Tag it per environment (`freeinference-cloud-agent-job:staging` / `:prod`) on a machine that runs both, or a rebuild on one side replaces the image the other is running. `-job` and not `-sandbox`, so it cannot be read as `…-sandbox-<env>-runner`, the image the runner container itself is built from. |
| `AGENT_SANDBOX_NETWORK` | existing | |
| `AGENT_SANDBOX_DOCKER` | existing | |
| `AGENT_SANDBOX_HOME` | existing | |
| `AGENT_SANDBOX_UID` / `AGENT_SANDBOX_GID` | existing | Non-root inside the sandbox. |
| `AGENT_SANDBOX_EXTRA_ARGS` | existing | |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK` | existing | ⚠️ Isolation escape hatch. Must be unset in any environment reachable by untrusted repositories. |
| `AGENT_SANDBOX_ALLOW_UNISOLATED` | existing | ⚠️ Same. |
| `AGENT_EGRESS_SETUP_TIER` | existing | Egress during dependency setup. |
| `AGENT_EGRESS_AGENT_TIER` | existing | Egress during the agent run. `platform_only` is the safe default; `custom` grants untrusted code general internet access. |
| `AGENT_EGRESS_ALLOWLIST` | existing | |
| `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` | existing | |
| `AGENT_EGRESS_NETWORK_TRUSTED` | existing | |
| `AGENT_EGRESS_NETWORK_CUSTOM` | existing | |
| `AGENT_EGRESS_NETWORK_FULL` | existing | |
| `AGENT_EGRESS_PROXY_URL_TRUSTED` | existing | The proxy a Trusted-tier sandbox is pointed at — the one enforcing the built-in registry list plus `AGENT_EGRESS_ALLOWLIST`. |
| `AGENT_EGRESS_PROXY_URL_CUSTOM` | existing | Same for the Custom tier, which supplies its own proxy by definition; nothing here renders a config for it. |
| `AGENT_EGRESS_PROXY_PORT` | existing | Port the rendered Squid config listens on. A non-numeric value is refused at render rather than defaulted, since a proxy silently on the wrong port is a sandbox with no egress filtering at all. |

## Web (`web/`)

Every one of these is baked into the bundle at build time and is therefore
**public**. Nothing secret may appear here — a `NEXT_PUBLIC_` variable is
readable by anyone who loads the page.

Baked in means each one reaches the app as a **build argument** and no other
way: a row here without an `ARG` in `deploy/control-plane/Dockerfile.web` and a
matching key under the `web` service's `build.args` is a variable an operator
can set everywhere and change nothing. Every row therefore names its compose
argument, and `test_web_build_arguments_are_wired` fails on a row that has
none.

| Variable | New? | Compose build argument | Notes |
|---|---|---|---|
| `NEXT_PUBLIC_BASE_PATH` | **NEW** | `AGENT_WEB_BASE_PATH` | Prefix the app is mounted under. `/agents` serves it inside the gateway's own hostname — same origin, so the session cookie needs no CORS and no DNS record is involved, and a rollback is a reverse-proxy edit rather than a DNS change waiting out a TTL. Empty for a deployment that owns its hostname. **Whatever this is, `IDENTITY_ALLOWED_REDIRECTS` on the gateway must contain `<origin><base path>/callback` verbatim** — that allowlist is matched as an exact string, and Next does not prefix a URL the code concatenates, so `callbackUri()` applies this by hand. |
| `NEXT_PUBLIC_AGENT_API_URL` | **NEW** | `AGENT_WEB_API_URL` | Control plane base URL. Empty means same-origin, which is the DR4 shape (one host, API under `/api`). Under a base path, the same-origin value is a relative prefix — `/agents/api` — which the reverse proxy strips before forwarding. A cross-origin value additionally needs CORS-with-credentials on the control plane, because the session is a cookie. |
| `NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL` | **NEW** | `AGENT_WEB_GATEWAY_AUTHORIZE_URL` | `${GATEWAY_BASE_URL}/authorize` (task C4). Unset means this deployment cannot sign anyone in, and says so instead of redirecting to a half-built URL. The gateway's token endpoint is **derived** from this URL's origin rather than configured separately — one variable, nothing to get out of step. |
| `NEXT_PUBLIC_APP_NAME` | **NEW** | `AGENT_WEB_APP_NAME` | Product name in the header and the document title. Defaults to `FreeInference Cloud Agent`. |
| `NEXT_PUBLIC_ORG_NAME` | **NEW** | `AGENT_WEB_ORG_NAME` | Optional organisation name beside the product name. Empty hides it. |
| `NEXT_PUBLIC_ORG_URL` | **NEW** | `AGENT_WEB_ORG_URL` | Link target for `NEXT_PUBLIC_ORG_NAME`. |
| `NEXT_PUBLIC_STATUS_URL` | **NEW** | `AGENT_WEB_STATUS_URL` | Status page, opened in a new tab from the header. Empty hides the link — an empty `href` resolves to the current page, so a Status link that is rendered anyway reopens the page the user is standing on. |
| `NEXT_PUBLIC_FEATURE_RAG` | **NEW** | `AGENT_WEB_FEATURE_RAG` | `true` shows the gateway's Docs Assistant link. Off by default; the page belongs to the gateway, not to this service. |
| `NEXT_PUBLIC_BUILD_SHA` | **NEW** | `BUILD_SHA` | Optional build identity, baked in by the deploy script. |
| `NEXT_PUBLIC_BUILD_TIMESTAMP` | **NEW** | `BUILD_TIMESTAMP` | Optional build identity. |

The source repo's frontend fetched `/site-config` from the gateway at runtime so
one bundle could serve several deployments. That endpoint is the gateway's and
is not part of either contract this service consumes, so the four branding
variables above replace it at build time.

## Set on the HybridInference side — not here

These are provisioned in the gateway's environment. Listed so the contract has
one place to be read from end to end.

| Variable | Task | Notes |
|---|---|---|
| `IDENTITY_JWT_PRIVATE_KEY` | C1 | RS256 private key, PEM. Public half is served at `/v1/identity/jwks`. |
| `IDENTITY_JWT_RETIRING_PUBLIC_KEYS` | C1 (review) | Optional. PEM public keys still published in JWKS during a signing-key rotation, so tokens minted with the old key stay verifiable until they expire. |
| `IDENTITY_ISSUER` | C3 | The `iss` claim in identity tokens. Falls back to the gateway's `base_url`; unset means issuance is switched off rather than tokens minted with an empty `iss`. Must equal the value this service verifies against. |
| `IDENTITY_JWT_RETIRING_PUBLIC_KEYS` | C1 | Public PEMs of previously-active signing keys, concatenated. Published alongside the current key so tokens issued before a rotation still verify. Keep a key here for at least the maximum token lifetime plus the JWKS cache window, then drop it. Public halves only — a retiring key must not be able to sign. |
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
