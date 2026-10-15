# Environment inventory

Use this inventory to find a configuration value, see which component reads it,
and learn where to set it. When application defaults differ from the supported
Compose deployment, both are listed.

For local setup, start with the [developer guide](docs/developer/development.md). For a
deployment, start with the
[control-plane configuration](deploy/control-plane/.env.example) and
[host configuration](deploy/host/.env.example). The
[release contract](deploy/release/README.md) describes packaged artifacts.
Names inherited from HybridInference remain in use; **NEW** and **MOVED** mark
earlier changes, not provisioning status.

> **Keep deployment values outside this public repository.**
>
> Store secrets in the operator's private environment/unit files or read-only
> key mounts. Keep actual identifiers, hostnames, port allocations, tunnel
> topology and app registrations in the private deployment overlay. Public
> examples use placeholders such as `agents.example.org`; documented protocol
> ports and defaults are safe to retain here.

## Settings and bootstrap configuration

### Sandbox Manager naming compatibility

The service is named **Sandbox Manager** (`sandbox-manager` in Compose and
`cloud_agent_host.sandbox_manager` in Python). Use `AGENT_SANDBOX_MANAGER_*`
names for new deployments. Existing values remain accepted:

| Current name | Previous name |
| --- | --- |
| `AGENT_SANDBOX_MANAGER_TOKEN` | `AGENT_WORKSPACE_BROKER_TOKEN` |
| `AGENT_SANDBOX_MANAGER_URL` | `AGENT_WORKSPACE_BROKER_URL` |
| `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` | `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` |
| `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` | `AGENT_BROKER_URL_ALLOWED_HOSTS` |
| `AGENT_SANDBOX_MANAGER_REVERSE_BIND` | `AGENT_BROKER_REVERSE_BIND` |
| `AGENT_SANDBOX_MANAGER_REVERSE_PORT` | `AGENT_BROKER_REVERSE_PORT` |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_HOST` | `AGENT_BROKER_REVERSE_TARGET_HOST` |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_PORT` | `AGENT_BROKER_REVERSE_TARGET_PORT` |
| `AGENT_SANDBOX_MANAGER_STOP_GRACE` | `AGENT_BROKER_STOP_GRACE` |

The supported host and local deployment scripts normalize the old token before
invoking Compose. For a direct `docker compose` invocation, provide
`AGENT_SANDBOX_MANAGER_TOKEN`; the recipe still refuses to render without it.

Host activation drains and removes the old service before creating its
replacement. Its old DNS alias and Python entry point remain available for
mixed-version deployments. Saved credentials keep their existing encryption
domain; no secret rotation or database migration is needed for this rename.
The physical `broker_url` database column, old host wire fields, and telemetry
producer ID remain compatibility details. New API fields and code use
`sandbox_manager_url`; the UI displays **Sandbox Manager**, including for old
measurement records.

Durable archive markers and deletion receipts also keep their existing paths
(`/var/lib/freeinference-broker` inside retained sandboxes and
`/var/lib/hybridinference/broker-archives` inside the manager). Renaming these
would hide existing lifecycle fences or pending deletion receipts.

Choose the configuration location by what the value controls:

- Use **Admin → Settings** for application behavior, integration URLs and
  credentials, publication, web appearance, operational intervals, upload and
  skill limits, task titles, and global sandbox/workspace limits.
- Use a server's **Servers** page for sandbox and workspace values that should
  apply only to that server.
- Keep bootstrap configuration in deployment files. This includes database
  access, encryption roots, recovery access, host provisioning, networking,
  isolation, image/build routing, and mounted paths.

Saved application settings live in the Agent database and remain authoritative
across restarts. The inventory below records built-in defaults and the
environment names used for an initial import. Once a setting is registered, the
service no longer reads it continuously from `.env`.

Run `alembic upgrade head` (through `0080_user_ssh_servers`) before
starting this release. The updated worker claim query requires
`agent_jobs.claim_retry_at`, and chat plugin selections require
`agent_threads.plugins`; existing queued jobs remain immediately eligible and
existing chats default to no plugins after the migrations. Migration 0049
starts every existing host without a deployment drain owner or release receipt;
0051 backfills a non-null drain-fence epoch for every host. The database rotates
that epoch whenever drain-relevant lifecycle, credential, placement, or
ownership state changes. Once the V2 control plane is activated, 0051 and
`AGENT_DEPLOY_DRAIN_FENCE_V2` are the application and schema rollback floor.
The V1 marker remains only so an older orchestrator can perform the bridge
upgrade; it is not a post-activation rollback target. Rebuild the sandbox image
to load selected plugins with the updated CLI wrappers. The main control-plane
listener then initializes the settings document once from its existing
environment. The
MCP-only listener
waits for that document and uses the same saved values.

After the initial import, edit values in **Admin → Settings**. Later
environment edits do not overwrite saved values. **Reset** restores a setting's
built-in default, including an empty default for a credential; it does not
restore an old environment value. Listeners synchronize changes every five
seconds, and the web server reads saved appearance settings for every request
without rebuilding or restarting the web image.

For each host sandbox or workspace setting, the service uses the first available
value in this order:

1. the override saved for that server;
2. the global value saved by an administrator;
3. the built-in default.

Migration `0036_host_settings_import` preserves valid nondefault legacy host
values as per-server overrides when an administrator has not already configured
them. Hosts without a complete report finish this import through their initial
heartbeats. An admin edit or reset excludes only the changed keys from pending
imports, so unrelated legacy values are preserved. Global exclusions
also apply to newly enrolled hosts. Later environment changes are ignored.
Clearing an override falls through to the global value or built-in default; it
never reactivates an old environment value.

Configure sandbox archives only in **Admin → Settings → Sandbox**. Cloud
Agent stores them as one revisioned policy in the Agent database. There is no
`AGENT_SANDBOX_ARCHIVE_*` environment surface. The destination is fixed at
`s3://freeinference/cloud-agent/sandbox`. The saved policy contains enablement,
region, bounded upload sizes, and either an AWS access key or the workload
identity fields (customer-managed KMS key ARN and expected bucket owner). The
secret access key is encrypted with the settings encryption key and is not
returned by the admin API. With no access key, sandbox managers authenticate
with their deployment workload identity through the standard AWS SDK credential
chain. See [Sandbox archives](docs/developer/operations.md#sandbox-archives) for IAM,
bucket versioning, rollout, and lifecycle semantics.

The database also stores the control-plane runtime identity, signing/encryption
keys, sandbox manager credentials, cookie/proxy policy, MCP target policy, and default
execution profile. During an upgrade, the main listener imports only newly
migrated fields that are missing. It never overwrites an existing value,
including an empty reset, and the relay waits for the import to finish. The
identity issuer stays pinned to this database. Existing user/compute encryption
keys cannot be replaced or reset through the settings editor without a separate
re-encryption procedure.

The Settings page identifies missing required fields and incomplete optional
integrations. If Gateway sign-in is not ready, an administrator can open
`<web base path>/setup` with `ADMIN_TOKEN`. Setup uses the same authenticated,
audited settings API. The recovery token stays in page memory and is never saved
in browser storage.

Keep bootstrap configuration in deployment files: database access; the settings
encryption root (`AGENT_SETTINGS_ENCRYPTION_KEY`, with the legacy environment
`AGENT_SESSION_SECRET` fallback); administrator recovery access (`ADMIN_TOKEN`);
host enrollment/sandbox manager provisioning; networking; tunnels/listeners; actual host
isolation; image/build routing; mounted paths; and trusted compute deployment
profiles. These values establish database access or configure processes outside
the control plane. Their names appear on the Settings page with hidden values.

## The one rule

> The gateway's `API_KEY_SECRET` and `JWT_SECRET_KEY` must never appear in this
> repository, in its configuration, or on any host that runs it.

The gateway mints and verifies inference grants. Cloud Agent verifies identity
tokens with public JWKS keys and signs its own tokens with independent secrets.
Any proposal to place either gateway signing secret here requires a design
review.

## Provisioning status

The original extraction provisioned staging on 2026-08-06 and production on
2026-08-07; [the cutover record](docs/agents/specs/cutover.md) documents that history. Those
dates do not show whether a current deployment is ready.

This repository publishes neutral releases. As a downstream operator, you own
your configuration, provisioning status, rollout record, and recovery evidence.
Keep secret material outside Git and back it up with the data it protects.

## Local development tools

These values are read by local tooling and are not saved application settings.

| Variable | New? | Value / who provisions |
|---|---|---|
| `CI_LOG_DIR` | **NEW** | Optional output directory set by the developer invoking `scripts/ci-local.sh`. Defaults to a fresh gitignored `.local-ci/<tier>.XXXXXX` directory in the checkout for each run. Contains gate logs and the database JUnit report; no application secret is required. |

## Control plane (`cloud_agent`)

### Local and CI permission regression

`AGENT_PERMISSION_TEST_IMAGE` enables the opt-in Linux UID regression in
`tests/external/test_shared_member_permissions.py`. It names an already-pulled
Docker image with Python 3 and GNU `find`/`stat`, such as `python:3.12-slim`.
After pulling the image, run:

```bash
AGENT_PERMISSION_TEST_IMAGE=python:3.12-slim \
  uv run pytest -q -m external tests/external/test_shared_member_permissions.py
```

The test uses disposable containers without network access or mounts and runs
the production normalizer as separate member UIDs without capabilities. CI runs
it before deployment; it needs no model API key or deployed-service setting.

### Database

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_DATABASE_URL` | **NEW** | Postgres DSN for a database owned exclusively by Cloud Agent. Do not share the gateway's database. |
| `AGENT_POSTGRES_USER` / `AGENT_POSTGRES_DB` / `AGENT_POSTGRES_PASSWORD` | existing | Supported control-plane Compose database initialization. User and database default to `cloudagent`; password is required. Keep the DSN consistent with these values. PostgreSQL applies initialization settings only to an empty data directory. |
| `TEST_DB_HOST` / `TEST_DB_PORT` / `TEST_DB_NAME` / `TEST_DB_USER` / `TEST_DB_PASSWORD` | **NEW** | Read by `make test-db` and the CI `dbtest` tier as five separate values, not a DSN. Defaults: `localhost` / `5432` / `hybridinference_test_db` / `postgres` / `postgres`. The database name must contain `_test_`; fixtures refuse other names. With xdist, each worker appends its ID and needs a pre-created database. CI creates `<TEST_DB_NAME>_gw0`…`_gw3` for `-n 4`. |

### Automatic PR merge archiving

These environment values are imported when the Admin Settings row is first
created. After that, edit the saved **Publication** settings. See
[the archiving runbook](docs/developer/automatic-pr-archiving.md).

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_PR_ARCHIVE_ENABLED` | `true` | Enable automatic archival after verifying that the current published PR merged. |
| `AGENT_PR_ARCHIVE_POLL_S` | `30` | Seconds between GitHub merge checks; saved setting accepts 5–3600 seconds. |

API timeout, batch size, retry delay limit, and repeated integration-failure
threshold are saved settings. The worker uses existing GitHub App credentials
or the configured publication token, and never passes them to the sandbox.

### pgAdmin database administration

The optional `pgadmin` Compose profile adds **Admin → Database → Open pgAdmin**.
The private service has no published port. Every request requires a current
`agent.admin` decision; pgAdmin also requires its own account login. Its
accounts, sessions, and saved connections persist in `agent-pgadmin`.

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_PGADMIN_URL` | optional | Private pgAdmin origin; use `http://pgadmin:5050` for the bundled service. Empty disables the launch link. Credentials, paths, queries, and fragments are refused. Set `COMPOSE_PROFILES=pgadmin` to start the service, or add `pgadmin` to existing profiles. Also names a service that the saved **Private pgAdmin URL** may select: besides the bundled service, that setting can only use the scheme, host and port of this variable or `AGENT_PGADMIN_BOOTSTRAP_URL`, so keep a custom address set here after the import. |
| `AGENT_PGADMIN_PUBLIC_URL` | optional override | Full public proxy URL ending in `/admin/agent/pgadmin`. Normally derived from `AGENT_WEB_BASE_URL` plus `/api/admin/agent/pgadmin`, preserving `/agents` when present. Set explicitly for a separately mounted API. Never derived from browser headers. |
| `AGENT_PGADMIN_BOOTSTRAP_DIR` | required for dashboard setup | Set `/var/lib/cloud-agent/pgadmin-bootstrap` for the bundled service. The first administrator chooses an email and password in **Admin → Database**. Credentials are committed once in the private `agent-pgadmin-bootstrap` volume, readable only by the control plane and pgAdmin's supplementary group. Passwords never appear in responses, audit records, Compose, or source control. Existing accounts and later password changes remain managed by pgAdmin. |
| `AGENT_PGADMIN_BOOTSTRAP_URL` | required for dashboard setup | Private origin used only to initialize the bundled service, normally `http://pgadmin:5050`. Setup saves this URL in Admin Settings when no pgAdmin URL has been saved. Subsequent requests respect the saved setting, including resets that disable access. |
| `AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL` | optional | Public proxy URL for first-time setup, ending in `/admin/agent/pgadmin`. Defaults to the saved public pgAdmin URL or the saved web base URL plus `/api/admin/agent/pgadmin`. |
| `AGENT_PGADMIN_ENTRYPOINT_FILE` | optional | Startup wrapper that waits for first-time dashboard setup before executing pgAdmin's native entrypoint. Defaults to the bundled `pgadmin-entrypoint.py`; artifact deployments resolve it from the selected release recipe. |
| `AGENT_PGADMIN_SERVERS_FILE` | optional | Read-only server-definition JSON. Defaults to the bundled `pgadmin-servers.json`, which registers `postgres:5432` with database/user `cloudagent` and no password. Supply an absolute custom file if `AGENT_POSTGRES_USER` or `AGENT_POSTGRES_DB` differs. Artifact deployments resolve the default file from the selected release recipe. Imported on first initialization; later edits are made in pgAdmin. |
| `AGENT_PGADMIN_COOKIE_SECURE` | optional | pgAdmin Python boolean, default `True`. Use `False` only for an HTTP local stack. The proxy independently scopes pgAdmin cookies to its public route, sets HttpOnly and SameSite=Lax, and requires Secure on HTTPS. |

### Gateway contracts

| Variable | New? | Value / who provisions |
|---|---|---|
| `GATEWAY_BASE_URL` | **NEW** | Origin of the HybridInference gateway this deployment federates with, e.g. `https://gateway.example.org`. A new value saved in Admin → Settings must use HTTPS unless it names localhost, a loopback address or a single-label name such as a Compose service; the same applies to `GATEWAY_JWKS_URL` and `GATEWAY_AUTHORIZE_URL`. A value imported or saved before that rule still loads. Because the dispatch token is sent to whatever URL is saved, changing this URL while a token is configured is refused unless the same update supplies the token again or clears it: on the settings page, clear the token, change the URL, then enter the token again. |
| `GATEWAY_JWKS_URL` | **NEW** | Explicit URL of the gateway's public key set, typically `<GATEWAY_BASE_URL>/v1/identity/jwks`; it is not derived automatically. The verifier caches the JWKS set for five minutes and refreshes for an unknown `kid`. Publish current and retiring public keys through rotation; removal takes effect when the cache expires. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | **NEW** | Bearer credential for minting, renewing, revoking and reading usage for inference grants. Generate random 32 bytes (a new value saved in Admin → Settings must be at least 32 bytes; a shorter value saved or imported earlier still loads, and startup logs `agent_weak_credential` until it is replaced); provision the same value on both sides and a distinct value for each environment. The saved token is sent only to `GATEWAY_BASE_URL`: Admin Settings refuses to change that URL while the token is kept, so clear the token, save the URL, then enter the token again, or save both in one update. |
| `GATEWAY_GRANT_BASE_PATH` | optional initial import | Grant service route prefix, default `/internal/agent-grants`; applies to mint, renew, revoke and usage queries. Use an absolute URL path without a query or fragment. Change it in Admin Settings; deployment clients apply it live. |
| `GATEWAY_GRANT_BINDING_ID` | optional; recommended before the first ledger-enabled deployment | Nonsecret identity of the logical gateway installation. When set, usage snapshots pin this identity across URL, grant-prefix and credential migrations. Change it when switching installations, including at the same URL. Without it, the fingerprint uses `GATEWAY_BASE_URL` and `GATEWAY_GRANT_BASE_PATH`, so changing either prevents refreshing old snapshots. Credential rotation always preserves the binding. Configure this only in the control plane; service credentials never enter snapshots or the browser. |
| `GATEWAY_ISSUER` | **Required for Gateway access** | Exact `iss` signed by the Gateway. It identifies the user namespace and need not equal the HTTP origin. Recorded in the Agent database; changing it requires a separate database or an explicit ownership migration. See [minimal identity migration](docs/developer/minimal-identity.md). |
| `GATEWAY_AUTHORIZE_URL` | **NEW** | Explicit consent-page URL, typically `<GATEWAY_BASE_URL>/authorize`; it is not derived automatically. The control plane performs the PKCE flow. Unset disables sign-in and reports it unavailable. |
| `AGENT_SESSION_REDIRECT_URI` | **NEW** | Public URL of `GET /v1/session/callback`, for example `https://agents.example.org/api/v1/session/callback`. It must exactly match an entry in the gateway's `IDENTITY_ALLOWED_REDIRECTS`. It is configured explicitly, never derived from `Host` or forwarded headers. |
| `AGENT_WEB_BASE_URL` | **NEW** | Public web-app URL, including its base path, for example `https://agents.example.org/agents`. Sign-in returns here after setting the cookie; unavailable sign-in returns with `?sign_in_error=unavailable`. Its origin is also one the main API accepts session-cookie writes from: a POST, PUT, PATCH or DELETE carrying the session cookie is refused with 403 when the browser reports it came from another origin (`Sec-Fetch-Site`, or `Origin` without it) that is neither this one, the request's own host, nor the pgAdmin public URL's. |
| `AGENT_GATEWAY_URL` | existing | Gateway URL as seen *from inside the sandbox* — the relay address, not the public URL. Differs from `GATEWAY_BASE_URL` by design. |

### Own secrets

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_SETTINGS_ENCRYPTION_KEY` | **NEW** | Optional dedicated key of at least 32 bytes for encrypted admin setting secrets. If absent, `AGENT_SESSION_SECRET` supplies the key. Keep the same effective key on the main API and MCP-only listener, and preserve it with database backups. Changing the effective key requires re-encrypting existing settings first; session-key rotation also changes it when the fallback is in use. |
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | **NEW** | Encrypts stored source-control credentials, per-user MCP credentials, and reusable cloud-environment variables/overrides under separate domain separators. Generate random 32 bytes, unique per environment. A new value saved in Admin → Settings must be at least 32 bytes; a shorter value saved or imported earlier still loads, and startup logs `agent_weak_credential` until it is replaced. Without a key, none store plaintext; MCP offers an empty picker and value-bearing environment operations fail closed. Credentials from the pre-extraction gateway key require re-wrapping, not copying. |
| `AGENT_CONTROL_TOKEN_SECRET` | **NEW** | Signs host/worker control tokens. Generate random 32 bytes, unique per environment and independent of gateway secrets. A new value saved in Admin → Settings must be at least 32 bytes; a shorter value saved earlier still loads, and startup logs `agent_weak_credential` until it is replaced. |
| `AGENT_SESSION_SECRET` | **NEW** | Signs the web session cookie. Generate random 32 bytes, unique per environment. |
| `AGENT_DEFAULT_EXECUTION_PROFILE` | **NEW** | Server-owned policy for new root threads. Application default: `trusted-v1`; supported production Compose default: `untrusted-kata-v1`; local Compose uses `trusted-v1`. The untrusted profile requires the current enrolled Host credential and the exact claiming replica's attestation of `io.containerd.kata.v2` with a guest kernel different from the host. The trusted profile permits runc for trusted internal/dev repositories. Follow-ups and Fork inherit the stored policy; callers cannot select it. |
| `AGENT_ONCALL_DISPATCH_TOKEN` | **NEW** | Authenticates the gateway alert relay on `/v1/agent/service/oncall/*` — one credential, one door. Random 32 bytes (a new value saved in Admin → Settings must be at least 32 bytes; a shorter value saved or imported earlier still loads, and startup logs `agent_weak_credential` until it is replaced), same value in the relay's `.env.oncall`, distinct per environment. Unset opens nothing (the door then answers only to `ADMIN_TOKEN`). |
| `AGENT_ONCALL_USER_ID` | **NEW** | Gateway user id every on-call job runs as. Its role decides which models the job's grant may carry (`glm-5.2` needs `internal`+), and its quota is what the analysis spends. Unset: the on-call door answers 503, the rest of the service is unaffected. |
| `AGENT_ALERT_WEBHOOK_URL` | **NEW** | Operator-owned JSON webhook for queue incidents, recovery and compute replacement events. Queue monitoring runs every minute, reports ten-minute-old runnable queue heads after two observations, retries delivery on the next pass and repeats unchanged incidents at most every 30 minutes. Delivery timeout: five seconds. Queue payloads contain event, deployment SHA and aggregate counts; compute events also contain Host identity and reason codes. Unset retains structured logs. Downstream distributions own deployment-workflow alerts. This URL never enters runners or sandboxes. |
| `AGENT_RESIDENCY_TTL_S` | **NEW** | How long a member's independently fenced live-process residency and short-lived inference grant survive between host heartbeats. Default 1800. Shared-live uses one residency per chat membership. This is credential/process ownership, not a second chat lifecycle clock: `AGENT_CHAT_IDLE_TIMEOUT_S` alone decides when a settled retained sandbox group is stopped. |
| `ADMIN_TOKEN` | existing | Optional machine-to-machine admin credential. An unset value opens no token access; admin sessions remain a separate authorization path. Use at least 32 random bytes; startup logs `agent_weak_credential` for a shorter value. |

### Sandbox deletion archives

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_SANDBOX_ARCHIVE_S3_BUCKET` | **NEW**, optional initial import | S3 bucket for stopped sandbox archives. Empty disables sandbox deletion. Saved as `sandbox_archive.bucket` in Admin → Settings → Application → Sandbox archives. |
| `AGENT_SANDBOX_ARCHIVE_S3_REGION` | **NEW**, optional initial import | AWS region for the archive bucket. Empty uses the standard SDK region configuration. |
| `AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT` | **NEW**, optional initial import | Optional HTTPS S3-compatible endpoint. It must support conditional `PutObject` and full-object SHA-256 checksum retrieval through `HeadObject`; an ETag or user-supplied metadata alone never permits deletion. Empty selects AWS S3. |
| `AGENT_SANDBOX_ARCHIVE_S3_PREFIX` | **NEW**, optional initial import | Object prefix for new archive requests, default `sandbox-archives`. Each sandbox/request pair receives its own immutable object. Completed receipts retain their original bucket and key after configuration changes. |
| `AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE` | **NEW**, optional deployment path | Absolute path to an operator-owned private Compose override. `deploy.sh` requires a readable regular file and appends it after release-owned overlays in both source and artifact modes. Keep the file outside the checkout and restrict it to the main `control-plane` service. |
| Standard AWS SDK credentials | **NEW**, deployment infrastructure | Provision only on the main control plane through the standard SDK chain, preferably an IAM workload role. For environment or shared-profile credentials, set `AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE` to a private override for the main `control-plane` service; the base Compose does not forward AWS credential variables. No long-lived AWS credential is sent to a runner host, sandbox, web application or MCP listener. |

See [sandbox archive storage](docs/developer/operations.md#sandbox-archive-storage) for
permissions, retention and verification requirements.

### Behaviour switches read by the control plane

| Variable | New? | Value / who provisions |
|---|---|---|
| `COMPOSE_PROFILES` (`edge`, `pgadmin`) | **NEW** | Compose-level, read by `docker compose` not by this service. `edge` starts the nginx edge for a deployment that owns its own hostname: `/api/*` to the control plane with the prefix stripped, everything else to the web app. `pgadmin` starts the private PostgreSQL administration UI. Combine profiles with commas, for example `edge,pgadmin`. |
| `AGENT_EDGE_BIND` / `AGENT_EDGE_PORT` | **NEW** | Where the edge listens; defaults `127.0.0.1` / `3003`. Only meaningful with the `edge` profile. |
| `AGENT_CONTROL_PLANE_BIND` / `AGENT_CONTROL_PLANE_PORT` | existing | Compose main-API binding, defaults `127.0.0.1` / `8000`. Put a TLS reverse proxy in front of public access. |
| `AGENT_MCP_RELAY_BIND` / `AGENT_MCP_RELAY_PORT` | existing | Compose MCP-only listener binding, defaults `127.0.0.1` / `8001`. Keep sandbox tools on this listener. |
| `AGENT_WEB_BIND` / `AGENT_WEB_PORT` | existing | Compose web-server binding, defaults `127.0.0.1` / `3002`. |
| `AGENT_EDGE_CONFIG_FILE` | existing | Host-side nginx configuration mounted read-only by the edge service. Default `./edge.nginx.conf`, relative to the Compose file. |
| `AGENT_NETWORK_NAME` | existing | Control-plane Compose network name, default `cloud-agent`. Use distinct names for independent stacks. |
| `AGENT_SESSION_COOKIE_SECURE` | **NEW** | Defaults to on. Set to `false` **only** for a developer running the whole stack on plain `http://localhost`; a session cookie without `Secure` is one anyone on the path can take. On, the session cookie is `__Host-cloud_agent_session`, which another host on the same site cannot set; a `cloud_agent_session` cookie from before that rename is still accepted until it expires, and sign-in and sign-out remove it. Off, the cookie keeps the plain name, because browsers refuse the prefix without `Secure`. |
| `AGENT_API_DOCS` | **NEW** | Defaults to off. Set to `true` on a development stack to serve `/docs`, `/redoc` and `/openapi.json` on the main API. Off, none of the three is served: the schema is unauthenticated, and the two pages load scripts from a CDN at a floating version. The MCP-only listener never serves them. |
| `AGENT_MAX_STREAMS_PER_USER` | **NEW** | Default `24`. How many live-update streams (the change stream and job event streams) one account may hold open on one control-plane process; one more answers 429 with `Retry-After`, which the web app backs off on. A browser tab holds one change stream and one stream per conversation it is watching. A missing, non-numeric or non-positive value uses the default. |
| `AGENT_TRUST_PROXY_HEADERS` | **NEW** | Defaults to off. Set to `true` only when a proxy this deployment controls sits directly in front of the control plane and writes `X-Forwarded-For`; both shipped nginx configurations overwrite it with the peer address. Only the rightmost entry, the one that proxy wrote, is recorded in the admin audit trail, so a proxy that appends instead works too; entries to its left are the caller's own and are ignored. Behind a further proxy, such as a tunnel or load balancer in front of the edge, the recorded address is that hop's unless the edge uses nginx's realip module as its configuration describes. |
| `AGENT_RUNTIME_REGISTRY_URL` | **NEW** | The npm registry the admin Settings page asks for each agent's latest release, and checks a requested version against. Default `https://registry.npmjs.org`. Set the same value on the hosts, which download from it. An unreachable registry shows on the page and blocks updating; nothing else depends on it. |
| `AGENT_DEPLOYMENT_SHA` | **NEW** | Exact commit this API serves. Required by the production control-plane Compose stack and exported by `deploy/control-plane/deploy.sh`; do not hand-edit it during a rollout. New retained-group claims require the polling replica to advertise the matching completed host marker, so a pending or failed host deployment cannot claim through a cold-start API. The local stack generates its own stack-scoped marker. |

### Source control

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_GITHUB_APP_ID` | existing | Identifies the GitHub App used for repository access. Setting it enables key loading; an unreadable key makes GitHub temporarily unavailable and is retried every 30 seconds. Without it, key settings are ignored. Configure the App's callback/setup URLs for this deployment. See [credentials](docs/developer/operations.md#2-credentials). |
| `AGENT_GITHUB_APP_CLIENT_ID` | existing | GitHub App OAuth client ID for users connecting their own GitHub account. |
| `AGENT_GITHUB_APP_CLIENT_SECRET` | existing | GitHub App OAuth client secret. Store outside Git; rotate when changing the App's audience. |
| `AGENT_GITHUB_APP_PRIVATE_KEY_PATH` | existing bootstrap | Path inside the container to a read-only PEM key. Supported Compose mounts it at `/etc/cloud-agent/github-app.pem`. An encrypted inline key saved in Admin Settings takes precedence over this mount. Keep private keys out of environment variables, Docker inspection output and log pipelines. |
| `AGENT_GITHUB_APP_PRIVATE_KEY_FILE` | existing | Host-side key file mounted read-only by control-plane Compose at `/etc/cloud-agent/github-app.pem`. Default `/dev/null` supplies no key. This differs from the in-container `_PATH` setting. |
| `AGENT_GITHUB_APP_PRIVATE_KEY` | existing initial import | Inline PEM for a one-time import in deployments that already supply it. Compose does not forward this variable. Set or replace an inline key in Admin Settings, where it is encrypted and never returned to the browser. The mounted `_PATH` form remains available for deployment-managed keys. |
| `AGENT_GITHUB_APP_INSTALL_URL` | existing | `https://github.com` authorization/installation URL used by the Connect GitHub flow; any other host, scheme or port is refused when saved or imported. The App, OAuth credentials and this URL must all be configured for user connections. |
| `AGENT_GITHUB_API` | existing | Defaults to `https://api.github.com`. Publication pushes to the Git host paired with it (`github.com` for the default, otherwise this URL's own host), so a token goes back only to the host that minted it. GitHub Enterprise Server is not supported: the Connect GitHub flow's OAuth and installation URLs are GitHub.com's. |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` | existing | GitLab.com OAuth application client ID for user connections. |
| `AGENT_GITLAB_OAUTH_CLIENT_SECRET` | existing | GitLab.com OAuth client secret; store outside Git. |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | existing | Public OAuth callback URI registered with GitLab. Must use HTTPS, except for localhost development, and anything else is refused when saved or imported; update the registration when the deployment URL changes. |
| `AGENT_REPO_ALLOWLIST` | existing | Comma-separated `owner/name` entries or `owner/*` rules for deployment-owned repository access. It is not per user: every user admitted to Cloud Agent may have the platform clone and publish to the listed repositories with this deployment's GitHub App or `AGENT_GITHUB_TOKEN` credential, without connecting GitHub, so list only repositories all of them may work on. Matching is case-insensitive. Empty grants no allowlist access; users may still use repositories authorized through their own GitHub connection. |
| `AGENT_GIT_AUTHOR_NAME` / `AGENT_GIT_AUTHOR_EMAIL` | existing | Commit identity for published work. Defaults: `HybridInference Agent` / `agent@localhost`. |
| `AGENT_GITHUB_TOKEN` | existing | Optional fallback GitHub credential for publication without a GitHub App. Unset does not disable App-based publication. Long-lived publication credentials stay in the control plane and never enter sandboxes. |
| `AGENT_PUBLISH_BASE_BRANCH` | existing | Pull-request target branch, default `dev`. Keep it nonempty: direct application configuration reads a blank value as a blank branch. Compose uses `:-dev` to apply the default to blank values. |

### MCP

Cloud Agent owns MCP discovery, per-user credentials, authorization and attempt
fencing. The gateway serves inference and has no MCP role.

Users add remote URLs, imported `mcpServers` snippets or stdio launch specs.
Templates only prefill the connection form; environment variables neither add
servers nor grant access to them. Credentials live per user in
`agent_mcp_connections`, encrypted with
[`AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`](#own-secrets). The settings below control
relay reachability and connection target policy.

| Variable | New? | Value / who provisions |
|---|---|---|
| ~~`AGENT_MCP_CONFIG`~~ | **removed** | There is no deployment-wide MCP registry. It named a file of remote servers with their credentials, and every user who could create a job could use them under that one identity — the team-MCP shape this product does not offer. Per-user connections replace it; the relay, the proxy and the sandbox boundary stay. Listed here rather than deleted because an existing deployment has this set, and a variable that silently stops meaning anything is worse than one recorded as gone. |
| `AGENT_SANDBOX_MCP_URL` | **host only** | The MCP relay **as this host's sandboxes reach it**. The host compose derives it from the host's own MCP tunnel (`http://<AGENT_MCP_HOSTNAME>:<LISTEN_PORT>`) and gives it to the runner and the sandbox manager, which hand it to every member CLI that has tools; it is also the relay the host rules admit. The control plane no longer reads it: the copy it handed over at claim time had to match every host's alias, and left unset it failed every turn whose owner had connected a tool. A host without one fails such a turn before any container work, telling the owner the site is misconfigured and to ask an admin to finish setting it up. |
| `AGENT_MCP_TUNNEL_*` | **NEW** | MCP-only SSH tunnel. Required suffixes: `SSH_DESTINATION`, `SSH_KEY`, `SSH_KNOWN_HOSTS`. Defaults for `SSH_PORT` / `REMOTE_HOST` / `REMOTE_PORT` / `LISTEN_PORT`: `22` / `127.0.0.1` / `8001` / `8000`. Its remote destination must be the MCP-only listener; main API port 8000 serves no tools. Give it a distinct, narrowly restricted key. |
| `AGENT_MCP_HOSTNAME` | **NEW** | The relay's alias on both container networks. Defaults to `agent-mcp`. |
| `AGENT_CONTROL_PLANE_TUNNEL_*` | **NEW** | Runner-only SSH tunnel to the main API. Required suffixes: `SSH_DESTINATION`, `SSH_KEY`, `SSH_KNOWN_HOSTS`. Defaults for `SSH_PORT` / `REMOTE_HOST` / `REMOTE_PORT` / `LISTEN_PORT`: `22` / `127.0.0.1` / `8000` / `8000`. It joins only the runner network and is unreachable from sandboxes. Use a key distinct from the MCP and inference keys. |
| `AGENT_CONTROL_PLANE_HOSTNAME` | **NEW** | That tunnel's alias, on the runner network only. Defaults to `agent-control-plane`. |
| `AGENT_MCP_PUBLIC_BASE_URL` | optional override | The public API base URL used to register and receive MCP OAuth callbacks, including the API prefix — for example `https://example.com/agents/api`. The callback is `<base>/v1/agent/mcp/oauth/callback/<connection-id>`. After processing it, the service returns the browser to `<AGENT_WEB_BASE_URL>/integrations`; the web and API bases may differ. When unset, reuse `AGENT_SESSION_REDIRECT_URI` with its exact `/v1/session/callback` suffix removed, preserving the public API origin and proxy prefix. This works with the existing required sign-in configuration; use the override only if MCP callbacks have a separate public address. Never derived from request headers. If neither setting supplies a callback address, browser authorization is unavailable; static credentials still work. |
| `AGENT_MCP_GITHUB_CLIENT_ID` / `AGENT_MCP_GITHUB_CLIENT_SECRET` | optional legacy import | Configure GitHub MCP browser sign-in in Admin Settings → MCP. Register a service-owned GitHub OAuth App or GitHub App with callback `<public API base>/v1/agent/mcp/oauth/callback/github` (including the API proxy prefix). Both client values and a public callback base are required. The preset then defaults to browser sign-in; each user authorizes their own account without registering an app or pasting a token. The client secret stays in encrypted settings and pending OAuth flows, while user tokens remain in separate MCP connections. Prefer a GitHub App limited to the required repositories and permissions, including Contents and Pull requests read and write access when agents publish changes; OAuth Apps request `repo` and `read:org` scopes (GitHub's `repo` scope permits writes). A personal access token remains available for narrower repository access. |
| `AGENT_MCP_ALLOW_PRIVATE_TARGETS` | **NEW** | Whether a user-added MCP connection may dial private and loopback addresses. Off by default: a remote connection that reaches loopback reaches **this** service, and a user who needs a private network should run a connector instead. Turning it on still refuses the ranges this platform never dials, so it cannot be used to reach the cloud metadata service. |
| `AGENT_MCP_ALLOW_PLAIN_HTTP` | **NEW** | Whether a user-added MCP connection may use `http://` as well as `https://`. Off by default because the credential would travel in the clear. A deployment whose connector bridge is genuinely HTTP inside a private network turns it on deliberately; it is a separate variable from the private-target one because it is a separate risk. |
| `AGENT_MCP_SERVERS` | **NEW** | **Not operator-set — listed so nobody provisions it.** The name the runner publishes the relay endpoints under for the image-shipped wrappers, as one JSON object of `{alias: url}`. The wrappers merge it into the CLI config they already write, under whichever key their CLI spells MCP with, and add the token reference in that CLI's own syntax — which is not portable between them. A value put here by hand would point a sandbox at servers nobody connected. |
| `AGENT_MCP_TOKEN` | **NEW** | **Not operator-set — listed so nobody provisions it.** The name the generated sandbox runtime config *references* (`${AGENT_MCP_TOKEN}`) for the relay credential the runner injects at launch: a chat process's residency credential, or a turn's own token. A value put here by hand would be a static tool credential shared by every job, which is the thing these scoped credentials exist to avoid. |

### Sandbox settings from the admin pages

Host settings can be set from the admin pages instead of in each host's
`.env`, including `AGENT_TIMEOUT_S`, `AGENT_CHAT_IDLE_TIMEOUT_S`,
`AGENT_SOURCE_IMPORT_MAX_MIB`, `AGENT_SHARED_RESIDENT_CAPACITY`,
`AGENT_STORAGE_MIN_AVAILABLE_MIB`,
`AGENT_SANDBOX_CPU_MILLICORES`, `AGENT_SANDBOX_MEMORY_MIB`, `AGENT_SANDBOX_DISK_MIB`,
`AGENT_TERMINAL_IDLE_TTL_SECONDS`, `AGENT_TERMINAL_HARD_TTL_SECONDS` and
`AGENT_TERMINAL_MAX_SESSIONS`. The **Settings** page sets a value for every
server (`PUT /admin/agent/host-settings`), and a server's details on the
**Servers** page set one for that server alone
(`PUT /admin/agent/runner-hosts/<host>/settings`). For each setting the host
applies, highest first:

1. the value set for that server;
2. the value set for every server;
3. the built-in default.

The control plane sends complete effective values, including defaults, on each
heartbeat. Host environment values apply only before the first settings answer
and remain visible as diagnostic local values. Clearing a per-server value
uses the global value or built-in default; clearing a global value uses the
built-in default. The bounds an admin may set are in
`cloud_agent_shared/host_settings.py` and are typo guards, not capacity statements.

Container CPU, memory, and disk settings apply to new sandbox groups. Resuming a
chat or forking it preserves the group's original limits; all members share
those resources. CPU is entered as vCPU in the admin pages and stored in
millicores (1 vCPU = 1000 millicores). Memory and disk accept MiB or GiB.
Defaults are 2 vCPU, 4096 MiB memory, and no per-container disk quota. Initial
environment values are `AGENT_SANDBOX_CPU_MILLICORES=2000`,
`AGENT_SANDBOX_MEMORY_MIB=4096`, and `AGENT_SANDBOX_DISK_MIB=0`.

A positive disk limit sets Docker's `--storage-opt size=<MiB>M` on the writable
layer, including the workspace, caches, and installed files. It excludes the
read-only image and memory-backed temporary files. This requires Docker
`overlay2` on an XFS backing filesystem mounted with project quotas (`pquota`
or `prjquota`). Docker refuses container creation when the storage backend does
not support the requested quota. Set the disk value to 0 to create groups with
no disk quota; resetting an override inherits the global value or default.
A positive disk limit is recommended on every server that runs the untrusted
profile (`untrusted-kata-v1`), whose sandboxes otherwise share one unbounded
disk. It is not required: a server at 0 still takes untrusted work, and the
control plane and runner log a warning and the Servers page shows **No
container disk quota** for it.
The runner's existing storage attestation continues to measure the host
filesystem rather than each sandbox's quota.

The same cards also manage workspace and terminal resource limits. These
additional environment names supply initial local values only:

| Variable | New? | Initial default / purpose |
|---|---|---|
| `AGENT_TERMINAL_MAX_SESSIONS_PER_WORKSPACE` | **NEW** | 4 open terminals per shared workspace. |
| `AGENT_WORKSPACE_COMMAND_TIMEOUT_S` | **NEW** | 60 seconds for workspace commands and Git inspection. |
| `AGENT_WORKSPACE_FILE_WRITE_MAX_MIB` | **NEW** | 2 MiB for one workspace file replacement. |
| `AGENT_WORKSPACE_FILE_PREVIEW_MAX_KIB` | **NEW** | 512 KiB for one file preview. |
| `AGENT_WORKSPACE_COMMAND_OUTPUT_MAX_MIB` | **NEW** | 2 MiB of workspace command output. |
| `AGENT_WORKSPACE_DIRECTORY_MAX_ENTRIES` | **NEW** | 5,000 entries per directory listing. |
| `AGENT_WORKSPACE_DOWNLOAD_MAX_MIB` | **NEW** | 1,024 MiB for one whole-workspace download, measured before compression. |
| `AGENT_TERMINAL_BUFFER_MAX_MIB` / `AGENT_TERMINAL_BUFFER_MAX_EVENTS` | **NEW** | 2 MiB / 4,096 events retained for terminal reconnects. |
| `AGENT_TERMINAL_OUTPUT_MAX_MIB` | **NEW** | 32 MiB of output over one terminal's lifetime. |
| `AGENT_TERMINAL_INPUT_MAX_KIB` | **NEW** | 64 KiB of input per terminal request. |
| `AGENT_TERMINAL_MAX_STREAMS_PER_SESSION` | **NEW** | 4 concurrent viewers per terminal. |

Workspace commands and Git inspection are also bounded in number, by the
broker rather than by a setting: at most 16 run at once on one host and 4 for
one chat, on a thread pool of their own, and one more is refused with 429
rather than queued behind them.

The values ride the heartbeat answer, so a host applies a change within one
heartbeat (about 30 seconds) with no redeploy. Nothing running is interrupted:
a turn keeps the time limit it started with, an existing sandbox keeps the
session capacity and CPU, memory, and disk limits it was created with, and the sandbox manager's terminal limits apply from its
next sweep. Each host reports what it applies and what its own `.env` gives,
and the page shows a value as *applying* until the host confirms it. A runner
that predates this (it does not advertise `host-settings-v1`) keeps its `.env`
values until it is redeployed, and the page says so.

### Optional compute-resource management

These values belong only to the stable main control plane, not its MCP-only
listener, any Host, or any sandbox. See [the compute runbook](docs/developer/operations-compute.md)
for deployment profiles, verified SSH access and CloudLab endpoint setup.

| Variable | Meaning |
| --- | --- |
| `AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY` | **NEW** | Control plane. Where automatic Server setup looks for the sandbox image this release published, as `<repository>:sandbox-<commit>`. Default `ghcr.io/harvardmadsys/cloud-agent-sandbox`; empty disables the lookup. When the image exists (checked anonymously), the generated Host configuration pulls `<repository>:sandbox-{deployment_sha}` with `AGENT_SANDBOX_IMAGE_PULL=true`, so its chats record a registry digest and can be restored on another Host. Otherwise the Host builds its own image, as before, and its chats stay on it. |
| `AGENT_COMPUTE_SECRET_KEY` | Independent encryption key, at least 32 bytes, for stored compute connections and Admin-managed deployment profiles. The supported deployer generates and persists a random value in `.env` when blank. Preserve it with database backups. |
| `AGENT_COMPUTE_CONFIG` | Absolute path inside the control-plane process to the operator-owned JSON deployment configuration. |
| `AGENT_COMPUTE_DIRECTORY` | Absolute host directory mounted read-only at `/run/compute` by the optional Compose overlay; `deploy.sh` includes the overlay when this is set. |
| `AGENT_CONTROL_PLANE_SSH_HOST` | Editable first-time Server setup address for the service node. The supported deployer defaults to the node's FQDN, falling back to its hostname; set an SSH address explicitly when using another deployment system or a different SSH alias. Saved setups take precedence. |
| `AGENT_GATEWAY_SSH_HOST` | Optional first-time Server setup gateway address override. Defaults to the hostname of the saved Gateway API URL, such as `freeinference.org`; use an override when SSH uses a different address. |
| `AGENT_CLOUDLAB_API_URL` | Operator-verified HTTPS Portal API base. Leave unset to disable the CloudLab adapter. |
| `AGENT_COMPUTE_REGISTRATION_URL` | Optional. The HTTPS address new CloudLab nodes post their SSH host keys to, ending in `/v1/compute/node-registrations`. When unset it is derived from `AGENT_SESSION_REDIRECT_URI` (its `/v1/session/callback` suffix replaced), like MCP callbacks. Only HTTPS is used: a node checks the control plane's certificate before sending its one-time token. Without either, nodes set to register their keys cannot be created. |

Without this configuration the existing Host management remains available;
resource creation requires configured credentials and deployment profiles.

## Host agent (`cloud_agent_host`)

### The stack's own identity

Read by `deploy/host/deploy.sh` and `deploy/host/sandbox_host.sh` rather than by
compose, because they name the stack rather than configure it.

| Variable | New? | Value / who provisions |
|---|---|---|
| `COMPOSE_PROJECT` | **NEW** | Required host-stack project name, with no host default. It prefixes containers, images and networks; reusing a name lets a second deployment recreate the first stack. Give sibling environments distinct names, such as `cloud-agent-sandbox-staging` and `cloud-agent-sandbox-prod`. The control-plane deploy script separately defaults its project to `cloudagent`. |
| `AGENT_HOST_STATE_ROOT` | existing | Host path for persisted enrollment state and readiness markers. Supported host Compose default: `/var/lib/hybridinference/agent-host`. Set a unique path per execution environment; it is required explicitly when `SIBLING_COMPOSE_PROJECTS` is set. Back it up with retained host state. |
| `AGENT_RUNNER_STOP_GRACE` / `AGENT_SANDBOX_MANAGER_STOP_GRACE` | existing | Compose shutdown windows, defaults `5m` / `30s`. The runner drains active children; sandbox manager cleanup follows the runner drain. |
| `SIBLING_COMPOSE_PROJECTS` | **NEW** | Comma-separated projects on this machine that are the *other* environment rather than a stray copy of this one. Setting it makes `AGENT_HOST_STATE_ROOT` required: sharing that host path means sharing an enrolment credential. Chat workspaces are Docker writable layers and have no host directory. The deploy's guard reports containers of this stack's shape under any project it does not know; a legitimate sibling must be named or it trips that guard forever. |
| `SHARED_READY_TIMEOUT` | **NEW** | Deployment-script wait for the two parallel rollout halves, default 900 seconds. The host uses the same bound while waiting for cold-start inference/MCP/control endpoints; the control deploy uses it while waiting for a fresh exact-SHA heartbeat and reverse sandbox manager probes. This is a rollout watchdog, not a chat/session timeout. |

### Scheduling

| Variable | New? | Notes |
|---|---|---|
| `AGENT_HOST_CREDENTIAL` | **NEW** | What `/v1/agent/host/register` hands back. Authenticates this machine's heartbeats and claims and names it; there is no shared claim credential or request-body host fallback. Revoked by removing the host. The runner normally reads the persisted file instead of setting this variable. |
| `AGENT_LEASE_TTL` | existing | Attempt lease horizon, default 120 seconds. Inference grant TTL is independent: `grants.GRANT_TTL_S = RENEWAL_INTERVAL_S × RENEWAL_HEADROOM_PASSES = 30 × 4 = 120 seconds`. Changing the lease horizon does not change grant lifetime. See [lease troubleshooting](docs/developer/runner-lease-troubleshooting.md). |
| `AGENT_TIMEOUT_S` | existing | Hard watchdog for one actively executing Agent CLI turn (default 3600 seconds). Hitting it fails/fences that turn; it does not mean the chat was idle and is not an alternate sandbox lifecycle timeout. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_RUNNER_HOST` | existing | Execution environment name redeemed by `AGENT_ENROLL_TOKEN` on first registration and shared by every turn the machine runs. After enrollment, the credential is authoritative; changing this value does not rename the environment. Use `AGENT_PHYSICAL_HOST` to group environments for display without changing existing identities. |

### Sandbox Manager, runtime and reporting

| Variable | New? | Notes |
|---|---|---|
| `AGENT_SANDBOX_MANAGER_URL` | existing | How a runner dials its sandbox manager — normally the Compose service URL. The runner advertises `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` (or this value only when the control plane truly shares that network) at enrollment. The control plane always resolves sandbox managers through the attempt's enrolled host row and never treats this as a global fallback. |
| `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` | NEW | What the runner *reports* at enrolment when that differs from what it *dials*. It may be omitted in the integrated local stack, where the control plane and sandbox manager share a network. It is required by `deploy/host/docker-compose.sandbox.yml`, including when its project happens to be on the same physical machine as the control-plane project: separate Compose networks cannot resolve the sandbox manager service name. Set it to the far end of the reverse forward (`AGENT_SANDBOX_MANAGER_REVERSE_BIND`/`_PORT`) and never without that tunnel; otherwise every terminal, file listing and Git status answers 503. |
| `AGENT_SANDBOX_MANAGER_TOKEN` | existing | Pool-wide sandbox manager credential: provision the same value in the control plane and every host. Host records contain sandbox manager URLs, not separate credentials. A compromised host's token therefore authenticates to other sandbox managers. Compare SHA-256 fingerprints during provisioning without displaying the secret; a mismatch causes sandbox manager 401 responses. Per-host sandbox manager credentials require a separate distribution design. |
| `AGENT_SANDBOX_MANAGER_REVERSE_BIND` / `AGENT_SANDBOX_MANAGER_REVERSE_PORT` | existing | Required reverse listener for standalone host deployments. Bind the actual stable control-plane Docker bridge gateway address, never loopback or `0.0.0.0`; use a distinct port per simultaneously deployed Host. The advertised sandbox manager URL must match it. |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_HOST` / `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_PORT` | existing | Host-side target of that reverse forward, defaults `sandbox-manager` / `8092`. |
| `AGENT_TUNNEL_*` | existing | Inference SSH tunnel. Required suffixes: `SSH_DESTINATION`, `SSH_KEY`, `SSH_KNOWN_HOSTS`. Defaults for `SSH_PORT` / `REMOTE_HOST` / `REMOTE_PORT` / `LISTEN_PORT`: `22` / `127.0.0.1` / `8080` / `8080`. Use a dedicated key restricted to this forward, without shell authority. |
| `AGENT_GATEWAY_HOSTNAME` | existing | Inference relay alias on runner and relay-discovery networks, default `agent-gateway`. |
| `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` | **NEW** | Control-plane allowlist for sandbox manager addresses, comma-separated. A bare hostname admits that host on any scheme and port; a `scheme://host:port` origin admits exactly that origin, which is what separates sandbox managers sharing one reverse-tunnel address. Hosts propose their sandbox manager URL at enrollment; restrict it to prevent a mistaken or compromised registration receiving the pool credential at an arbitrary address. Unset permits any hostname for upgrade compatibility. Sandbox manager URLs are always origins: a path, query, fragment or userinfo is refused. Automatic CloudLab setup checks for the bridge address as a bare hostname entry. Link-local addresses, where cloud metadata services answer, are refused whatever this is set to, including for sandbox manager URLs already stored. |
| `AGENT_CONTROL_PLANE_URL` | **NEW** | Runner URL for registration, claims and heartbeats. Standalone Compose derives it from the main-API tunnel alias `AGENT_CONTROL_PLANE_HOSTNAME`, not the MCP alias. Pointing it at the MCP-only listener returns 404 for control requests. |
| `AGENT_WORKER_ID` | existing | Worker-name prefix, default `runner`. The runner appends process identity so concurrent turn children have distinct worker names. |
| `AGENT_REPO_CLONE_BASE_URL` | **NEW** | Operator-selected base used by the trusted runner for source clone URLs; defaults to `https://github.com`. The complete local stack points it at its private `git://local-repo` fixture so no external GitHub checkout is needed. This is deployment configuration, never a value supplied by a job. `http`, `https`, and `git` schemes are accepted for trusted local mirrors, but a repository clone token is refused unless the scheme is HTTPS so credentials cannot cross plaintext transport. |
| `AGENT_SOURCE_IMPORT_MAX_MIB` | **NEW** | Largest repository checkout a first turn may import into its sandbox, in MiB; default 1024, accepted 1–65536. Read by the runner, never supplied by a job. It counts the working tree **and** the fetched commit's Git objects, and the single-commit pack stores most content a second time (incompressible files almost byte for byte), so a repository whose files total 600 MB can already exceed 1024. Past it the turn fails before the agent starts with `source checkout … exceeds this host's … retained import size limit`. The clone is measured while git writes it and stopped once it passes the limit, so a first turn never fills the runner's disk with a repository it is about to refuse. Sending a follow-up retries the same import and fails the same way, so raise this or shrink the repository. The archive is spooled to an unnamed file on the runner container's disk and streamed to `docker cp`, so raising it costs that much temporary disk per concurrent first turn, not memory; the `docker cp` timeout grows with the archive (two minutes per GiB). Only a chat's first turn moves the whole repository: after a pull request is published, the commit sent back to the workspace carries just what that turn changed. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_TERMINAL_IDLE_TTL_SECONDS` | existing | Reaps one stale interactive PTY after no input/output activity (default 1800 seconds). A retained PTY uses the earlier terminal/chat limit and hands its last activity time to the supervisor, so cleanup does not start a second inactivity window. This setting never stops the chat container directly. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_TERMINAL_HARD_TTL_SECONDS` | existing | Absolute lifetime guard for one PTY (default 8 hours), even if it remains active. This bounds a resource and credential exposure window; it is not a chat lifecycle state. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_TERMINAL_MAX_SESSIONS` | existing | How many interactive terminals the sandbox manager holds open at once across all chats (default 64, accepted 1–4096). Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_RETAINED_DOCKER_DATA_ROOT` | **NEW** | Expected daemon-side `DockerRootDir`. Supported host/local Compose default: `/var/lib/docker`; direct sandbox manager execution only checks it when configured. A mismatch at sandbox manager startup fails retained mode closed. For same-host disk durability, bind `<disk>/docker` onto `/var/lib/docker` and `<disk>/containerd` onto `/var/lib/containerd`, and leave this value at `/var/lib/docker`. A non-default daemon `data-root` or containerd `root` is rejected. Bootstrap verifies the mount with `findmnt` and persists the binds in `/etc/fstab` plus systemd `RequiresMountsFor` drop-ins. See [Retained Docker storage](docs/developer/operations.md#retained-docker-storage-on-a-host). The path is never mounted into sandbox managers or sandboxes; Docker alone manages container layers. This supports stop/start and host reboots. Automatic CloudLab replacement uses a separate stopped-daemon transfer protocol; this setting is not a checkpoint or off-node backup. |
| `AGENT_CONTAINERD_CONTENT_DIR` | **NEW** | Sandbox Manager path of containerd's content store (`<root>/io.containerd.content.v1.content`), mounted read-only. Code and host Compose default `/var/lib/hybridinference/containerd-content`; host Compose binds `AGENT_HOST_CONTAINERD_CONTENT_DIR` (default `/var/lib/containerd/io.containerd.content.v1.content`, the system containerd that Docker uses with its containerd image store) there. A restorable checkpoint then copies only the commit's new layers. When the mount is absent, empty or from another daemon, the checkpoint falls back to streaming the whole image through `docker image save`, which is slower but equivalent. Docker's own managed containerd keeps the store under `/var/lib/docker/containerd/daemon/io.containerd.content.v1.content`. |
| `AGENT_SANDBOX_ARCHIVE_STATE_DIR` | **NEW** | Sandbox Manager-only directory for temporary stopped-container exports and immutable per-request deletion receipts. Code default `/var/lib/hybridinference/broker-archives`; host Compose binds `${AGENT_HOST_STATE_ROOT}/sandbox-archives` there. Keep this directory on durable storage so a sandbox manager replacement can finish a verified deletion after a lost response. It must never be mounted into a sandbox. Exports use mode 0600 within mode 0700 request directories; the full archive contains repository files and native session state, while credential tmpfs and secret-bearing Docker configuration fields are omitted. Archives over 5 GiB are refused and the original retained; at most four requests retain pending archives per sandbox manager. Temporary files are removed after abort or successful deletion, and phase receipts remain. Restorable checkpoints keep their latest receipt per sandbox in `checkpoints/` and spool their exports in `checkpoint-spool/` here. |
| `AGENT_SHARED_RESIDENT_CAPACITY` | **NEW** | Number of credential-isolated member UID/private-tmpfs slots built into each new shared container. Default 8; accepted range 2–128. Fork reserves a free slot before any native mutation. This bounds resident chats in one group, while `AGENT_HOST_SLOTS` separately bounds simultaneously claimed turns on the machine. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_CHAT_IDLE_TIMEOUT_S` | **NEW** | The single product-level inactivity clock for a retained chat group. Defaults to 1800 seconds (30 minutes). Turn/file/Git completion and terminal activity advance the same group deadline; activity tokens prevent a mid-operation stop without stacking another full timeout. When the settled clock expires, the host issues `docker stop`, releases that group's bridge, and retains the same container/writable layer. It never authorizes `docker rm`. Wake rebuilds the group's bridge, host rules first, before `docker start`. `AGENT_TIMEOUT_S` and terminal TTLs are resource watchdogs, not alternate chat lifecycle clocks. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages*. |
| `AGENT_ENROLL_TOKEN` | **NEW** | One-time host enrollment token (task G2). Minted at `POST /admin/agent/runner-hosts/enroll-token`, redeemed once at `POST /v1/agent/host/register`, then replaced by the returned host credential. 15-minute TTL, hashed at rest, and shown exactly once — losing it means issuing another, not looking it up. **The token names the host it may register**: the admin minting it supplies `host`, and registration under any other name is refused without spending the token, so a typo cannot revoke a live machine's credential. The runner-side enrollment client is implemented: it serializes first registration across restarts, writes the host credential mode 0600 at `AGENT_HOST_CREDENTIAL_FILE`, and uses authenticated heartbeats thereafter. |
| `AGENT_HOST_CREDENTIAL_FILE` | **NEW** | Where the host credential is kept between restarts, written mode 0600 before any byte reaches it — this machine runs untrusted repository code next door. The code default is `/var/lib/hybridinference/agent-host-credential`, but the supported host Compose overrides it to `${AGENT_HOST_STATE_ROOT:-/var/lib/hybridinference/agent-host}/credential`, so **on a deployed host the file lives under `AGENT_HOST_STATE_ROOT`** and that is the path the rebuild runbook and any re-enrolment step must name. `AGENT_HOST_CREDENTIAL` in the environment wins over the file, for deployments that inject it another way. |
| `AGENT_DEPLOYMENT_SHA` / `AGENT_SHARED_LIVE_READY_FILE` | **NEW** | Deployment-script-managed rolling fence, not operator-selected topology. The control plane requires the same exact SHA described above. `deploy/host/deploy.sh` supplies it and `sandbox_host.sh` keeps the marker under `AGENT_HOST_STATE_ROOT`. A valid runner keeps advertising the base shared-sandbox/native-fork protocol while the marker is pending so host readiness can inspect it, but it does not poll for work until the exact `shared-sandbox-deployment:<sha>` marker is complete. This runner-side pause also protects the previous API, which does not know the new exact-SHA claim field during a parallel rollout. Do not set either to choose behavior: retained/shared-live is unconditional. |
| `AGENT_HOST_SLOTS` | **NEW** | How many turns this host runs at once until an admin sets a number on the Servers page (`PUT /admin/agent/runner-hosts/<host>/slots`). The runner container runs one service that enrolls, heartbeats and claims; it starts a process per turn as it claims one, and claims only while it is under the limit. A number from the admin page arrives on the heartbeat answer and becomes the limit from then on — nothing is started or stopped to apply it, and turns already running are never interrupted. The heartbeat reports the limit back as `slots_total` and the claim admits against the lower of that and the saved number, so lowering applies immediately and raising applies once the host confirms. Defaults to 3; at least 2, because a fork's sibling runs beside its parent; at most 128, a typo guard rather than a statement about the machine. |
| `AGENT_RUNNER_REPLICAS` | **removed** | No longer read. Concurrency is the limit in `AGENT_HOST_SLOTS` and on the admin page, the Compose stack runs one runner container per host, and `sandbox_host.sh` warns when the variable is still set. |
| `AGENT_RUNNER_VERSION` | **NEW** | Free-text version for the pool listing. Operator-facing only — never a compatibility gate, and a missing value must not stop a host enrolling. |
| `AGENT_HOST_CAPACITY_ENABLED` | **NEW** | Whether a runner samples its Docker host's CPU, memory and retained-container count for the admin Servers page. Defaults to enabled; `0`/`false`/`no`/`off` turns it off, and the server is then shown as not measured rather than as idle. The host Compose stack bind-mounts the host's `/proc/stat`, `/proc/meminfo` and `/proc/sys/kernel/random/boot_id` read-only under `/run/host-metrics` for it — three named files, never all of `/proc` and never `pid: host`, because a container's own `/proc/stat` describes the container and a shared pid namespace would show untrusted sandbox neighbours every process on the machine. Sampling is best effort in both directions: a missing file yields a missing measurement, never a fallback read of an unscoped `/proc`, and an unscoped or malformed report is dropped by the control plane without refusing the heartbeat that carried it. |
| `AGENT_HOST_STORAGE_PROBE` | **NEW** | Which probe measures the filesystem holding the sandbox writable layers, shown as the Servers page's Storage column. `overlay_root` (the default, and what the host Compose stack interpolates) reads the runner's own root filesystem: the runner is an ordinary container under the same daemon and storage driver, so OverlayFS's `statfs` makes `os.statvfs("/")` describe the upper layer's filesystem — the disk the sandboxes write to. `off` stops measuring storage on that host and is the rollback; it needs an `up` to reach replicas that are already running, since a container's environment is fixed when it is created, and the `check` before it clears the verdict so those replicas stop publishing a figure in the meantime. No other spelling is accepted: the host script refuses anything but these two at configuration time, because its vocabulary and the runner's used to differ and a value only one side recognised produced a rollout log reading "storage verified" beside a page reading "switched off". A reading is published only when the deployment has attested the topology in `.storage-probe/verdict.json`: `sandbox_host.sh` compares the container's own numbers against `df` of the **mount root** of the filesystem containing the upper directory, because `df <upperdir>` returns a project quota when `overlay2.size` is configured and would agree with a container reading that quota. Verification failures never block a deployment — the runner reports `unverified` and every other metric keeps working. |
| `AGENT_STORAGE_MIN_AVAILABLE_MIB` | **NEW** | Storage reserve for new sandboxes, in MiB; default 1024, accepted 1–1048576. Before a turn allocates a sandbox, the runner reads fresh available space through the attested storage probe above and declines the untouched turn when less than this remains; existing sandboxes can still resume. Unknown or unverified capacity is never treated as pressure, and the reserve is an admission guard, not a disk quota. Can be set from the admin pages, which win over this value; see *Sandbox settings from the admin pages* and [host runtime settings](docs/developer/runtime-settings.md). |
| `AGENT_LOG_LEVEL` | **NEW** | Process log level, default `INFO`. The control-plane listeners use it during bootstrap and import it once into Admin → Settings; subsequent changes apply to their own logging handler without restarting. Runner and sandbox manager process logs remain host deployment configuration and read this value at startup. |
| `AGENT_LOG_FORMAT` | **NEW** | Process log format, default `text`. The control plane imports it once into Admin → Settings and applies saved text/JSON changes without restarting. Host processes still read it at startup. `json` emits one JSON object per record and includes structured `extra` fields; the same handler also formats uvicorn logs. Text emits the message without those fields. An unknown initial environment value warns and uses text. Existing embedding or test logging handlers retain their configuration. To inspect JSON logs, pipe `docker logs …` to `jq`. |
| `AGENT_OPERATION_PROFILING_ENABLED` | **NEW** | Legacy static-experiment switch. With no explicit MODE, `true` preserves environment-driven collection and still requires a valid RUN. Unset or `false` now defaults to **Admin control with collection Disabled until an administrator enables a run**. Under explicit `MODE=admin` this variable is ignored. No profiling variables are required to use the Admin button. In environment mode each producer (CP, runner, sandbox manager) needs the same static experiment. Read once per process. |
| `AGENT_OPERATION_PROFILING_RUN` | **NEW** | JSON descriptor for the current profiling run: `run_id`, a non-empty `operations` list (this slice implements `attempt.startup`), an absolute UTC `expires_at`, a positive `max_operations`, and an optional `target` naming `host`, `job_id`, `thread_id` or `sandbox_id` — any other target field rejects the whole descriptor, because a filter that can never match would silently measure nothing. A descriptor missing any other required part leaves profiling off and logs one warning instead of refusing to start. `expires_at` becomes a monotonic deadline when first read, so a service restart or a wall-clock jump cannot extend a run, and it is enforced while collecting: past it nothing more is recorded, the record ends at the deadline rather than when someone noticed, and it is emitted as partial (`measurement_complete: false`, `profiling_stopped: "expired"`). `max_operations` is a per-process budget in this slice, not a fleet-wide admission: two replicas can each spend it. A slot is only spent on an operation that was actually selected — targets are matched with every configured field present and equal, so an unknown value is a mismatch, and a rejected candidate returns its slot. `operations` may also name `files.list`, `files.open` and `terminal.open`, which the **control plane** roots; a control plane that is not enabled neither selects nor reports them. |
| `AGENT_OPERATION_PROFILING_SINK` | **NEW** | Optional JSONL path for the finished measurements, which are logged as well. An external benchmark reads this file to aggregate a baseline. It is **not** an outbox and is not required for Postgres/Admin delivery: root producers send the same cached frozen record directly through their bounded delivery path. Log and JSONL publication still use one bounded background exporter, so a slow file or log handler never runs on the measured thread. The runner sends selected context to the sandbox manager in `X-Agent-Operation-Profiling`; file/terminal response metadata returns in JSON or `x-agent-profiling-*` headers. Browser continuation uses `X-Agent-Interaction-Id` (the older measurement header is compatibility-only). |
| `AGENT_OPERATION_PROFILING_MODE` | **NEW** | Optional control-source override: `admin`, `environment`, or `disabled`. **Unset or blank defaults to Admin control**, except that legacy `ENABLED=true` retains environment mode. Admin reads persisted experiment state; no active experiment means no collection. Explicit `admin` ignores old ENABLED/RUN values. `environment` uses only the static experiment; `disabled` forbids collection, including continuation of inherited selections, and makes the Admin switch unavailable. Invalid values also disable collection and warn. Overrides are read once per process; ordinary Enable/Disable does not change them or require a restart. CP and runner sync in the background (about 30 s idle, 5 s active); runner uses the authenticated profiling-policy endpoint and forwards to the sandbox manager. Disable stops new selection, while in-flight work retains its original deadline. Disconnected nodes are bounded by the experiment expiry. See the [default-control design](docs/agents/specs/2026-09-22-measurement-admin-default.zh-CN.md). |
| `AGENT_OPERATION_PROFILING_DEPLOYMENT` | **NEW** | Which deployment's control row this control-plane process commands; defaults to `default`. The singleton control row is keyed by this name, and the name comes from trusted configuration and never from a request: a browser that could name its namespace could see — and stop — another deployment's experiment, because a Stop only has to name a run id. Set it when two deployments share one database (staging and production, or two preview stacks). Read by the control-plane process only; the host agent and the sandbox manager never read it, because they cannot act on a name they do not hold. A missing or malformed value falls back to `default` rather than refusing to boot; a malformed one is logged as a warning naming the refused value, since the fallback shares `default`'s namespace. |

The control-plane, host and local Compose files pass optional profiling overrides
through to their respective producers. An empty MODE reaches the shared parser
and uses the same default as an unset variable: Admin control is available
without setup. Normal schema migrations still apply. The deployment namespace
is only needed when independent deployments share a database.

Collection starts Disabled on a fresh database. The Admin Enable/Disable button
controls the persisted run without editing configuration or restarting services.
There is no per-operation DB read when Disabled, but the existing background
policy sync still runs; this is not zero background I/O. A restart restores any
unexpired active experiment rather than creating one. `config_version` names the
immutable experiment and `revision` orders control changes. There is no renewal
lease. Disable does not rewrite the manifest expiry or discard in-flight records.
`max_operations` stays a per-producer-instance allowance — the Admin API reports
`budget_scope=producer_instance` and does not present it as a deployment-wide
cap. Different runtimes and execution profiles remain separate in Admin
distributions. A 2026-09-18 local rerun measured roughly 30 microseconds
of collector work per synthetic startup; this does not include a real
container, sandbox manager, network, or database.

Two benchmarks answer two different questions, and neither substitutes for the
other. `scripts/bench_startup_profiling_overhead.py` measures the collector's
own cost on a synthetic attempt sequence — no claim, container, supervisor,
agent CLI or network — and its own output says so.
`scripts/bench_operation_overhead.py` drives the real control-plane routes over
a real socket against a real database and, with a container runtime available,
the real supervisor activation that produces each of the four startup paths. It
reports per-operation p50/p95, the delta against the uninstrumented cohort,
queue drop counts, the background cost, and the agreed
`max(10 ms, baseline p95 × 1%)` budget verdict. Read each script's
`what`/`not_what` block before quoting either.

### Physical servers and execution environments

`AGENT_RUNNER_HOST` identifies one enrolled execution environment. When two
stacks on the same physical server talk to one control plane, they need distinct
names, credentials, state roots and sandbox manager URLs. Do not rename or re-enroll an
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

The global sandbox location selector is always visible, including with one
server. It shows Automatic or the exact selected Host identity; grouping by a
physical server never silently chooses a different executor. It applies to all
new, unassigned sandbox groups. Retained groups keep their original Host, and
the page shows their actual distribution. An unavailable saved selection stays
visible with a recovery action and never silently falls back to another Host.
Disabled servers are listed in a collapsed Unavailable servers section, not
counted as choices. Slots are the displayed environment's concurrency limit, not
GPU counts or a sum of old and current deployments. Grouping never changes
credentials, sandbox manager routing, or retained sandbox ownership. Server names are
reported metadata; runtime suffixes are never guessed.

Set `AGENT_PHYSICAL_HOST` before host activation and use the coordinated release
phases in [the rolling-update runbook](docs/developer/operations.md#rolling-deployment-order).
Allow active turns to settle, complete the target host generation, and pass the
control-plane exact-SHA readiness fence before activating the API/web release.
Existing host credentials and retained sandboxes stay in place.

## Sandbox isolation (host agent reads, sandbox inherits)

| Variable | New? | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | existing | Defaults to `kata`. `container` is the explicit trusted-repository downgrade. `process` is a non-claimable test seam and cannot serve retained chats. |
| `AGENT_SANDBOX_RUNTIME` | existing | Unset selects `io.containerd.kata.v2` with backend `kata`, or Docker's default runtime with backend `container`. Set `runc` only for the trusted execution profile. The untrusted profile requires exactly `io.containerd.kata.v2`; gVisor needs a separate execution profile. |
| `AGENT_SANDBOX_IMAGE` | existing | Required sandbox image, with no application default. Use distinct tags for sibling environments, such as `cloud-agent-job:staging` / `:prod`, so one rebuild cannot replace the other's image. This is the job image, distinct from the runner image. |
| `AGENT_SANDBOX_IMAGE_PULL` | **NEW** | `true` makes a source deployment (`sandbox_host.sh`) pull `AGENT_SANDBOX_IMAGE` instead of building it; default `false`. Use it with the image published for the deployed commit, for example `ghcr.io/harvardmadsys/cloud-agent-sandbox:sandbox-<sha>` (an autoscaling deployment template can write `{deployment_sha}`). A pulled image is recorded by its registry digest, which is what lets another host restore this host's chats from their checkpoints; a locally built image is recorded by a local name no other host can fetch, so its groups stay on this host. The extra runtime version menus need a local build and are refused with this set. Artifact deployments always pull. |
| `AGENT_SANDBOX_NAMESPACE` | **NEW**, required | Stable deployment namespace stamped onto retained containers and group bridges so sibling Compose stacks on one Docker daemon cannot discover, stop, or prune each other's chats. It also names this deployment's host nftables table and bridge-interface prefix, so a runner or sandbox manager without one refuses to start rather than sharing a default. The host stack uses its unique Compose project; the complete local stack derives it from `LOCAL_PROJECT_NAME`. |
| `AGENT_SANDBOX_DOCKER` | existing | Docker executable used by the host backend, default `docker`. |
| `AGENT_SANDBOX_HOME` | existing | Sandbox user home, default `/home/agent`. |
| `AGENT_SANDBOX_UID` / `AGENT_SANDBOX_GID` | existing | Sandbox non-root identity, defaults `10001` / `10001`. |
| `AGENT_SANDBOX_EXTRA_ARGS` | existing | Additional Docker arguments parsed with `shlex.split`. Retained chats reject unsafe options; this is trusted deployment configuration, never job input. |
| `AGENT_MEMBER_PROCESS_REAPER_SOURCE` | **NEW (internal)** | Trusted runner-side path to the root-owned `member-process-reaper` helper copied into a retained container created by an older compatible image. The runner image supplies `/usr/local/libexec/member-process-reaper`; operators normally never set this. It is infrastructure compatibility material, not chat data or a host workspace mount. |
| `AGENT_SANDBOX_ALLOW_UNISOLATED` | existing | ⚠️ Isolation escape hatch, and narrower than the row above: it only opens the `process` backend, which is a non-claimable test seam and can never serve a retained chat. Must be unset in any environment reachable by untrusted repositories. |
| `AGENT_EGRESS_CHAT_NETWORK_POOL` | **NEW**, required | This deployment's RFC1918 IPv4 pool; Docker allocates one `/29` from it per active sandbox group bridge (a `/16` holds 8192). No default: it must not overlap another deployment on the same machine — of two whose pools overlap, the host rules refuse the one installed later — nor any network the host already routes (LAN, VPN, private services): preflight reads the host's routes and refuses a pool that shares addresses with one, because Docker takes an explicit subnet as given and a bridge there would draw the host's own traffic. Idle and lost stops remove a group's bridge immediately; sandbox manager reconciliation removes stale interrupted-create bridges after a five-minute grace period so it cannot delete one between creation and sandbox attachment. |
| `AGENT_EGRESS_DNS_SERVER` | **NEW**, required | The one IPv4 resolver sandboxes may use, on TCP and UDP 53. It is the only exception to the protected ranges, so a site resolver on a private address, or on this host, works. Under runc Docker's embedded resolver forwards to it; a Kata guest is given it in `/etc/resolv.conf` by the trusted guest helper. Not loopback: a sandbox cannot reach the host's `127.0.0.53`. |
| `AGENT_EGRESS_PROTECTED_CIDRS` | **NEW** | Comma-separated IPv4 blocks sandboxes must not reach, on top of the built-in host, private, link-local/metadata, CGNAT, multicast and reserved ranges — an institution's services on public addresses, for example. It can only add protection. The control plane reads the same variable when it checks an owner's SSH server, so set it there to the hosts' value. |
| `AGENT_RELAY_NETWORK` | **NEW** | The route-less network the two relays are discovered on; no sandbox joins it. Group lifecycle attaches the relays found here, under their aliases, to each group's bridge. The host stack derives `<COMPOSE_PROJECT>-agent-relays`, so sibling stacks never find each other's relays; set it only to name an existing network. |
| ~~`AGENT_EGRESS_SETUP_TIER`, `AGENT_EGRESS_AGENT_TIER`, `AGENT_EGRESS_ALLOWLIST`, `AGENT_EGRESS_NETWORK_*`, `AGENT_EGRESS_PROXY_*`, `AGENT_EGRESS_STOP_GRACE`, `AGENT_SANDBOX_NETWORK`, `AGENT_SANDBOX_ALLOW_OPEN_NETWORK`~~ | **removed** | Retired with the network tiers and the Squid proxy when sandboxes moved to one network ([design](docs/agents/specs/2026-09-23-agent-public-egress-design.zh-CN.md)). Nothing reads them: there is no tier to select, no proxy to point at and no open-network switch. Delete them from `.env`; setting one changes nothing. |
| `AGENT_LIVE_SESSION_ID` / `AGENT_LIVE_WORKDIR` | **NEW (internal)** | Generated by the runtime adapter for the private OpenCode/Kilo stdio bridge. Operators must not set these. They select the already-authorized native session and fixed `/workspace` path inside one member process; neither is a credential or host path. |
| `AGENT_PLUGIN_PATHS` | **NEW (internal)** | Generated by the host plugin installer for pi, OpenCode, and Kilo. A JSON array of absolute paths to the selected plugins installed in the member's private home; defaults to `[]`. CLI wrappers load these paths explicitly while retaining disabled project plugin discovery. Operators must not set this; each chat's saved selections determine it. |
| `AGENT_ACTION_UID` | **NEW (internal)** | Set by the sandbox manager on a repository environment action it starts in a member's terminal: the scoped UID (31000–31063) the action runs as. Operators must not set it. |
| `AGENT_ENVIRONMENT_SCRIPT` / `AGENT_SCRIPT_STATUS` | **NEW (internal)** | Passed by the sandbox manager to the wrapper that runs one repository environment script (`environment_process.py`): the script, and where to record its exit status. The wrapper removes both from its environment before the script starts. Operators must not set them. |
| `AGENT_SECRET_PATTERN_*` | **NEW (internal)** | Set only for the snapshot secret scan inside the sandbox (`environment_snapshot.py`): base64 patterns derived from the bound environment's Secrets, so a snapshot that adds one is refused without its contents leaving the sandbox. Operators must not set them. |

### Kata runtime qualification generation

The supported host deployment mounts `/var/lib/freeinference/kata-runtime`
read-only at `/run/agent-kata-runtime` in the runner. Its root-owned `epoch.json`
contains `{ "version": 1, "state": "ready", "generation": "<UUID>" }` after a
verified install. `setup_kata_runtime.sh` holds a host-wide installation lock,
publishes `updating` before changing the runtime, links or configuration, and
publishes a new ready generation only after installation and verification
succeed. A failed update remains `updating`; rerun the installer to repair it.
All deployments on that host observe the same generation, including atomic
replacement of the marker. `--check` remains verification-only.

For an existing host, deployment verifies Kata and initializes only a missing
marker through a short-lived, networkless runc container with access to this
directory alone. It takes the same installation lock and never replaces an
existing generation or clears an incomplete update. Missing epoch evidence
requires full probes; malformed or updating evidence fails closed. In-place
runtime/configuration updates must use the supported installer so every runner
invalidates qualification before the first changed file.

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

### Claude inference retry policy

The runtime adapter sets `CLAUDE_CODE_MAX_RETRIES=15` and
`CLAUDE_CODE_RETRY_WATCHDOG=0` for headless and resident Claude Code processes.
These are fixed launch settings, supplied by the runner rather than host
`.env` options. The pinned CLI retries transient inference requests with
exponential backoff and jitter within the current native turn. Disabling the
watchdog keeps the request retry count bounded; `AGENT_TIMEOUT_S` remains the
overall execution limit. Retry progress appears in chat, and exhaustion fails
the turn while preserving its native context. See
[Runtime boundaries](docs/developer/operations.md#runtime-boundaries) for delay limits
and rollout behavior.

### Agent CLI version pins (image build)

The five default versions live in both
`deploy/sandbox/Dockerfile.agent-sandbox` and
`apps/backend/cloud_agent_shared/shared_protocol.py`. They are one reviewed protocol unit:
the image installs and labels them, while every runner and the control plane
use the matching constants to decide whether `shared-sandbox-v1` may be
advertised or claimed. They are therefore source pins, not per-host `.env`
settings. Change both locations together and re-run the agent-loop conformance
suite when intentionally changing a default.

The Dockerfile still verifies every exact version against the installed binary
at build time. A dist-tag (`latest`) or range (`^2.1`) fails the build, so its
labels cannot claim a different binary than the image actually contains.

**Updating an agent without a rebuild.** Claude Code, Codex, OpenCode and pi
can also be updated from the admin Settings page (_Agent versions_ → _Update
to …_). Every host then downloads that exact npm release into a local image
beside the sandbox image, checks it with one real turn through this release's
driver against a model on loopback, and reports progress on its heartbeat;
once every online host holds it, new chats use it and existing chats keep
their version. _Roll back_ returns new chats to the version they used before.
Kilo is not offered: it runs a source build carrying a reviewed isolation
patch. The defaults above stay what a host without a selection runs. See the
[design](docs/agents/specs/2026-10-15-agent-cli-updates-design.zh-CN.md).

| Variable | New? | Notes |
|---|---|---|
| `AGENT_RUNTIME_REGISTRY_URL` | **NEW** | Host side of the control-plane row above: the npm registry a host downloads updated agents from. Default `https://registry.npmjs.org`. |
| `AGENT_RUNTIME_DOWNLOAD_NETWORK` | **NEW** | The Docker network the download step runs on. Default `bridge`. Only npm runs there, with install scripts disabled; the packages' own scripts and the check run later with no network. Name another network only when the host's default bridge cannot reach the registry. |

Each runtime takes a `*_EXTRA_VERSIONS` deployment setting — comma-separated,
blank by default — naming **additional** versions the same image carries beside
the reviewed default. That list is the *menu*: build time decides what the image
holds, and an admin picks which item new sandboxes run under _Agent versions_
on the admin Settings page (`agent_runner_policy.runtime_versions`). Blank leaves an image holding exactly
its defaults, which is where every deployment starts.

Every added version must be exact and its event stream must be compatible with
`runtimes.py`.

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

Interpret a passing conformance run within these limits:

- **`claude-code` only.** This is the one runtime with a stream-conformance
  suite; the other four have contract fixtures and their adapters' unit tests,
  and no equivalent gate. Adding a version for them is currently a review
  obligation rather than a test.
- **It proves the parser, not the option.** A pass means the version's event
  shape is intelligible to our normalizer. It does not exercise the deployed
  image, the sandbox, or the dropdown.

Verify stream compatibility before offering a version to users.

| Variable | New? | Notes |
|---|---|---|
| `CLAUDE_CODE_EXTRA_VERSIONS` | **NEW** | Extra `@anthropic-ai/claude-code` versions. ~300 MB each. |
| `CODEX_EXTRA_VERSIONS` | **NEW** | Extra `@openai/codex` versions. |
| `PI_EXTRA_VERSIONS` | **NEW** | Extra `@mariozechner/pi-coding-agent` versions. |
| `OPENCODE_EXTRA_VERSIONS` | **NEW** | Extra `opencode-ai` versions. |
| `KILO_EXTRA_VERSIONS` | **NEW** | Extra `@kilocode/cli` versions. |

## Web (`apps/frontend/`)

Frontend configuration is public. Build-time routing settings enter the
downloaded bundle and require a rebuild. Appearance settings are saved through
Admin → Settings and read by the web server for each request. Deployment
timestamps and release identity remain runtime environment metadata. These
public values may not contain secrets.

### Build-time: the image variant

A `NEXT_PUBLIC_*` variable is substituted into the bundle during `next build`.
Each one must therefore be an `ARG` in `deploy/control-plane/Dockerfile.web`
*and* a key under the `web` service's `build.args`: a row here without both is a
variable an operator can set everywhere and change nothing. Every row names its
compose argument, and `test_web_build_arguments_are_wired` fails on a row that
has none. Changing one is a rebuild.

The base path and API prefix must match the reverse proxy. The supported
same-origin layouts use `AGENT_WEB_API_URL=<AGENT_WEB_BASE_PATH>/api`: `/api`
for the standalone edge, or `/agents/api` under `/agents`. The build rejects a
nonempty relative API value that disagrees with the base path. Absolute URLs
and an empty value are accepted for operator-defined routing; empty means API
requests start directly at `/v1/...`. The pairing tests run the actual Next.js
configuration.

| Variable | New? | Compose build argument | Notes |
|---|---|---|---|
| `NEXT_PUBLIC_BASE_PATH` | | `AGENT_WEB_BASE_PATH` | Prefix the app is mounted under. `/agents` serves it inside the gateway's own hostname — same origin, so the session cookie needs no CORS and no DNS record is involved, and a rollback is a reverse-proxy edit rather than a DNS change waiting out a TTL. Empty for a deployment that owns its hostname. The redirect URI the gateway allowlists is no longer derived from this: sign-in returns to the control plane's `AGENT_SESSION_REDIRECT_URI`, not to a page in this app. This value still has to match `AGENT_WEB_BASE_URL`, which is where the control plane sends the browser afterwards. |
| `NEXT_PUBLIC_AGENT_API_URL` | | `AGENT_WEB_API_URL` | Browser API base. For the shipped standalone edge, use `/api`; under a mount prefix use `<AGENT_WEB_BASE_PATH>/api`, for example `/agents/api`. Empty sends `/v1/...` requests directly to the browser origin and requires routing that exposes those paths. Cross-origin URLs need control-plane CORS with credentials because authentication uses a session cookie. |
| `NEXT_PUBLIC_BUILD_SHA` | | `BUILD_SHA` | Optional build identity, baked in by the deploy script. |
| `NEXT_PUBLIC_BUILD_TIMESTAMP` | | `BUILD_TIMESTAMP` | Optional build identity. |

### Runtime: the deployment's identity

The main control plane imports the first eight appearance variables below when
it initializes the settings document. Set them in its deployment environment
before the first start, then change them through Admin → Settings. The web
server reads the saved public appearance fields on every request through
`GET /v1/agent/site-config`; the same built image serves any deployment. A web
server without `AGENT_CONTROL_PLANE_URL` uses its environment for standalone
development. Deployment time remains web runtime metadata.

| Variable | New? | Notes |
|---|---|---|
| `AGENT_WEB_APP_NAME` | **MOVED** | Product name in the header and the document title. Empty shows `Cloud Agent`. |
| `AGENT_WEB_ORG_NAME` | **MOVED** | Optional organisation name beside the product name. Empty hides it. |
| `AGENT_WEB_ORG_URL` | **MOVED** | Link target for `AGENT_WEB_ORG_NAME`. |
| `AGENT_WEB_STATUS_URL` | **MOVED** | Status page, opened in a new tab from the header. Empty hides the link — an empty `href` resolves to the current page, so a Status link that is rendered anyway reopens the page the user is standing on. |
| `AGENT_WEB_DOCS_URL` | **NEW** | User documentation for this deployment, opened in a new tab from Help in the account menu and from the My Settings page. Empty hides the link. Point it at the distribution's own user guide; this repository's developer documentation is for operators and contributors. |
| `AGENT_WEB_CONTACT_URL` | **NEW** | Where users reach the deployment's operators: an http(s) support page, or `mailto:` and a single address (no `?subject=` or other fields). Shown as Contact us under Help in the account menu. Empty hides the link. |
| `AGENT_WEB_GATEWAY_URL` | **MOVED** | `${GATEWAY_BASE_URL}`. The gateway's origin, for the links this app does not host itself — nothing to do with sign-in. It replaced `NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL`, which the browser needed only while it built the authorize URL and redeemed the code itself; both are the control plane's job now (`GATEWAY_AUTHORIZE_URL`). A path in this value is ignored; empty hides the links. |
| `AGENT_WEB_FEATURE_RAG` | **MOVED** | `true` shows the gateway's Docs Assistant link. `false` or empty hides it; any other value is refused. The page belongs to the gateway, not to this service. |
| `AGENT_WEB_DEPLOYED_AT` | **NEW** | ISO timestamp with a timezone for the deployment footer. The control-plane deploy script sets the activation time in UTC and records it in `.deployed-at` after successful health checks; deploying the same release again preserves it. When starting Compose manually, supply the actual deployment time. Empty shows `Not available`. |

The web service also receives the existing `AGENT_DEPLOYMENT_SHA` for the footer's
version: the first seven commit characters are visible, with the full commit in
the tooltip. Both values are resolved per request, independently of the image's
build date.

Saved URLs and booleans are validated before an admin update is committed.
The standalone environment fallback validates them when rendering.
The gateway's `/site-config` endpoint is outside the contracts Cloud Agent
consumes; public saved appearance comes from this service's own endpoint.

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
  The sandbox reaches its two relays, the resolver in `AGENT_EGRESS_DNS_SERVER`
  and public IPv4 on TCP 80/443 — the same during setup and the agent run.
  Tool calls remain confined to the MCP-only listener. Inference keeps the
  existing gateway endpoint (`AGENT_TUNNEL_REMOTE_PORT` defaults to 8080);
  a separate inference listener or credential-substitution relay is not a
  deployment prerequisite. Public replay of a leaked inference grant is an
  [accepted risk](docs/agents/specs/2026-09-23-agent-public-egress-design.zh-CN.md),
  subject to its owner's existing model permissions, quota and grant lifecycle.
  If a deployment fronts either relay through a tunnel, the port allocation
  is overlay detail.
- **Sandbox image registry**: choose where hosts pull `AGENT_SANDBOX_IMAGE` before deploying them.

## Adding a variable

1. Add a row here, in the right section, marked **NEW**.
2. Note who provisions it and whether it differs per environment.
3. If it is a secret, say where it is stored — never the value itself.
4. The PR checklist enforces this; a new variable without a row is a review stop.

### Repository environments

Reusable repository environments are available by default. There is no feature switch or initial environment-variable import. A legacy `repository_environments_enabled=false` setting or `AGENT_REPOSITORY_ENVIRONMENTS_ENABLED=false` value is ignored. Settings, task selection and the workspace Environment tab are always offered. `/config` always reports environment limits.

A task with no configured variables or scripts uses the base environment without environment preparation. Configured workspaces still require an environment-capable runner and sandbox manager; owner authorization, encryption and execution isolation remain enforced.
