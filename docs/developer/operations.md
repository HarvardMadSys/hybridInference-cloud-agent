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

This is a task-based runbook; you do not need to read it from beginning to end.
Use it to provision hosts, deploy Cloud Agent, protect retained workspaces, and
diagnose job or network failures. The control plane owns the job API, database,
publisher, MCP relay, and admin settings. The gateway provides inference and
identity over HTTP.

Start with the task you need:

| Task | Go to |
| --- | --- |
| Prepare a host and its credentials | [Prerequisites](#before-a-job-can-run-for-real), [credentials](#2-credentials) |
| Start or inspect the host service | [Running self-hosted](#running-self-hosted) |
| Enable isolation for untrusted repositories | [Provisioning Kata](#provisioning-kata-on-a-runner-host) |
| Change application behavior, integrations, or appearance | [Admin Settings](#admin-settings) |
| Open PostgreSQL administration | [pgAdmin](#pgadmin) |
| Browse and filter database rows | [Database browser](#database-browser) |
| Change concurrency, sandbox settings, or placement | [How many runners](#how-many-runners), [which machine runs the jobs](#which-machine-runs-the-jobs) |
| Add a host on another machine | [Tunnels and sandbox manager routing](#running-runners-on-another-machine) |
| Deploy or roll back a release | [Rolling deployment order](#rolling-deployment-order), [deployment reliability](deployment-reliability.md) |
| Preserve an old database or apply a maintenance migration | [Old-chain conversion](#preserving-an-old-chain-database), [database rebuild](#rebuilding-the-agent-database), [maintenance migrations](#applying-migrations-that-are-not-backwards-compatible) |
| Protect workspaces and monitor disk use | [Sandbox archives](#sandbox-archives), [retained Docker storage](#retained-docker-storage-on-a-host), [storage measurement](#how-full-is-the-sandbox-storage) |
| Diagnose connectivity | [The sandbox network](#the-sandbox-network), [refusal messages](#when-something-is-refused) |
| Configure or troubleshoot tools and repository access | [MCP servers](#mcp-servers), [repository entitlements](#who-may-run-a-job-against-which-repository) |
| Answer a waiting agent question | [Native agent questions](#native-agent-questions) |
| Investigate a task | [Recorded events and audit](#reading-a-tasks-recorded-events) |
| Review test coverage and historical evidence | [Runtime verification](#runtime-verification), [verified](#verified), [not verified](#not-verified) |

[ENVIRONMENT.md](../../ENVIRONMENT.md) documents configuration, and the
[compute-resource runbook](operations-compute.md) covers administrator-managed
SSH and CloudLab hosts. For pre-drain checks, migration regression, failed peers,
and queue alerts, use [deployment reliability](deployment-reliability.md).
The historical measurements below describe the recorded runs, not the current
state of your deployment. Check the deployment's own records for current
readiness, and replace every `<...>` placeholder before running a command.

## Admin Settings

Use **Admin → Settings** to manage application and integration configuration,
publication, web appearance, operational intervals, upload and skill limits,
task-title generation, sandbox/workspace limits, and sandbox archives. These
settings are stored in the Agent database.

The application API uses `GET /admin/agent/settings` and
`PUT /admin/agent/settings`. Send a partial body such as
`{"settings": {"max_attempts": 3}}`; omitted keys keep their current values.
Writes use the existing administrator guard and appear in the Audit log.
Credential fields show only whether a value is configured. To change one, enter
and save a replacement. **Reset** restores the built-in default. If a required
field is missing or an optional integration is only partly configured, the
**Administrator setup required** prompt links to the fields that still need
attention.

Admin Settings is operator access, and `agent.admin` should be granted only to
people trusted to operate the deployment. It edits the session and host
signing keys and the Gateway public-key URL, so an administrator can sign in as
any user, and the GitHub API URL that the App's, the publication token's and
users' own GitHub credentials are sent to. Saved credentials stay write-only,
and the Gateway dispatch token is bound to the Gateway API URL's origin: moving
that URL to another origin is refused while the token is kept. Reset the token,
save the URL, then enter the token again.

For the first deployment of this release, run `alembic upgrade head` through
`0070_memory_cutover`. Existing hosts begin without a deployment drain
owner or release receipt, and 0051 backfills each one with a non-null fence
epoch. 0066 builds an index on `agent_jobs` concurrently: on a long job history
it takes a while without blocking the running API, and an interrupted run can
simply be repeated. 0067 only replaces a trigger function. Once this release
runs, a shared workspace that a failed snapshot left `dirty` publishes again:
a snapshot already pending behind the failure is captured, otherwise the
group's next settled turn is (see
[Resume, Fork, and publication](#resume-fork-and-publication)). 0069 adds the
durable-memory table; 0070 moves the memory switches and saved preferences
onto it, leaving the retired memory tables unread until a later release drops
them. Rebuild the sandbox image for explicit plugin
loading.
Then start the main control-plane listener with its existing complete
environment. It imports registered environment settings once,
while the MCP-only listener waits for initialization and then reads the same
settings. Newly migrated runtime identity, service keys, sandbox manager credentials,
and policy fields are imported only when absent, under the same atomic
preservation rule.

Later deployments keep the saved admin values, and resetting a field never
restores an old `.env` value. Listeners synchronize saved changes every five
seconds. The web server reads saved public appearance fields on every request,
with a 400 ms budget for the response and its body. A timeout, unavailable
backend, or invalid response uses the last valid public appearance read by that
web process for the same control-plane URL; before its first successful read,
it uses neutral display defaults. Successful reads show saved edits on the next
page request. This fallback contains no session or access decisions and does
not survive a web-process restart.
Apply changes to subsequent operations as described beside each setting.

The import of registered environment settings refuses a
`GATEWAY_GRANT_DISPATCH_TOKEN`, `AGENT_ONCALL_DISPATCH_TOKEN`,
`AGENT_CONTROL_TOKEN_SECRET` or `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` shorter
than 32 bytes: the listener stops with an error naming the variable before
anything is written, so rolling back is safe. Check their lengths before the
first deployment of this release. Replacing one has its usual cost: the
dispatch token changes on both sides, a new control key needs hosts
re-enrolled, and a new encryption key orphans saved user connections. Values
already saved in Admin Settings keep working. The same import refuses an
`AGENT_GITHUB_APP_INSTALL_URL` that is not an `https://github.com` URL and an
`AGENT_GITLAB_OAUTH_REDIRECT_URI` that is not HTTPS (or HTTP on localhost),
which their connect flows could never use.

A release can narrow what a setting accepts. A value saved earlier that it
refuses does not stop the listeners: each logs
`saved setting not applied as stored: …`, naming the key but never the value,
once per settings revision, and uses the built-in default in its place. The
exception is a signing key, encryption key or service token shorter than a
newly required minimum, which stays in use until it is replaced. The editor
shows the value in effect; save a valid value or **Reset** to clear the
warning. Undecryptable or unencrypted stored credentials still stop the
listener.

Automatic session archiving after a verified GitHub PR merge is enabled by
default. Configure it under **Publication**; see
[automatic PR archiving](automatic-pr-archiving.md) for its session selection,
retry ledger, verification, and rollout steps.

For a new installation, provision the database, a stable
`AGENT_SETTINGS_ENCRYPTION_KEY` of at least 32 bytes and a separate random
`ADMIN_TOKEN`, plus the infrastructure/build fields in the minimal
`deploy/control-plane/.env.example`. Bootstrap the setup UI before enrolling
execution hosts or running the normal deployment readiness gate:

```bash
AGENT_DEPLOYMENT_SHA=$(git rev-parse HEAD) docker compose -p cloudagent \
  --env-file .env -f deploy/control-plane/docker-compose.yml \
  up -d --build control-plane mcp-relay web
```

Open `<web base path>/setup` and enter the recovery token. This route is outside
the sign-in guard. It uses the ordinary administrator API and audit trail;
unauthenticated visitors cannot read or change settings. Reloading or locking
the page clears its in-memory token. Complete Gateway sign-in, execution and
user credential fields, then enroll hosts and run the usual deployment command.
Optional integrations need setup only when their configuration has been started.
The shared-live deployment gate continues to require qualified host readiness.

Existing installations can retain the environment `AGENT_SESSION_SECRET`
fallback for the settings root. Before removing that legacy environment entry,
set `AGENT_SETTINGS_ENCRYPTION_KEY` to the **same original value**. Both listeners must receive the
same effective key. Back it up with the database. Before changing that key,
re-encrypt stored settings from the old key to the new one and coordinate the
listener rollout; replacing the environment value alone makes existing
ciphertext unreadable. The saved session signing key can be changed separately
from this stable root. The saved user/compute encryption keys continue to protect
their separate credential stores; the API rejects replacing or resetting an
existing encryption key without a separate credential re-encryption procedure.

Database access, the settings encryption root, recovery authentication,
host provisioning, networking/listeners, actual host isolation,
build routing, mounted files, and trusted compute
deployment profiles remain deployment configuration. These bootstrap names
appear on the page with hidden values. See [the environment inventory](../../ENVIRONMENT.md#settings-and-bootstrap-configuration).

## Sandbox archive storage

This section configures the archive required by the explicit **Archive to S3
and delete** control. Automatic lifecycle archives use the separate policy
under **Admin → Settings → Sandbox**, described in [Sandbox archives](#sandbox-archives).

Configure **Admin → Settings → Application → Sandbox archives** before enabling stopped
sandbox deletion. Set the S3 bucket and, optionally, its region, HTTPS
S3-compatible endpoint and object prefix. The matching
`AGENT_SANDBOX_ARCHIVE_S3_*` environment variables import once when these fields
are first introduced; later environment changes do not override saved settings.
An empty bucket leaves deletion unavailable.

The bucket field also accepts an S3 URI in the browser. Saving
`s3://freeinference/cloud-agent/sandbox` sets the bucket to `freeinference` and
the prefix to `cloud-agent/sandbox` in one update. A bucket-root URI clears the
prefix; entering only a bucket name leaves the prefix unchanged. Direct API
updates and initial environment imports require the bucket name and prefix in
their separate settings.

Provision the main control plane's AWS SDK credential chain, preferably with an
IAM workload role. Its archive role needs `s3:PutObject` and `s3:GetObject` on the
configured archive prefix; `HeadObject` uses `s3:GetObject`. If SSE-KMS is applied
by the bucket, also grant the required KMS permissions, including
`kms:GenerateDataKey` and `kms:Decrypt` so checksum-enabled HEAD can verify the
object. The application does not need `s3:DeleteObject` for archive storage.
Provision these manual-deletion credentials only on the main control plane;
the owning sandbox manager receives a request-specific presigned upload URL. The base Compose
does not forward AWS credential variables. For environment credentials, create
a private override restricted to the main service, for example:

```yaml
services:
  control-plane:
    environment:
      AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID:?Set the archive access key}
      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY:?Set the archive secret key}
      AWS_SESSION_TOKEN: ${AWS_SESSION_TOKEN:-}
```

Keep the override outside the checkout and set its absolute path as
`AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE` in the deployment root's private
`.env`. The deployer validates that it is a readable regular file and appends
it after release-owned overlays for every stack operation in both source and
artifact modes. Leaving the setting empty uses only the reviewed deployment
files. For shared profiles, mount the credentials and configuration files read-only
in this service and set `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE` and
`AWS_PROFILE` to their container paths and selected profile. Use the saved
archive region setting or provision `AWS_DEFAULT_REGION` in the same override.
Apply these manual-deletion credential mounts and variables only to the main
control plane. The automatic lifecycle archive policy separately uses the
trusted sandbox manager's workload identity; neither path provides AWS
credentials to sandbox containers, the web application or the MCP relay.

The host receives a 15-minute presigned PUT for exactly one request object, with
a fixed byte length, full-object SHA-256 checksum and `If-None-Match: *`. The
single PUT archive limit is **5 GiB**; an oversized archive keeps its container.
A retry may encounter HTTP 412 if the upload already succeeded. The control
plane independently reads `HeadObject` with `ChecksumMode=ENABLED` and requires
the exact service-verified SHA-256 and byte length before deletion can proceed.
ETags, supplied metadata, missing checksums and composite checksums cannot
substitute for that verification. S3-compatible storage must implement these
conditional PUT and checksum semantics.

Use a private bucket, block public access, and configure encryption, retention
and recovery access for these archives. Conditional PUT prevents the issued
capability from overwriting an existing request object; the archive role and
other principals must preserve this immutability. Bucket policies can require
conditional writes, and versioning/Object Lock provide stronger protection
against later administrative changes. Do not expire an archive while deletion
or recovery may still depend on it. Persisted receipts include its original
S3 URI, SHA-256, byte length and object version when available, so changing
settings does not redirect verification of an existing deletion request.

## Database browser

Open **Admin → Database** to inspect the Agent database directly in the dashboard.
Select a table, inspect its columns and types, add filters, choose a sort column
and page size, then select **Run query**. Previous and next controls page through
the results. Filters are combined with AND; the Contains filter matches a literal
case-insensitive substring. The browser uses the control plane's existing
database connection and works without enabling pgAdmin.

The built-in browser exposes reviewed metadata columns from Agent tables. Stored
credentials, raw user content, and opaque configuration documents are omitted.
It supports read-only queries with parameterized filters, stable ordering,
bounded results, and a statement timeout. Each request checks current
`agent.admin` permission. Query outcomes appear under **Audit log → Database**;
the trail records query metadata without filter values or returned data.

The API is `GET /admin/agent/database/tables` for the visible schema and
`POST /admin/agent/database/query` for filtered rows. Database writes and arbitrary
SQL remain available through the separately configured pgAdmin service below.

## pgAdmin

Open **Admin → Database → Open pgAdmin** to manage PostgreSQL. The page checks
the private service before offering the link. The existing same-origin API
proxy carries pgAdmin pages, assets, forms, downloads, and SQL requests, so the
gateway and standalone edge need no new public route or port. Every request
requires current `agent.admin` permission. pgAdmin retains its own login and
CSRF checks; Agent cookies and bearer credentials are never sent upstream.

pgAdmin's pages run on the Agent's own origin, where the administrator's Agent
session authorizes every admin route. The proxy therefore adds a second
`Content-Security-Policy` to each pgAdmin response, enforced alongside
pgAdmin's own: pgAdmin may fetch, submit forms, open frames and start workers
only under the public pgAdmin path. It also sends
`Cross-Origin-Opener-Policy: same-origin`, so a pgAdmin page holds no handle on
another Agent page it opens, and the proxy's own error answers are sandboxed.
This narrows what a script injected into pgAdmin could do with the session; it
is not an origin boundary. pgAdmin still shares the origin's browser storage,
and an error page that an edge in front of the control plane serves under the
pgAdmin path without these headers is outside the policy. Only a separate
origin, with its own hostname, TLS and edge route and a proxy credential other
than the Agent session cookie, would remove the shared session; this release
does not provide one. Until then, keep the pinned pgAdmin image and its default
security headers (its own policy refuses inline scripts without its per-request
nonce), enable the profile only where it is used, and grant `agent.admin` only
to operators.

Enable the optional service in the control-plane deployment's `.env`:

```dotenv
COMPOSE_PROFILES=pgadmin
AGENT_PGADMIN_BOOTSTRAP_URL=http://pgadmin:5050
AGENT_PGADMIN_BOOTSTRAP_DIR=/var/lib/cloud-agent/pgadmin-bootstrap
```

Retain any existing profiles, for example `COMPOSE_PROFILES=edge,pgadmin`.
Run the normal control-plane deployment workflow. An existing stack can start
the service and apply its new control-plane environment with:

```bash
docker compose --env-file .env -f <selected-compose-file> up -d pgadmin control-plane
```

Use the release-generated Compose file for an artifact deployment. Its bundled
server definitions are resolved from the selected recipe. First-time setup also
saves the connection in **Admin → Settings → Database tools** if no private
pgAdmin URL has been saved. The default public URL is the saved web base URL
followed by `/api/admin/agent/pgadmin/`; set `AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL`
before setup if the API has a different public prefix. Later changes use
**Private pgAdmin URL** and **Public pgAdmin proxy URL** in Admin Settings.
Existing `AGENT_PGADMIN_URL` and
`AGENT_PGADMIN_PUBLIC_URL` values are imported once along with other runtime settings.

**Private pgAdmin URL** selects a pgAdmin service the deployment names; it
cannot name any other address. The bundled `http://pgadmin:5050` is always
allowed, and so are the scheme, host and port of `AGENT_PGADMIN_URL` and
`AGENT_PGADMIN_BOOTSTRAP_URL` in the control plane's environment. Any other
saved URL makes the dashboard, the launch link and the proxy answer 503 without
contacting it: the proxy forwards every method, path and body and returns the
reply, so an address an administrator could choose freely would reach a cloud
metadata endpoint or any other private service the control plane can reach.
To run pgAdmin elsewhere, set its address as `AGENT_PGADMIN_URL` in the
control plane's environment, restart the control plane, and select the same
address in **Private pgAdmin URL**. Keep the variable set after it has been
imported.

Open **Admin → Database** and choose an administrator email and a password of
at least 12 characters. Only a current Agent administrator can complete setup.
The service waits for that first setup; the page then checks readiness and
offers **Open pgAdmin**. No administrator account or default password is shipped.
Concurrent setup attempts cannot replace the first credentials. The private
`agent-pgadmin-bootstrap` volume holds the initial credentials; preserve it and
`agent-pgadmin` during deployments. pgAdmin mounts the bootstrap volume read-only.

Sign in with the pgAdmin email and password you chose. The initial server
definition points to `postgres:5432`, database/user `cloudagent`. Enter the
deployment's PostgreSQL password when connecting. If the database name or user
differs, supply a custom `AGENT_PGADMIN_SERVERS_FILE` before initialization, or
edit the connection in pgAdmin. Database passwords are not preloaded or exposed
in the dashboard. Saved accounts and connections persist in the separate
`agent-pgadmin` volume. Setup does not reset an existing account, including on a
container restart. Manage later accounts and password changes in pgAdmin.

For the HTTP-only local stack, use `AGENT_PGADMIN_COOKIE_SECURE=False` alongside
the existing local Agent-cookie setting. Keep the default `True` for HTTPS.
If the dashboard reports unavailable, inspect `docker compose logs pgadmin`
and its health state. Before setup, a waiting container is healthy and logs
`Waiting for first-time setup in Admin -> Database.` After setup, unreadable
bootstrap storage or unavailable PostgreSQL prevent startup. Setup records
`agent.database.pgadmin.setup`. A setup that cannot write the bootstrap volume
is recorded as failed; fix the volume and set up again from the same page.
Opening pgAdmin records `agent.database.pgadmin.open` in the Agent audit log,
both for the launch link and for the first request of a new pgAdmin session at
any URL. The Agent audit log does not record SQL: pgAdmin connects as the
database owner, which can change any table, the audit log included, so treat
pgAdmin access as full database access. The proxy adds `frame-ancestors
'self'`, `nosniff` and `X-Frame-Options: SAMEORIGIN` to pgAdmin's responses;
pgAdmin's own policy still allows its inline scripts. Resetting **Private
pgAdmin URL** in Admin Settings disables
access after the listeners synchronize; removing the profile and stopping
pgAdmin preserves its volume.

See [pgAdmin's container documentation](https://www.pgadmin.org/docs/pgadmin4/9.18/container_deployment.html)
for account and server initialization behavior.

## What a job does

```text
POST /v1/agent/jobs           owner queues work
  → enrolled host claims       exact shared-live capability attestation
    → runner creates/starts     one retained Docker group and its writable layer
      → native Agent CLI        one live process/session per member chat
      → normalized events       streamed to the owner over SSE
      → model calls             billed through the gateway into api_logs
      → optional direct push    member's GH_TOKEN; skips the publisher's checks
    → quiescent group snapshot  immutable diff for the trusted publisher
  → publisher validates         .github/ block, secret scan, path escape, size
    → agent/<origin-chat-id>     one stable branch per workspace group
      → draft PR                merges follow the repository's GitHub rules
```

The agent can also push and open pull requests itself, and **Watch PR and
merge** asks it to merge once the PR satisfies its required checks and reviews.
Branch protection and other GitHub rules on the repository decide what may
merge without a person.

Keep these credential boundaries intact:

- **No long-lived repository credential enters the sandbox.** The host
  captures a quiescent group diff; the control-plane publisher holds the
  GitHub token it publishes with. The runner holds
  a clone credential, but it is read-only, scoped to the one repository, and
  deliberately never recorded in `.git/config`: the checkout adds no remote,
  the credential travels in git's environment rather than its argv, and the
  runner refuses to start the agent if it finds the token anywhere under
  `.git/`.
- **The only repository credential in the sandbox is short-lived.** When the
  GitHub App is configured, the owning member receives `GH_TOKEN`: an App
  installation token limited to `contents: write` and `pull_requests: write`
  on the job's repository, expiring after about one hour. Pushes made with it
  skip the publisher's patch gate. See [credentials](#2-credentials).
- **The sandbox also has two narrow, non-interchangeable credentials.** The
  gateway-issued `agr` grant buys inference as the chat owner: it names the
  allowed model, is scoped to one member residency, and is renewed only while
  that exact host/member generation keeps heartbeating. Spend lands against
  the owner's gateway daily quota. This service separately mints an
  attempt-fenced `mcp` token that reaches only the MCP relay and only the
  servers authorized by the conversation's current bindings. **There is no
  per-job spend cap**; inference uses the owner's gateway quota. Neither credential can
  write the event log, upload a replacement patch, or move the job to a
  terminal state; the trusted runner holds the token that can.

Resident inference grants use a mint identity that includes the member's durable
residency generation. Retrying a mint for that generation recovers the same
grant; replacing a process or retrying Fork with a new generation mints a fresh
grant rather than recovering a revoked predecessor. Worker replacement revokes
the previous grant best-effort, matching Fork cleanup; a gateway revocation
outage does not prevent issuing the replacement.

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

### Draft prompt rewriting

`POST /v1/agent/prompts/rewrite` prepares a draft without creating a job or
starting a sandbox. It requires an owner session and current `agent.use`
permission. The request contains `prompt`, `model`, optional `clarify`, and
optional `answers` (`question` / `answer` pairs). With `clarify: true` and no
answers it returns two to four clarification questions; otherwise it returns a
complete prompt for the owner to review before submission. Answer-bearing
requests always produce a rewrite rather than another question round.

Each call resolves the selected model against the owner's current catalog and
uses a fresh 120-second grant scoped to that model. Its opaque operation ID
starts with `prompt-rewrite-`, so inference remains subject to the owner's
Gateway quota while staying separate from job usage. The grant is revoked
after the request and never renewed. The call has a 90-second total deadline;
drafts and rewritten prompts are limited to 32,000 characters, with up to four
clarification answers. Invalid, truncated, or warmup model responses are
reported as errors without replacing the draft.

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

### Native agent questions

Claude Code `AskUserQuestion`, Codex user-input requests, and OpenCode/Kilo
question requests open a question dialog while the turn waits. Choose an
option or enter a custom answer when offered, then submit to continue the
same native tool call. **Answer later** dismisses the dialog without answering;
**Review question** reopens it. **Skip** explicitly declines the request.

Claude Code `ExitPlanMode` uses the same dialog to present its plan for
approval. Select **Approve plan** and submit to let Claude exit plan mode and
continue implementation. **Skip** declines the plan approval and asks the
agent to keep planning; **Answer later** leaves the native approval request
waiting until the owner answers it. Claude handles entering plan mode and
reading or updating its plan file within the native session.

The page reads pending questions from the resident agent, so reloading the
conversation rediscovers unanswered requests. An answer names the job,
attempt, and request it was read from. The owner API and host both fence that
identity; stopped, completed, or replaced attempts cannot take a stale answer,
and question answers are never queued as follow-up turns.

Deploy the sandbox_manager/runner and rebuilt sandbox image together. The HTTP bridge
helper pin is now `3`; helper `2` cannot execute native question replies and
does not satisfy the current turn-time attestation. Before starting a
replacement OpenCode/Kilo process, the host installs its trusted bridge into
the retained container, including conversations without MCP connections. This
updates the helper while preserving the workspace and native session state.

## Who may run a job against which repository

A repository needs at least one entitlement: the user's GitHub connection or
the deployment allowlist. Job creation fails closed when neither applies.

**User connection.** The user authorizes the GitHub App through the configured
frontend callback. The control plane exchanges the code for a user token,
records the GitHub account in `agent_github_accounts`, and stores only the
installations GitHub reports in `agent_repo_grants`. Reconnecting removes grants
for installations the user can no longer reach.

The composer, job creation, claims, workspace reads, and publishing all check
repositories **as the user**, through `/user/installations/{id}/repositories`.
Only repositories the user can **push** to qualify, because the result is a
branch and pull request. The response is cached for one minute per user. Do not
replace this check with an installation-token repository listing: GitHub can
report an organization's installation to a collaborator with access to only
one of its repositories, and the installation token could expose the rest.

The user token stays in the control plane, encrypted with
`AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`. Expiring App user tokens last eight
hours and have a six-month, single-use refresh token. Refresh begins five
minutes before expiry under a row lease so concurrent callers cannot spend the
same refresh token. A connection without a stored token, including a legacy
connection or one GitHub has refused, grants no access. The composer asks the
user to **Connect GitHub** again; GitHub skips consent for an existing
authorization.

Connecting an account and installing the App are separate steps. If the App is
not installed on the user's own account, the connect flow offers GitHub's
installation page even when an organization's installation already grants
access. The user can select an account and repositories or choose **Not now**.
A connected user with no repositories goes to that page instead of repeating
OAuth. **Configure repositories** in Integrations and **Configure repository
access** in the composer open the same page.

**Deployment allowlist.** `AGENT_REPO_ALLOWLIST` supports single-tenant use
against the operator's repositories. It is not per user: every user admitted to
Cloud Agent may have the platform clone and publish to every listed repository
with the deployment's GitHub credentials, so list only repositories all of them
may work on. When the App's OAuth credentials and
`AGENT_GITHUB_APP_INSTALL_URL` are configured, the composer still requires a
connection before repository selection, including allowlisted repositories.
The create endpoint independently honors the allowlist.

**Disconnecting.** **Disconnect GitHub** in the Integrations Manage menu
deletes the account, encrypted token, and installation grants and asks GitHub
to withdraw the authorization. Uninstalling the App on GitHub revokes the
platform's installation access.

## Sandbox inventory, resource usage, and controls

The admin console's **Sandboxes** tab initially lists every group recorded as
running, including containers retained between turns. Use the state filter to
include stopped, starting, stopping, or failed groups. Fork members share one
container row. The inventory refreshes every 30 seconds and follows every page
of `GET /admin/agent/sandboxes`, which requires admin access. The default API
filter remains `state=running`; `state=all` includes every lifecycle state.

Search matches session titles, repositories, owners, hosts, agent versions, and
container identifiers across the complete loaded inventory. Combine it with
host and activity filters. Column headings toggle sorting; **Sort by** also
offers host, state, agent, and creation time. CPU, memory, session counts, and
timestamps sort by their raw values, with unavailable values last. Uncheck
**Auto-refresh** to pause both inventory and expanded-detail polling; manual
refresh and measurement-age indicators remain active.

Open a sandbox's details to inspect its member sessions, host, runtime,
container identity, and lifecycle. Member details include session links and
activity states, without exposing native session identifiers, credentials,
prompts, or raw container diagnostics.

**Stop idle sandbox** requests a stop of the shared container, preserving its
workspace and native sessions for a later turn. It applies to every member of
the group. The API checks the reviewed container, generation, and last activity
before accepting the request, and refuses a busy group, pending snapshot or
publication work, or a group with open terminals or stale terminal observations.
The request is recorded in the admin audit log. The host performs its own idle
check before stopping the container.
When the terminal report is zero, missing, or older than 90 seconds, opening or
refreshing details queries the owning sandbox manager for that group's census
directly. This inspects only the selected container and counts terminals across
all its members; unrelated containers and host-wide reconciliation do not delay
the check. Full inventory reconciliation remains on the host heartbeat path.
Stop requests also verify terminal activity before closing admission: older
sandbox managers may report only the origin session's terminals even when another member
is active. This also supports runners that omit terminal counts from their
heartbeats. The report must match the reviewed container and generation; an
unavailable sandbox manager or unknown activity keeps stopping disabled and shows the
required recovery step.
Resource-stat failures do not discard an otherwise valid terminal report.
The page shows the pending request until a host observation confirms the stop;
an offline host cannot complete it. New turns wait while the stop is pending.
This action does not delete the container or reclaim its retained disk space.

**Archive to S3 and delete** removes a stopped or dead, idle container after its
complete retained filesystem has been uploaded and verified in S3. Configure
the bucket, region, optional S3 endpoint, and object prefix under **Admin →
Settings → Application → Sandbox archives**. [S3 storage setup](#sandbox-archive-storage)
describes permissions, credential provisioning and compatible storage requirements.

Deletion blocks new turns, Fork, and workspace operations while the owning
sandbox manager prepares the archive. The archive contains `manifest.json` and
`container-rootfs.tar`, including workspace files and retained native session
files. Archives larger than 5 GiB are refused before deletion. An immutable S3
object must match both SHA-256 and byte length, and its receipt must commit to
the database before the host may remove the exact reviewed stopped container.
Upload failures retain the container. Once an archive is verified, uncertain
deletion acknowledgements keep admission blocked and retry the same request.
The sandbox manager stores its deletion receipts on its own persistent volume so an
acknowledgement can survive a sandbox manager restart.

**Delete without archive** removes the same exact stopped or dead, idle container without
uploading workspace files or native session history. It does not require an S3
bucket. The host still refuses a running, busy, or changed container, and the
control plane marks native resume lost only after the host confirms removal.
Chat history and the lifecycle audit remain. There is no S3 object to restore
from. An archive deletion that is already pending or verified is left unchanged;
wait for it to finish or fail before choosing this action.

**Sandboxes of deleted chats are removed automatically.** When an owner
permanently deletes the last chat in a sandbox, the deletion worker asks the
owning host to stop it, then queues its deletion through the same two paths:
archived to S3 first when a bucket is configured, discarded otherwise. Only
sandboxes on online hosts are considered, a sweep runs every minute, and its
events and audit rows name `user-deletion-sweeper` as the actor with source
`system`. Database records of the deleted chats remain. A sandbox that another
chat still uses is never touched. The first sweep after an upgrade also picks
up every older sandbox whose chats were all deleted before, so expect a batch
of stops and deletions; configure the archive bucket first if those sandboxes
should be kept in S3.

**Verify stopped and force delete** recovers a sandbox still recorded as running
only when the owning sandbox manager confirms that the exact reviewed container is
stopped or dead, and idle. It does not stop a running container or bypass unavailable
host proof. If verification fails, the error identifies missing host registration,
an absent or refused advertised URL, a missing or rejected sandbox manager credential,
a connection failure, a timeout, or unavailable sandbox status. Check the
indicated configuration or sandbox manager readiness before retrying. For a connection
failure, verify the [reverse sandbox manager route](#the-way-back-the-control-plane-reaching-this-hosts-sandbox-manager).
Control-plane logs record `agent_sandbox_delete_verification_failed` with the
sandbox, owning host, status code, and failure type without raw sandbox manager errors.

**Remove stale entry** is available when the owning host has no heartbeat or
has not reported for more than 90 seconds. It removes the entry from the default
admin inventory without contacting the sandbox manager, so a retired or unreachable host
does not leave an entry that can never be cleared. The server rechecks host
freshness and the reviewed identity, and refuses active turns, workspace
operations, snapshots, or a pending deletion. The action is audited and
reversible through **Include removed** and **Restore to inventory**. It preserves
the recorded lifecycle state, container identity, native state, and chat history,
without deleting any retained container or files, creating an S3 archive, or
reclaiming host disk. A matching running host
observation or new admitted work makes the entry visible again. Restore the
sandbox manager connection to complete S3 archival and physical deletion.

The details show progress, archive location, checksum, size, and any failure.
Chat history, memberships, execution records, and audit receipts remain in the
database. Deleted native sessions cannot resume; this operation does not add
automatic recovery from S3. **Remove from inventory** is a separate reversible
visibility control for stopped sandboxes. Use **Include removed sandboxes**
with **All states**, then **Restore to inventory**, to inspect retained entries.
Resuming a sandbox removed only from inventory makes it visible again.

For batch operations, select individual inventory rows or **Select all visible
sandboxes**, choose an action, then **Review selected sandboxes**. Selection
applies only to the current state, search, host, and activity filters; hidden
rows are excluded. The review reads current details for each selected sandbox
and lists any blockers before a single confirmation applies the action to
eligible entries. Stop, archive-and-delete, deletion without an archive,
inventory removal, and inventory restoration use the same checks as individual
controls. Up to four requests run at once, and each result is reported
separately. A stop or deletion result means the request was accepted; host
completion is still asynchronous. Failed or skipped entries remain selected
for a new explicit review. Expired reviews and changed containers are refused,
without automatically retrying a new container identity. No batch feature flag
or additional migration is required.

CPU is measured in occupied cores over the interval between Docker samples;
memory is current usage with inactive file cache removed. Network and block
I/O show cumulative container counters and interval rates. All measurements
cover the whole shared container. The details also show process and terminal
counts, session capacity, active operations, snapshot state, and sandbox age.
Sandbox age starts at group creation and does not measure container uptime.

The sandbox manager samples Docker in a bounded background worker, then sends
cached observations with the host heartbeat. Each sample retains its original
age through repeated heartbeats. Readings older than 90 seconds are marked
stale and excluded from the fleet CPU and memory totals; those totals show
their measured coverage. Counters Docker or Kata does not report remain
unavailable. A current host heartbeat alone does not imply current resource
measurements.

Sandbox controls require migration `0037_admin_sandbox_stop` and matching
control-plane and host releases. Apply `alembic upgrade head` before starting
the updated API. Inventory removal additionally requires
`0044_admin_sandbox_archive`, and S3-backed container deletion requires
`0045_admin_sandbox_deletion` plus the matching sandbox manager release.
The append-only admin trajectory requires `0057_admin_sandbox_trajectory`.
Resource persistence requires migration
`0034_sandbox_resource_usage` and a host running the updated sandbox manager.
Older hosts still appear in the inventory with resource usage unavailable.
Only the latest observation is stored; this view does not provide resource
history.

## Reading a task's recorded events

The admin console shows tool-call measurements in Resources and model records
in LLM requests. To inspect raw runtime events for support or abuse
investigations, use the two admin routes below. See the amendment in
[the admin observability spec](../agents/specs/2026-09-10-admin-observability-v1.md).

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

Both routes require admin authorization. Owners read their own events through
the session routes, which do not require an admin content-read audit.

**Do not read `agent_job_events.payload` directly.** A `psql` query returns the
same content with no audit record, no size bound and no column allowlist, which
is the situation the content route exists to prevent. If reading events one at
a time through the API is too awkward for a real investigation, that is a
reason to improve the tooling, not a reason to go around the audit.

**Reading the trail.** _Admin → Audit log_ (`GET /admin/agent/audit-log`)
lists what administrators did, newest first: settings and agent-version
changes, server actions (pause, remove, placement, adding or releasing a
server), measurement runs, and the content reads above. It filters by
`category` (`settings`, `servers`, `measurements`, `content`) and pages with
`before`, the oldest `id` of the previous page. The Servers page's polling
reads are not recorded; rows they left before that changed stay in
`agent_admin_audit_log` and are never listed. The page names an administrator
by user id only, because identity tokens carry no display name.

## MCP servers

MCP calls pass through the control plane. The sandbox holds a scoped relay
token; upstream credentials stay outside it:

```text
sandbox ──[attempt-fenced mcp token]──▶ MCP-only relay ──[owner credential]──▶ MCP server
```

### Listeners and connections

The MCP listener is a separate FastAPI app on port 8001. It exposes only the
MCP tool surface; job, host, session, admin, and inference APIs stay elsewhere. The sandbox authenticates to
`/v1/agent/mcp` over its second fixed relay, the MCP tunnel attached to its
group bridge. The upstream address and the owner's credential stay in the
control plane.

**Keep the tool relay off the public main API.** The public edge forwards
`/api/*` to port 8000, where no tool route is mounted. A leaked `mcp` token
therefore cannot be replayed through that API. Browser connection management,
OAuth callbacks, and approvals share `/v1/agent/mcp` and must remain on 8000.
Do not block the whole prefix at the edge. Point the sandbox MCP tunnel at the
MCP-only listener on 8001.

Users add connections through `/v1/agent/mcp`: a remote URL, an imported
`mcpServers` snippet, or a stdio launch specification. `agent_mcp_connections`
stores the configuration and credential; credentials are encrypted with
`AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` under an MCP-specific domain separator.
Discovery stores a capability snapshot. Templates prefill the form; they do
not define a deployment-wide registry or server catalogue.

MCP OAuth uses `AGENT_MCP_PUBLIC_BASE_URL` for the public API callback and
`AGENT_WEB_BASE_URL` for the browser's return to `/integrations`. For a shared
hostname deployment these can be `https://example.com/agents/api` and
`https://example.com/agents` respectively. The page reports the authorization
result, verifies a stored credential before showing success, and directs the
user to Test to discover the connection's tools.

The relay, connection API, discovery path and all five coding-agent adapters
are implemented and tested for remote MCP. The remaining stdio and optional
protocol capabilities are described below and in the
[conformance record](../agents/reviews/2026-09-11-per-user-mcp-v1-conformance.md).

### Authorization and routing

The relay enforces these rules:

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
  and waits for the owner's decision in the conversation's Tools panel or the
  account's [Approvals inbox](../user/mcp-approvals.md). Approval
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
- **Two endpoints share one authorization function.**
  `/v1/agent/mcp/<alias>` passes one connection's protocol through to the CLI.
  `/v1/agent/mcp` is the merged host: it answers `initialize`, lists the job's
  connections as one catalogue, and routes each invocation to its connection.
  All five adapters receive only this merged endpoint, in one entry named
  `session`. A fixed name and identity fit runtime allowlists; adding one entry
  per connection would duplicate the merged catalogue. The per-connection route
  remains available to clients needing unmodified protocol passthrough. A legacy
  SSE server cannot initialize through a pipe, so the adapters use the merged
  host instead.

Tools and prompts use `<alias>__<name>` on the merged surface. Routing looks up
that name in the catalogue; it never splits on `__`, because aliases can contain
that separator. Ambiguous or long names receive a stable, bounded suffix.
Resources use `mcp-resource://<connection-id>/<original-uri>` so equal upstream
URIs remain distinct. Reading strips only that prefix and preserves the original
URI and its percent encoding. Resources and prompts have their own permission
scopes and pass through the same `decide` function as tools. An approval in the
Tools panel releases the held invocation under the same policy on either
endpoint.

An unresolved connection is logged and skipped in merged listings so other
connections remain usable. Its per-connection route still refuses the broken
connection explicitly.

The merged host also serves the platform's own tools, which no connection
provides. `web_search` is listed when a search provider is configured; the
memory tools `memory_save`, `memory_update` and `memory_delete` are listed for
every task. Both are named in the claim by non-secret markers
(`:platform:web-search`, `:platform:memory`), so every MCP-capable runtime loads
the merged endpoint even in a conversation with no connections. A memory tool
writes for the owner and task the fenced token names, never for ones its
arguments name, and only to that task's repository or the owner's user scope.

### End-to-end coverage

`tests/external/test_agent_mcp_end_to_end.py` crosses a real listener, migrated
Postgres database, pinned pi 0.73.1 CLI, and shipped bridge. Its model and
upstream MCP server are threaded HTTP stubs; this is transport and authorization
coverage, not a live-provider acceptance result.

The recorded run seeded one connection with a static credential and discovery
snapshot and a job with a live attempt fence. The model saw
`session__github__get_issue`. The upstream received `server/discover`,
`initialize`, and `tools/call`, each with the connection credential instead of
the sandbox token. The tool arrived as `get_issue` with unchanged model
arguments, and its text result returned to the model. The test also verifies
fallback to a handshake when the era probe returns `-32601`; the namespaced tool
name never reaches the upstream.

The test runs in seconds but needs Postgres, npm, and network access to install
the pinned CLI. It skips when those prerequisites are absent and belongs to the
external tier. Run it deliberately after changes to the merged endpoint, host,
or bridge; it crosses seams the per-layer suites do not.

### Connector configuration

`/v1/agent/mcp/connectors` registers a user's machine, returns its registration
secret once, and supports rename and revocation. The secret is hashed at rest.
It also binds or unbinds a `user-connector` connection. Remote connections cannot
be bound because the platform reaches those servers directly.

Binding changes the execution target and both authorization versions. Revoking
a connector detaches all its connections in the same transaction. No connector
runtime ships yet: views report `runtime: "unavailable"`, and a bound connection
is refused at use. Managed-worker connections likewise require an available
worker. Configuration support does not establish execution support.

### Deployment and rollback

Apply the schema before replacing the API, resolve legacy bindings, then deploy
matching runner and sandbox artifacts.

**Schema first.** `0008_generic_mcp_connections` drops
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

**Resolve legacy bindings.** The revision maps a legacy name only where the thread's
owner has exactly one connection for that provider, and writes everything else
into `agent_mcp_legacy_bindings` with `no_connection` or `ambiguous_connection`.
Review those rows as the list of sessions requiring manual tool selection.
They are retained until reviewed; neither an ambiguous selection nor a missing
connection is silently discarded or assigned to a different connection.

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

MCP and connectors remain independent of the gateway inference path. Credential
rotation uses the existing `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`; domain
separation prevents source-control and MCP ciphertext from being read as each
other.

### Runtime boundaries

Claude Code retries transient inference failures within the native request,
including HTTP 429 and 5xx responses. Both headless and resident launches set
`CLAUDE_CODE_MAX_RETRIES=15` and `CLAUDE_CODE_RETRY_WATCHDOG=0`: up to fifteen
retries after the initial request, with the pinned CLI's exponential delay
starting at 500 ms, capped at 32 seconds, and adding up to 25% jitter. The CLI
honors integer `Retry-After` values; its ordinary mode stops if the required
delay exceeds 60 seconds. The runner's execution timeout remains the overall
turn limit, and owner cancellation still interrupts the query during backoff.
Retry notifications appear in the chat with the attempt and wait time.

Retries reuse the pending inference request, preserving completed tool calls,
the native session and the single platform prompt. Exhaustion remains a failed
native turn with retained context; the runner does not send the prompt again.
Ordinary permanent client errors such as HTTP 400 and 403 are not retried;
HTTP 401 retains the CLI's native credential-refresh recovery. Deploy the
updated runner to apply the launch settings; existing resident CLI processes
pick them up when restarted or restored.

An adapter declares MCP support only after both behaviors are checked against
the pinned binary: it directs the CLI to the platform relay and prevents every
other MCP configuration source from adding servers. Runtime family alone does
not establish that boundary.

| Runtime | Pin | How the relay reaches it | Why the repository cannot add servers |
| --- | --- | --- | --- |
| `claude-code` | 2.1.220 | `--mcp-config` with the platform's servers | `--strict-mcp-config`, verified against the CLI: without it a committed `.mcp.json` is loaded alongside ours with no approval step |
| `opencode` | 1.18.9 | the reviewed `opencode-freeinference` wrapper merges the relay endpoints into the `OPENCODE_CONFIG` it writes | the wrapper exports `OPENCODE_DISABLE_PROJECT_CONFIG`, which covers both `opencode.json` and `.opencode/` |
| `kilo` | 7.4.17 | the reviewed wrapper configures the authenticated merged relay for run and serve; the live bridge verifies connection before reporting ready | the pinned build patches the legacy MCP loader to honor `KILO_DISABLE_PROJECT_CONFIG`, covering `.kilo/mcp.json` and `.kilocode/mcp.json` alongside the regular project configuration |
| `codex` | 0.145.0 | exec and app-server share the merged relay URL and token environment reference; app-server verifies MCP startup | trusted guest-root bootstrap atomically installs the deployment's exact relay URL as the sole server identity in `/etc/codex/requirements.toml` before launch; repository and user configuration cannot expand that system allowlist. After `AGENT_SANDBOX_MCP_URL` changes, a retained container's next Codex launch replaces that one-relay policy with the new URL, unless another Codex member of the group is still running; any other policy shape is refused, never replaced |
| `pi` | 0.73.1 | the reviewed `pi-mcp-bridge.ts` shipped at `/usr/local/libexec/`, loaded with an explicit `-e` | `--no-extensions` disables every other extension source — the project's `.pi/extensions/`, the global directory, and anything a settings file names — so the explicit path is the only way in. Verified against the pinned binary: a repository's own extension does not load, the relay's tools are registered as `<alias>__<tool>`, and a call reaches the upstream |

A job naming connections on a runtime that has not declared them is refused at
creation rather than run with the tools silently missing.

Codex's system policy is installed from the trusted deployment address without
a per-deployment image build.

**Use the runtime's credential-reference syntax.** Claude Code expands
`${AGENT_MCP_TOKEN}`. The OpenCode family substitutes `{env:AGENT_MCP_TOKEN}`
in the raw configuration before parsing it. The wrong syntax sends a literal
placeholder as the bearer token, causing 401 responses. Both forms were checked
against the pinned binaries. `apps/backend/cloud_agent_host/runtimes.py` chooses
the address; the adjacent CLI adapter in `deploy/sandbox/` writes the reference.
The token travels in the environment, never in argv or the configuration file.

### What `--strict-mcp-config` is holding shut

Always pass `--strict-mcp-config` alongside `--mcp-config`, including an empty
platform configuration. Strict mode makes that configuration the only source
of MCP servers.

The measurement recorded for hybridInference#1129 used the job's exact CLI
configuration: headless `-p`, `bypassPermissions`, and a checkout as the working
directory. A directory containing

```json
{
  "mcpServers": {
    "evil_repo_server": { "command": "/bin/echo", "args": ["pwned"] }
  }
}
```

produces these two `system/init` lines, differing only in the flag:

| invocation | MCP servers reported by the CLI |
| --- | --- |
| `--mcp-config <ours>` | `evil_repo_server`, `github` |
| `--mcp-config <ours> --strict-mcp-config` | `github` |

The repository server loads without a prompt or approval step under that
permission mode. Keep the flag unconditional, including for jobs with no MCP
bindings.

## Before a job can run for real

Complete host provisioning, network checks, enrollment, and credentials before
enabling job claims. Use this checklist for each deployment.

### 0. The sandbox network needs its host rules, a resolver and both relays

Every sandbox group runs on one routable bridge of its own — setup, the agent
and each Fork sibling alike — and this deployment's host nftables tables
decide what it reaches: public IPv4 on TCP 80/443, the one resolver in
`AGENT_EGRESS_DNS_SERVER`, and the group's two relays — the gateway tunnel for
inference and the MCP tunnel for tools — and nothing else. There is no mode to
choose and no setting that opens it further; changing what a sandbox can reach
is a design change. [The sandbox network](#the-sandbox-network) below has the
whole rule set, how to read it on a host, and what each refusal means.

A host therefore needs, before it can run a job:

- `AGENT_EGRESS_CHAT_NETWORK_POOL` and `AGENT_EGRESS_DNS_SERVER`. Neither has
  a default: the pool must be this deployment's own, and the resolver is the
  only DNS a sandbox can use.
- Both relays running on the relay network, `AGENT_RELAY_NETWORK`, which the
  host compose derives as `<COMPOSE_PROJECT>-agent-relays`. It is declared
  `internal: true` and no sandbox group ever joins it: lifecycle code finds the
  two tunnels there by alias and attaches them, under the same aliases, to each
  group's bridge. If either alias has no running container there, or more than
  one, the group network is refused before the Agent CLI starts.
- A kernel with nftables, bridge family included. Every runner's preflight
  installs this deployment's tables and reads them back before it claims
  anything.
- Docker 28 or newer, client and daemon. Each relay joins a group bridge with
  a gateway priority of -1: a container on several networks takes its default
  route from the highest priority and then by network name, and
  `freeinference-chat-net-*` sorts before `<COMPOSE_PROJECT>_internal`, so at
  the default priority the first group a relay joined would become its way
  out — and its SSH session would meet the sandbox rules and die for every
  group sharing it. Preflight refuses an older Docker, and a relay found on a
  bridge without the priority is attached again with it.

Checks run at four points:

1. `sandbox_host.sh prepare` validates network settings with the candidate
   runner image's parser before draining any runner.
2. `sandbox_host.sh check` sends requests from containers on the relay network.
   It requires HTTP 200 from inference `/health` and MCP `/healthz` before
   publishing the exact-SHA ready marker. The runner's lower-level inference
   probe accepts any HTTP response as reachability; that weaker probe cannot
   release a deployment.
3. Each runner's preflight installs and reads back the host nftables tables.
4. Each group cold start checks inside the guest, as the sandbox user, for a
   default route, the configured resolver, and both relay TCP ports. Failure
   stops the sandbox, preserves its workspace, and fails the turn.

DNS resolution alone does not establish relay health: a tunnel can retain its
name while its upstream is down.

For a runner host that is not the gateway's, see
[Running runners on another machine](#running-runners-on-another-machine): the
two relays are SSH tunnels to the gateway and to the MCP-only listener.

Let Compose create the relay network. A network created by hand under the same
name is refused because its labels do not match. The
`freeinference-chat-net-*` networks are different: lifecycle code creates them
through the Docker API, deriving their names and labels from the sandbox group
and generation and their host interface names from this deployment's prefix.
It allocates one `/29` from `AGENT_EGRESS_CHAT_NETWORK_POOL` and lets Docker
arbitrate concurrent claims. A group's bridge is disconnected and deleted on
idle or lost stop while Docker keeps the container writable layer, and wake
creates it again. Sandbox Manager reconciliation also removes labelled bridges left by an
interrupted create after a five-minute grace period. The grace prevents status
reconciliation from deleting a bridge between creation and sandbox attachment
while still reclaiming bridge interfaces and IPAM.

### 1. Only enrolled sandbox hosts run jobs

Jobs run on enrolled hosts. The deleted `agent-job-runner` Actions workflow
used an unisolated `process` backend and shared dispatcher credential; it is
not an execution or fallback path in this service.

Deploy at least one host through `deploy/host/deploy.sh`, enroll it with the
control plane, and let its runner service poll `/v1/agent/worker/claim`. A
runner cannot claim until its exact replica attests the retained protocol,
container image/runtime pins, sandbox manager configuration, execution profile, and
host capacity.

Standalone, the host stack needs more than an enrolment credential. Start from
`deploy/host/.env.example`: `COMPOSE_PROJECT`, `AGENT_SANDBOX_IMAGE`,
`AGENT_EGRESS_CHAT_NETWORK_POOL` and `AGENT_EGRESS_DNS_SERVER` are required
(`docker-compose.sandbox.yml` interpolates each with `:?`, so an unset value
stops the render), alongside `AGENT_SANDBOX_MANAGER_TOKEN`, the three tunnel
destinations with their own keys and known-hosts files, and the sandbox manager
reverse-forward trio for the separate control-plane and host Compose stacks.

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

**Sandbox manager credential** — generate one
`AGENT_SANDBOX_MANAGER_TOKEN` per environment and set the same value on the
control plane and every host sandbox_manager. It authorizes file, terminal, Git, and
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
container and defaults to that mounted path in
`deploy/control-plane/docker-compose.yml`, so the Compose deployment sets only
`AGENT_GITHUB_APP_PRIVATE_KEY_FILE`. Overriding the path variable without
mounting anything there produces a service that starts with GitHub
unavailable — see below. Put the file on the host, owned by the service user
and `chmod 600`. The control-plane image does not carry the deployment's
`config/`, `distributions/`, or `var/data/` directories.

**Whether GitHub is configured is decided by the saved GitHub App ID**, imported
from `AGENT_GITHUB_APP_ID` at the first settings initialization. The
key path always has a default in this stack, and `/dev/null` is mounted there
when no App exists, so the path says nothing. With the App ID set, a key that
cannot be loaded — missing, empty, unreadable by the service user, or not a
valid key — does not stop the control plane (issue #47):

- Integrations and the task composer say GitHub is temporarily unavailable.
  Creating a job, a runner's claim, or a branch lookup answers `503
  source_control_unavailable` with `Retry-After: 30`; queued jobs, pending
  publishes and goal wakeups wait without spending any retries.
- The log carries one `ERROR` event, `source_control_load_failed`, with a
  `reason` (`key_file_unreadable`, `key_missing`, `key_invalid`, `unexpected`)
  and, for a file, its path and the OS error. It repeats only if the reason
  changes. If jobs are queued and none can start, the existing
  `queue_not_starting` alert fires as well.
- The service reads the key again every 30 seconds. Fixing the file in place —
  its owner, mode or content — recovers the same process within a minute and
  logs `source_control_load_recovered`. Change the App ID and inline credentials
  through Admin → Settings. Changing the host path of the key bind mount still
  needs the container recreated.

Alternatively save an inline PEM in the Settings page; it is encrypted in the
database and takes precedence over the mounted key. The legacy
`AGENT_GITHUB_APP_PRIVATE_KEY` environment value is accepted for the one-time
import in deployments that pass it through. The mounted file remains available
when the saved inline key is empty.

For checkout and publication, the App needs `contents: write` and
`pull_requests: write`. To enable **Create repository** in the workspace and
new-task pickers, also configure the GitHub App's repository permission
**Repository creation: write**. GitHub also accepts
**Administration: Read and write**, but Repository creation grants only the
creation operations this feature needs. See GitHub's
[repository creation permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps#repository-permissions-for-repository-creation).
Existing installations may need their owners to approve the updated
permissions on GitHub. Without that permission,
repository creation reports that the App cannot create repositories; existing
tasks continue to use their existing permissions. The App does not need
`workflows`, so a patch touching `.github/` cannot be pushed even if the gate
were bypassed. Revocation is uninstalling the App.

Creation calls GitHub's `POST /user/repos` with the connected user's encrypted,
refreshable GitHub App user token, kept in the control plane. It creates only
in that user's personal account, defaults to private, and initializes a README.
No long-lived credential enters a sandbox. Creation does not add a local
repository grant or bypass the deployment allowlist: if the App installation
covers only selected repositories, the owner grants access to the new one
through **Configure repository access** and refreshes the picker. A transient
access-check failure after creation is handled as a created repository awaiting
access, rather than another creation attempt.

For the _connect_ flow (a user authorizing their own repositories) the App
also needs its OAuth half, with the callback pointing at this deployment's
frontend:

```bash
AGENT_GITHUB_APP_CLIENT_ID=Iv1.xxxxxxxx
AGENT_GITHUB_APP_CLIENT_SECRET=...
AGENT_GITHUB_APP_INSTALL_URL=https://github.com/apps/<app-slug>/installations/new
# App setting "Callback URL": <agent web origin>/connected?provider=github
#   staging:    https://agents-staging.freeinference.org/connected?provider=github
#   production: https://agents.freeinference.org/connected?provider=github
```

Set the callback to the **agent web's own origin**, where the callback page and
session cookie live. For a web build with a base path, use
`<origin><basePath>/connected?provider=github`. The authorize link sends no
`redirect_uri`, so GitHub uses the App's first Callback URL. Update that setting
whenever the web origin moves.

The old `<gateway>/agents/connected` callback is historical. Staging moved to
its own hostname on 2026-09-09, production on 2026-09-11, and the gateway
retired `/agents` to a 404. The callbacks were corrected on 2026-09-26; copying
the old gateway URL breaks the connect flow.

The composer and the Integrations page derive the Connect link from
`AGENT_GITHUB_APP_CLIENT_ID` and GitHub's web OAuth endpoint. The install URL
is offered after connecting whenever the App is not on the user's own account.
Keeping those two URLs separate is required for reconnects: GitHub sends an
already-installed App's `/installations/new` link to its settings page without
invoking the OAuth callback.

**Configure repositories** uses the installation URL; GitHub selects the user
or organization settings page. Set `AGENT_GITHUB_APP_INSTALL_URL` to
`/apps/<slug>/installations/new`. Pointing it at the OAuth endpoint leaves a
user with no installation looping through authorization.

The same App also supplies the runner's clone credential, and the two are
_not_ the same token. An installation token inherits every permission the App
holds unless it asks for less, so the clone token is requested as
`contents: read` on the single repository being worked on. Checkout does not
inherit the publisher's broader authority.

The claim separately mints a single-repository installation token with
`contents: write` and `pull_requests: write`, injected as `GH_TOKEN` in the
owning sandbox member's Agent CLI environment. `GH_REPO` selects the job's
repository and `GH_HOST` selects `github.com`; `gh` authenticates as the App
installation, rather than as the user's account. The token is not written to
Git or GitHub CLI configuration, shared workspace files, or the image. Setup
scripts and standalone terminal sessions do not receive it. Git uses the
`gh auth git-credential` helper through invocation-only environment settings,
so `git push` and `gh pr create` can authenticate without an interactive login.
The host clone credential stays read-only; user account/refresh tokens and
the App's private key remain outside the sandbox.

Direct sandbox pushes bypass the trusted publisher's patch gate, including
its workflow-file and secret checks. Repository branch protection and GitHub
rules still apply. The App installation must grant Contents and Pull requests
write access; token issuance failures return the claim to the queue.

Each claim obtains a live token from the App's expiry-aware cache. When that
token changes, the member resumes its native session in a replacement process
so subsequent commands inherit the new credential. Installation tokens expire
after about one hour; there is no in-turn renewal, so `gh` calls in a longer
turn may require a new turn to obtain a fresh token.

With no App configured the claim returns no clone token and the runner clones
anonymously, which is enough for a public repository. A private repository
needs the App.

A static `AGENT_GITHUB_TOKEN` still works as a fallback for the trusted
publisher, but it is a long-lived credential someone has to create and
rotate; prefer the App. Sandbox GitHub authentication requires the App and
never falls back to this static token.
With neither, the publisher loop idles and says so — jobs still run and still
produce patches, they just never become PRs.

## Running self-hosted

Administrators can also attach an SSH-accessible server or provision a CloudLab
experiment from Servers, then select the ready Host as the global location for
new sandboxes. The [compute-resource runbook](operations-compute.md) covers
operator profiles, credentials, automatic renewal and guarded resource release.
Existing retained sandboxes remain on their original Host when this selection
changes; neither Host Remove nor Forget destroys the underlying machine.

Run the host agent as a **standing service** so queued jobs can be claimed after
crashes and reboots. Configure enrollment and the shared sandbox manager token in `.env`.
The example below includes the shared-kernel setting for trusted internal
repositories. For untrusted repositories, use the default `kata` backend and
complete [Kata provisioning](#provisioning-kata-on-a-runner-host).

```bash
AGENT_ENROLL_TOKEN="<single-use token for this host>"
AGENT_SANDBOX_MANAGER_TOKEN="<same environment value as the control plane>"
AGENT_SANDBOX_BACKEND=container  # explicit trusted-repository downgrade
```

The `AGENT_SANDBOX_BACKEND=container` line is a **deliberate downgrade**: sandbox
groups share the host kernel. Before enabling Kata for new groups, provision
the host and verify the guest runtime and relay paths with the checks below.
Existing retained runc containers need the separate migration treatment
described there.

Compose uses `restart: unless-stopped`; enable the host's Docker daemon to carry
the service across reboots. The host lifecycle commands are:

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

Use the standalone host stack even on the gateway's machine, with tunnel
destinations pointing to its local listeners. The old co-located
`agent_runner.sh` overlay did not move; [MIGRATION.md](../../MIGRATION.md) records
that decision. `preflight` checks Docker, retained-session configuration, host
isolation, and the configured image. `up` repeats those checks against the image
it deploys.

### Host configuration reference

The script validates the required environment before starting and surfaces
the runner's own preflight verdict (gateway reachable, retained image/runtime
usable, Docker data-root correct, relay network present and route-less, host
network rules installed and read back) instead of leaving it in a detached
log. Equivalent by hand:

```bash
# Put a tested alternate version in the corresponding *_EXTRA_VERSIONS build
# argument, then select it in Admin. Default versions are shared protocol pins
# and change in source together with SHARED_SANDBOX_RUNTIME_PINS.
docker build -f deploy/sandbox/Dockerfile.agent-sandbox \
             --build-arg CLAUDE_CODE_EXTRA_VERSIONS="<exact-version>" \
             -t freeinference-cloud-agent-job:latest .

AGENT_SANDBOX_IMAGE=freeinference-cloud-agent-job:latest \
AGENT_HOST_SLOTS=4 \
  docker compose -f deploy/host/docker-compose.sandbox.yml \
               up -d
```

`AGENT_SANDBOX_IMAGE` is required by the host compose rather than defaulted
(`docker-compose.sandbox.yml:211,332`), so name it explicitly; deployments tag
it per environment (`…-job:staging`, `…-job:prod`) so a rebuild on one side
cannot replace the other's image.

`claim_job` uses `FOR UPDATE SKIP LOCKED`, so every host shares one queue
with no leader and no sharding.

| Variable | Default | Notes |
| --- | --- | --- |
| `AGENT_SANDBOX_BACKEND` | `kata` | `process` (no isolation) refuses to start unless `AGENT_SANDBOX_ALLOW_UNISOLATED=1`. On the default `kata`, deploy refuses until the host has the shim and one real container proves it gets its own kernel; `container` is an explicit, and loudly restated, shared-kernel downgrade |
| `AGENT_SANDBOX_IMAGE` | required | Image built from `Dockerfile.agent-sandbox` or an equivalent deployment-owned image |
| `*_EXTRA_VERSIONS` | blank | Comma-separated tested versions to bake beside each source-reviewed default. Select one for new chats in Admin; retained chats keep the version they started with. The five defaults are not host settings because runner/control-plane capability admission attests them as one protocol unit |
| `AGENT_EGRESS_CHAT_NETWORK_POOL` | required | This deployment's RFC1918 IPv4 pool, one daemon-arbitrated `/29` per active group bridge. No default: it must be disjoint from every other deployment's pool on the machine — of two that overlap, the host rules refuse the one installed later — and from the host's Docker, VPN and private-service routes. A group's bridge is removed on stop and rebuilt on wake |
| `AGENT_EGRESS_DNS_SERVER` | required | The one IPv4 resolver sandboxes may use, on TCP and UDP 53. It is the only exception to the protected ranges, so a site resolver on a private address or on this host works. Not loopback, and not inside the pool; see _Choosing the resolver_ below |
| `AGENT_EGRESS_PROTECTED_CIDRS` | — | Comma-separated IPv4 blocks to refuse on top of the built-in host, private, link-local/metadata, CGNAT, multicast and reserved ranges — an institution's services on public addresses, for example. It can only add protection |
| `AGENT_RELAY_NETWORK` | derived | Where lifecycle finds the two relays; the host stack derives `<COMPOSE_PROJECT>-agent-relays`. It must exist and be `internal`, or preflight refuses, and no sandbox group ever joins it. Set it only to name an existing network |
| `AGENT_EGRESS_SETUP_TIER`, `_AGENT_TIER` | removed | Retired with the network tiers and the Squid proxy, together with `AGENT_EGRESS_ALLOWLIST`, `AGENT_EGRESS_NETWORK_*`, `AGENT_EGRESS_PROXY_*`, `AGENT_EGRESS_STOP_GRACE`, `AGENT_SANDBOX_NETWORK` and `AGENT_SANDBOX_ALLOW_OPEN_NETWORK`. Nothing reads them, so setting one changes nothing; delete them from `.env` |
| `AGENT_RUNNER_HOST` | host name | Enrolled execution environment, shared by every turn it runs — the unit the admin placement switch picks between. `deploy/host/sandbox_host.sh` fills it from the host's own name. Never derived inside the container, where the hostname is a container id. After enrolment the persisted credential is authoritative, so editing this renames nothing. Unset keeps the environment out of the pool and changes nothing else. Distinct from `AGENT_PHYSICAL_HOST`, which is the display-only inventory name grouping environments that share one server |
| `AGENT_PHYSICAL_HOST` | host name | Physical server this environment runs on, reported at enrolment as the `physical_host` label and used to group the admin inventory. `sandbox_host.sh` defaults it to `hostname -s`; set it explicitly when invoking Compose directly. Never a container hostname or a runtime suffix |
| `AGENT_HOST_SLOTS` | `3` | Turns at once until an admin sets a number on the Servers page; at least two, at most 128. `AGENT_RUNNER_REPLICAS` is no longer read |
| `AGENT_RETAINED_DOCKER_DATA_ROOT` | `/var/lib/docker` | Expected daemon `DockerRootDir`. Keep this default. Put the durable disk under it by bind-mounting `<disk>/docker` and `<disk>/containerd` onto `/var/lib/docker` and `/var/lib/containerd`. A custom daemon data root is rejected. Verified by the host and sandbox manager, and never mounted into a chat container |
| `AGENT_HOST_STORAGE_PROBE` | `overlay_root` | Which probe measures the filesystem holding the sandbox writable layers, shown as the Servers page's Storage column. `off` stops measuring storage on that host and is the rollback; an unrecognized name reports `unsupported` rather than measuring something else. A reading is published only for a topology the host deployment has attested; see _How full is the sandbox storage?_ |
| `AGENT_STORAGE_MIN_AVAILABLE_MIB` | `1024` | Bootstrap value for **Storage reserve for new sandboxes**; the admin runtime setting replaces it after the host's first settings heartbeat. Fresh verified available space below this reserve declines a new sandbox allocation without spending the turn. Existing sandboxes may resume; unknown capacity does not establish a pressure verdict. This is an admission guard, not a disk quota |
| `AGENT_SANDBOX_UID` / `_GID` | `10001` | Only for a custom sandbox image; must match its user |
| `AGENT_REPO_ALLOWLIST` | — | Comma-separated `owner/name`, or `owner/*`, for the single-tenant dogfood. Every admitted user may have the platform clone and publish to the listed repositories. Users who connect the App themselves do not need it; unset simply means the only entitlement is a user's own connection |
| `AGENT_GITHUB_APP_CLIENT_ID` / `_CLIENT_SECRET` | — | The App's OAuth half. Only the user-facing connect flow needs it; minting installation tokens uses the private key alone |
| `AGENT_GITHUB_APP_INSTALL_URL` | — | GitHub App installation URL, `https://github.com/apps/<slug>/installations/new`. Offered after connecting whenever the App is not on the user's own account, and behind _Configure repositories_. The control plane adds a short-lived, user-bound `state`; set the App's Callback URL to the agent web's own origin, `<origin>/connected?provider=github` (see _2. Credentials_) |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` | — | GitLab.com OAuth application credentials, kept on the control plane. Configure only `read_user` and `read_api` scopes |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | — | Exact registered callback, normally `<frontend>/agents/connected?provider=gitlab`. HTTPS is required except for localhost development |
| `AGENT_SANDBOX_MCP_URL` | — | Host only: MCP-only relay URL as this host's sandboxes see it. The standalone host compose derives it from the MCP tunnel alias, and the control plane no longer reads it; never point it at inference or the full control API |
| `AGENT_MCP_TUNNEL_*` | — | Remote MCP-only listener destination and independent restricted SSH key/known-hosts; see `ENVIRONMENT.md` for the complete set |
| `AGENT_GITHUB_TOKEN` | — | Control-plane fallback publisher credential; unset means the publisher idles |
| `AGENT_PUBLISH_BASE_BRANCH` | `dev` | What a draft PR targets when its job named no branch of its own. A job started from a branch targets _that_ branch; this is only the fallback |

The authenticated `/agents/integrations` page is the only supported place to
start either OAuth flow. OAuth state is unpredictable, bound to the signed-in
user and provider, expires after ten minutes, and is consumed once. GitLab also
uses PKCE. GitLab access and refresh tokens are encrypted before persistence,
rotated on refresh, never returned to the browser, and revoked best-effort on
disconnect.

GitHub repositories retain the complete Agent lifecycle: checkout, branch
discovery, push, and draft PR publishing use short-lived GitHub App installation
tokens. Draft PR titles summarize the captured user and assistant messages across
the shared session through a bounded inference request under a separate,
owner-scoped grant. Generic completion text is rejected and retried. If inference
is unavailable, the publisher uses the first concrete line from each included
turn's closing summary, skipping headings and generic completion preambles. It
falls back to the changed files when no usable summary exists. The
`agent_pr_title_generation_failed` log records the snapshot ID and fallback
reason as structured fields, plus the exception type and HTTP status when
available, without logging conversation text or credentials. Enable JSON logging
in Admin Settings to see these fields. The original task remains in the PR body.
A conversation keeps one draft PR across its turns while that PR is open; once
someone closes or merges it, the next published snapshot opens a new draft PR
from the same `agent/…` branch.
GitLab is intentionally narrower in this release: it verifies the
authenticated GitLab user and shows their accessible projects for discovery.
GitLab projects do not appear in the Agent task composer, and the control plane does
not claim to read their source or publish GitLab merge requests yet.

### Provisioning Kata on a runner host

Kata gives each sandbox group a lightweight VM with its own kernel. Use it for
untrusted repositories, whose build and test scripts execute arbitrary code.
Plain containers share the host kernel and are limited to trusted internal
repositories.

Install the runtime on the **host**. The runner image contains a Docker client;
the host daemon must resolve `io.containerd.kata.v2`. Run once per host as root:

```bash
sudo deploy/host/setup_kata_runtime.sh
```

It is idempotent, checks the host can actually start VMs (`/dev/kvm`, CPU
virtualization extensions) before downloading anything, verifies the release
against a pinned SHA-256, and links the shim into `/usr/local/bin` so
containerd finds it without a unit-file edit. The version is pinned in the
script; upgrading is a two-line change there, and `--check` then fails on every
host still carrying the old one.

#### Guest tracing and member isolation

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

#### Guest networking

Docker's embedded DNS resolver (`127.0.0.11`) is outside the guest. After each
Kata activation a trusted guest-root helper therefore writes two files: a relay
block in `/etc/hosts` with the relays' addresses on that group's bridge, and
`/etc/resolv.conf` naming `AGENT_EGRESS_DNS_SERVER`, the only resolver the host
rules let a group reach. Under runc neither is touched: the container was
created with `--dns` set to that same server, and Docker's resolver forwards
to it. There is no network switch and no guest restart for networking — setup
and the agent share the group's bridge. A running guest is never rewired
underneath it either, because Docker's live network changes alone do not update
Kata 3.32's guest interfaces: a running sandbox that is not on exactly its
group bridge is refused until it has been stopped.

#### Existing runc groups and activation checks

When switching a host from runc, keep its retained runc groups on a compatible
sandbox manager or migrate them separately. A backend setting does not change the
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
probes under Kata on the relay network (`AGENT_RELAY_NETWORK`) and requires
HTTP 200 from inference `/health` and MCP `/healthz`. It also checks the live
runner's full sandbox preflight — which installs and reads back the host
tables — sandbox manager, and control-plane paths. The relay network is only where
lifecycle finds the two relays; retained groups get their own bridges during
activation, and the in-guest check at each cold start covers the path from
there. The compose `..._internal` network is the host services' network and
cannot substitute for either check.

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

#### Scheduling and runtime attestation

The control plane independently enforces the same fact at scheduling time.
`AGENT_DEFAULT_EXECUTION_PROFILE` is already `untrusted-kata-v1` in the
control-plane compose (`docker-compose.yml:139`), so this is the default rather
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

#### Member process cleanup

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

#### Fork compatibility and turn ordering

Historical Fork additionally requires `native-fork-at-anchor-v1`, and for the
retained group either the host's current sandbox image being the group's own,
or the group's `claude-session-fork` helper matching the reviewed version
(recorded in `agent_sandboxes.fork_helper_version` when the container was created) while
the host's image still ships the exact CLI version the container runs. A
release therefore does not take Fork away from existing chats: it rebuilds the
image, but the image's helpers and pinned CLIs stay the same unless the release
changed them. An older, unknown or unreviewed fork helper keeps the exact-image
rule, so it is never assumed to understand a historical anchor; nor is a host's
fork driver pointed at a CLI version its own image no longer ships. Sending a
message into a running turn follows the same CLI-version rule and does not
depend on the fork helper.

Migration `0026_fork_helper_version` records version `2` for existing groups
created under this schema: both the old protocol-v2 claim gate and the newer
helper-version gate required that same at-anchor implementation. Groups copied
from a legacy database stay unrecorded. A runner creating a group reports the
helper version in its reservation; a later reservation cannot overwrite it
with a rebuilt host image's version. An older API ignores the optional field,
and an older runner omits it, leaving new rows unrecorded until their image's
capability can be proved rather than inferred from the host's current image.

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

#### Execution slots

Set concurrency from the admin Servers page. The runner container runs
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

#### Removing a server

Open a server's details on the admin Servers page and choose **Remove server**
from its action menu. Confirm permanent removal to delete its allocation, nodes,
registered hosts, host settings and capacity history, node registration tokens,
admission holds, and compute activity. Shared connections and deployment profiles
remain available. User conversations, runs, and deleted-sandbox audit records are
preserved.

Release cloud resources before removing their records. Attached servers without
release support can be removed after their hosts are disabled. Remove retained
sandboxes through the sandbox deletion flow and wait for cleanup to finish; clear
the default placement pin, running attempts, and deployment drains first. Removal
checks these dependencies in the same transaction as cleanup and refuses work
whose provider result is unresolved. An acquisition that never submitted to its
provider can be removed even when resource observation is unavailable.

The compute API is `DELETE /admin/agent/servers/<allocation_id>` with
`{"expected_revision": N}`. A revision conflict requires refreshing the server.
For hosts without an allocation, the action uses the existing guarded
`DELETE /admin/agent/runner-hosts/<host>` endpoint. Stop an unmanaged host agent
before removal; inventory cleanup does not shut down its machine or remove files.

#### Sandbox settings

Use the admin settings pages for sandbox and workspace limits. The turn time
limit, repository size limit, container CPU, memory and disk limits, sandbox
idle timeout, sessions per sandbox,
workspace command/file limits and terminal resource limits sit in the
_Sandbox_ card on the _Settings_ page, once
for every server, and in the _Sandbox_ table of each server's details on the
_Servers_ page for that server alone; the API is `PUT /admin/agent/host-settings` and
`PUT /admin/agent/runner-hosts/<host>/settings` with
`{"settings": {"<key>": N}}`, where `null` clears a key. A server's own value
wins over the global one, and a setting set in neither place uses its built-in
default. Migration `0036_host_settings_import` imports existing valid nondefault
`.env` observations as per-server settings without replacing an administrator's
choices. Hosts without a complete report finish this import through their
initial heartbeats. Admin edits and resets exclude only the changed keys from
pending imports, so unrelated legacy customizations are preserved. Global
exclusions also protect newly enrolled hosts. Subsequent `.env` edits have no
effect, and clearing an override never restores them.
`ENVIRONMENT.md` §_Sandbox settings from the admin pages_ lists the precedence
and initial variables. They ride the heartbeat
answer like the slot limit, so nothing is redeployed, and the same rule holds:
saving is not applying. Each host reports what it applies, the runner's share
as it hands each turn over and the sandbox manager's as the sandbox manager confirms
it, and the page shows _applying_ until the report matches. Nothing running is
cut short: a turn keeps the time limit it started with, an existing sandbox
keeps its CPU, memory, disk quota and chat capacity, and a shortened terminal lifetime
closes terminals already past it on the sandbox manager's next sweep. A runner that
does not advertise `host-settings-v1` keeps its `.env` values until it is
redeployed.

The container size controls apply to each new sandbox group, shared by its
conversation and forks. Defaults are 2 vCPU, 4 GiB memory, and disk 0 (no
per-container quota). CPU accepts fractional vCPU; memory and disk accept MiB
or GiB. A positive disk limit bounds the writable layer, including the
workspace and installed files, using Docker's `--storage-opt size=`. It
requires `overlay2` on XFS mounted with project quotas (`pquota` or
`prjquota`); Docker rejects creation on an unsupported host. Existing groups
keep their original limits through follow-ups, restarts, snapshots and Fork.
See `ENVIRONMENT.md` for bootstrap variables and storage requirements.

**Recommended for servers that run untrusted work.** An `untrusted-kata-v1`
sandbox's writable layer holds its workspace and every installed file, survives
`docker stop`, and shares one disk with every other sandbox on the server, so
without a disk limit one tenant can fill that disk for all of them. Set
**Container disk** above 0 on each Kata server once its Docker data root is on
XFS with project quotas. This is a recommendation, not a requirement: scheduling
and admission do not change, and a server left at 0 keeps taking untrusted
work. Each claim reports the limit a new group would get (the claim
attestation's `sandbox_disk_limit_mib`). The control plane logs
`agent_untrusted_sandbox_without_disk_quota` when it hands such a server a new
untrusted group, the runner logs the same event as it starts that group, and
the Servers page shows **No container disk quota** under that server's
Storage. Existing groups keep the limit they were created with and are not
flagged.

#### Sandbox archives

This policy captures automatic initial and final lifecycle archives. The
explicit **Archive to S3 and delete** control has its own control-plane storage
configuration, documented in [Sandbox archive storage](#sandbox-archive-storage).

Use the **Sandbox archives** card under **Admin → Settings → Sandbox** to store
the deployment-wide archive policy in the Agent database. The destination is
fixed at `s3://freeinference/cloud-agent/sandbox`; it is displayed read-only and
is never accepted from the browser. An administrator atomically saves the
enabled state, AWS region, maximum compressed archive size, multipart
threshold, and part size. Authentication is either an AWS access key or the
workload-identity pair: a customer-managed KMS key ARN and the expected
12-digit bucket owner. An access key makes that pair optional; uploads then
use SSE-S3 unless a KMS key is also set. The secret is write-only, encrypted
at rest, and omitted from the audit row. Updates use the revision returned by
the last read, so two administrators cannot silently overwrite each other.
Every change is recorded in the Audit log.

The dashboard uses admin-only `GET` and `PUT`
`/admin/agent/sandbox-archive-settings`. To inspect the same policy and host
rollout directly in Postgres, use read-only queries such as:

```sql
SELECT sandbox_archive_settings,
       sandbox_archive_revision,
       sandbox_archive_updated_at
FROM agent_runner_policy
WHERE id IS TRUE;

SELECT host,
       sandbox_archive_settings_applied_revision
FROM agent_runner_hosts
ORDER BY host;
```

Do not put archive policy, the destination, or an AWS access key in `.env`.
Enter an access key on the Sandbox archives card when the sandbox manager has no
workload identity. The control plane encrypts the secret before storing it
and sends it to the sandbox manager on the authenticated heartbeat channel. The sandbox manager
keeps it in memory for S3. Sandbox markers do not contain it, so it does not
enter the container. A sandbox manager that predates access-key support rejects that
heartbeat payload and does not confirm the revision; upgrade those sandbox managers
before relying on the key. With no access key, the sandbox manager uses the
host's AWS workload identity and the ordinary SDK credential chain. That
identity needs the narrow S3 multipart/write and exact object-metadata
permissions for the fixed prefix, plus encrypt access to the configured KMS
key. The bucket must have versioning enabled. Configure lifecycle retention
and access logging on the bucket itself.

Concretely, grant the sandbox manager role `s3:GetBucketVersioning` on
`arn:aws:s3:::freeinference`; `s3:ListBucket` on that bucket constrained to the
`cloud-agent/sandbox/*` prefix (so an exact-key `HeadObject` can distinguish a
missing immutable initial object from an authorization failure); and
`s3:GetObject`, `s3:PutObject`, and `s3:AbortMultipartUpload` on
`arn:aws:s3:::freeinference/cloud-agent/sandbox/*`. The configured KMS key
policy must permit `kms:GenerateDataKey` and `kms:Decrypt` for SSE-KMS
multipart uploads. An access key needs those same S3 actions; SSE-S3 uploads
do not call KMS. Add an `AbortIncompleteMultipartUpload` bucket lifecycle
rule as defense in depth for a host that disappears before its bounded abort
request completes.

Enabling archives requires migration `0042_sandbox_archive_settings` and a
runner/sandbox manager advertising `sandbox-archive-settings-v1`. The settings
card distinguishes the saved database revision from the revision each sandbox manager
has confirmed applying. While archives are enabled, new claims fail closed on
hosts or exact runner replicas that have not applied that revision; disabling
the policy preserves compatibility with older hosts.

This is a lifecycle archive, not a per-turn transcript log:

- the initial object captures `/workspace` once after source/setup and before
  the first Agent process receives a prompt;
- each sandbox-manager-managed clean stop flushes native Agent state and stores a final
  object containing `/workspace`, `/agent-state`, and the optional
  `/home/agent/.freeinference-platform` state;
- repeated turns while the sandbox remains live do not create objects. A later
  wake followed by another clean stop replaces the deterministic final key;
  S3 bucket versioning retains its earlier object versions according to the
  bucket's lifecycle policy.

Policy changes are forward-only. Enabling archives cannot reconstruct the true
pre-prompt baseline of a sandbox that already ran while the policy was
disabled, so such a sandbox can have a final object without an initial object.
Disabling prevents new archive preparations and can leave an earlier initial
object without a later final. It does not abandon durable work that was already
prepared while enabled: an initial baseline whose upload result is unresolved,
or a clean stop already fenced for final capture, remains pinned to its original
revision and finishes under that revision. For paired before/after archives,
enable the policy and wait for the settings page to show the revision applied on
every compatible server before admitting new work; stop or drain those
sandboxes before disabling it.

The initial object is immutable and retry-safe. Final capture is synchronous
with the stop hook, bounded by the configured size and operation deadline, and
an exhausted upload is reported as a failed clean stop instead of being called
successful. Archives are an export/forensics boundary only: this release does
not list, download, replay, or restore a sandbox from S3. The retained Docker
writable layer remains authoritative for normal stop/start continuity.

A stop the broker did not perform cleanly has no final archive. This covers a
host crash or reboot, a broker that exited while sandboxes kept running, a
`docker stop` issued behind the broker, and a native transport that failed to
close. Native history cannot be proven flushed in those cases, so the broker
records a `skipped` final marker (event `sandbox_final_archive_skipped`)
instead of uploading, releases the bridge, and lets the chat continue: the next
turn resumes through the ordinary native-resume checks, exactly as with
archives disabled. The next clean stop archives normally, so S3 then holds no
final for the skipped stop. Groups that an older broker marked `blocked` are
treated the same way after the upgrade. Only a group whose skip cannot be
written durably, or one found before any policy has been delivered, stays
fenced with its bridge until reconciliation succeeds.

#### Restorable checkpoints

While archives are enabled, every clean archived stop also stores one more
object: a checkpoint of the container's changes over its sandbox image. This
is on by default; clear **Save restorable checkpoints** on the same settings
card to opt out, and disabling archives disables checkpoints. The broker runs
`docker container commit` on the stopped container and uploads an OCI image
archive that holds only the commit's manifest, config and new layers to
`cloud-agent/sandbox/v2/<sha256(sandbox id)>/g<generation>/<checkpoint id>.oci.tar`.
The key names the sandbox, not the host, so a later release can restore it on
any server that holds the same image. This release only captures; nothing
restores from a checkpoint yet.

- Checkpoints require archives and use the same bucket, credentials, size limit
  and IAM permissions on `cloud-agent/sandbox/*`. Each object is written once
  (`If-None-Match`); a retry after a lost response must find identical bytes.
- A checkpoint never fails a stop. It runs after the final archive is durable;
  a failure is logged as `sandbox_checkpoint_failed` and the stop completes.
  It shares the final archive's capture slot and deadline, and adds a commit,
  an export and an upload to each clean stop.
- The Sandbox Manager reads the new layers straight from containerd's content
  store, mounted read-only at `AGENT_CONTAINERD_CONTENT_DIR`. Host Compose binds
  `AGENT_HOST_CONTAINERD_CONTENT_DIR` (default
  `/var/lib/containerd/io.containerd.content.v1.content`, the system containerd
  behind Docker's containerd image store) there. Blobs are named by digest and
  every byte copied is checked against it; a damaged blob fails the checkpoint.
  The mount grants nothing the Docker socket does not already grant. When the
  mount is missing, empty, or belongs to another daemon, the broker logs
  `sandbox_checkpoint_content_unavailable` and falls back to `docker image save`,
  which streams the whole image (once per image for the base scan, then once
  per checkpoint) and keeps the same blobs. On a CloudLab Kata host the save
  path took about 90 seconds per export for a 12 MiB change; the content-store
  path writes only the change.
- The broker keeps the latest receipt per sandbox under
  `AGENT_SANDBOX_ARCHIVE_STATE_DIR/checkpoints` and repeats it on every
  heartbeat. The control plane records it in `agent_sandbox_checkpoints` only
  when it names the group's current host, generation and container. The admin
  sandbox page shows the latest one. Receipts are recorded as `reported`; the
  object is verified when restore uses it.
- The policy omits the default and carries only an opt-out
  (`checkpoints_enabled: false`), which is sent only to hosts that advertise
  `sandbox-checkpoints-v1`. Older sandbox managers parse the policy unchanged
  and keep archiving without checkpoints.
- The archive holds the container's writable layer: workspace, native session
  state, and anything installed outside `/workspace`. Credential tmpfs mounts
  are not part of a commit. Bucket lifecycle and retention rules apply to the
  `v2/` prefix like the rest of the archives.

#### Launch failures and queue order

If a turn process cannot start or exits before taking delivery, the host
returns it through `POST /v1/agent/worker/jobs/<job>/release`. That spends no
attempt and avoids waiting for an unowned lease. Release is refused once the
CLI has admitted the turn: replay could execute the prompt twice. Lease expiry
then follows the retained-session rules and can end the conversation.
Consecutive launch failures pause claims, trigger backoff, and are reported in
the heartbeat so the Servers page can explain unused capacity.

Run one runner container per host. `AGENT_HOST_SLOTS` provides the initial
limit until an administrator overrides it; the deployment no longer reads a
replica count. Scaling the service would enroll the same host twice. Deployment
checks start a turn process with empty stdin and require preflight followed by
a clean refusal to run, exercising launch before any prompt is admitted.

Queue order is global FIFO. Per-user fairness and concurrency quotas remain
deferred, so a batch from one user can occupy every slot.

### One sandbox-group inactivity clock (and separate safety watchdogs)

`AGENT_CHAT_IDLE_TIMEOUT_S` defaults to **1800 seconds (30 minutes)** and is
the group's sole inactivity timeout. After the latest activity across every
member has durably settled and no member or sandbox manager operation is active, the
Shared Session Supervisor stops the container and removes its bridge. It never
issues `docker rm`. A later acquire reinstalls host rules, recreates the bridge,
and starts and inspects the same labelled container. Supervisor shutdown leaves
chat containers untouched.

At sandbox manager startup, reconciliation stops running orphans before readmission.
It never adopts resident processes on the strength of an unproven in-memory
lease or infers an active turn from an empty lease table. Docker observations
travel in the enrolled heartbeat. The control plane checks the durable attempt
fence and idle deadline before returning a generation-scoped stop command; the
next heartbeat records Docker's STOPPED state.

For a proven activation, the heartbeat also reports active turn, terminal,
file, or Git operations and the remaining host-monotonic idle delay. A terminal
close carries its last activity time: manual close counts as fresh activity;
reaping an already-idle PTY continues the original group window without adding
a second timeout. A reconstructed generation-zero RUNNING container with no
active attempt or deadline receives one fresh 30-minute recovery window. The
database accepts it only once, preventing later heartbeats from extending it
or stopping an active turn.

#### Lost attempt leases

Before a prompt reaches Agent CLI stdin, the worker persists its `job_id` as
the sandbox's admission receipt. If that retained activation's attempt lease
expires, the system stops it, marks the context LOST, and fails the job; it
does not automatically retry even when the crash may have happened just before
stdin. Any later messages already waiting behind that turn are failed in the
same database transaction, and a new follow-up receives
`sandbox_context_lost` instead of becoming a permanently unclaimable queued
job. That conservative choice prevents duplicate prompt/tool execution.

#### Safety watchdogs

These guards serve separate purposes; they do not create extra idle states or
deactivate a healthy group:

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

A bounded workspace command's timeout or output overflow kills its own verified
process group and preserves the sandbox's other resident sessions and terminals.
Host output readers use fixed-size chunks, byte-bounded diagnostic tails, and
bounded queues; oversized native CLI records stop the affected member. Resource
checkpoints continue to reach the control plane while CLI output is silent. See
[Host execution limits](host-execution-limits.md) for the limits and boundaries.

#### Resume, Fork, and publication

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

A snapshot whose patch the publisher refuses, whose capture the host refuses,
or whose five publication retries run out stays `failed` with its reason, and
its group is marked `dirty`. It is not retried. The group's next snapshot,
captured when a later turn settles, supersedes it: every snapshot is the whole
diff from the last published commit, so it carries the failed changes too, and
is refused in turn if it still holds what was refused. Snapshots still publish
in epoch order; only a failed one stops holding the later ones back. If the
failed attempt had already pushed its commit to the `agent/<thread>` branch, a
later snapshot with different changes is refused as a moved branch until that
branch is deleted; the platform never force-pushes.

Disk-pressure admission and alerting and a non-mutating pre-prompt context
fingerprint remain deferred. Local functional checks do not establish production
Kata, live-provider, or operational acceptance.

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
separate sandbox manager suspension token for the shared workspace. Completing or
recovering one sibling releases only that token; interactive PTYs are resumed
only when no sibling remains in a protected phase. The sandbox manager still accepts
the previous generation's token-less request shape during a rolling deploy,
but current control-plane requests always identify the owning job.

### Rolling deployment order

Downstream deployment automation coordinates the host and control-plane
scripts. This repository publishes neutral artifacts; it does not contain a
named environment's deployment Actions jobs. The two deployment tracks can run
in parallel under the exact-SHA readiness fence described here. See
[the release contract](../../deploy/release/README.md) for artifact deployments.

#### Host and control-plane readiness

The host prepares the replacement sandbox/runner artifacts and proves Kata
before invalidating its ready marker. It resolves the old runner's exact
Compose container IDs, then asks Docker Engine for an unbounded graceful stop.
The runner stops claiming and waits for every admitted turn. Graceful shutdown
does not signal its turn children: each child finishes its one turn and exits
naturally, including a child whose acceptance handover is still uncertain. The
sandbox manager and tunnels remain available until those turns settle. Only then does
activation replace the sandbox manager and runner. After the isolation, tunnel, runtime
and retained Docker-root checks pass, it records `.deployed-sha` and only then
publishes an exact `shared-sandbox-deployment:<sha>` capability. In parallel,
the control-plane deployment prepares its artifacts and runs migrations. Before
`compose up` can replace a running API, the deployment waits for a heartbeat
newer than the start of its readiness check and reverse-probes every sandbox manager
eligible for new-chat placement. The claim route enforces that same exact deployment
capability. A pending runner registers and heartbeats its base protocol so the
host checks can inspect them, but does not poll; this also prevents the previous
API from assigning work before it knows the new claim field. A runner with a
pending/failed marker therefore cannot take work even during a cold-start API
bootstrap. When placement has moved between hosts, the check also probes every
online host that still owns a retained group; changing the pin must not make old
chats disappear from rollout safety. `SHARED_READY_TIMEOUT` controls that wait
and defaults to 900 seconds.

The deploy user writes `.shared-live-ready` in the deployment checkout, separate
from the container-owned enrollment directory. Runners mount only that file
read-only at `/run/cloud-agent/shared-live-ready`. The host script preserves its
inode when changing `pending:<sha>` to `<sha>`, so existing file mounts see the
transition; an empty or partial write cannot advertise an exact target SHA.
No credential ownership or permission changes are needed for a rollout.

#### Rollback compatibility

The deployment orchestrator runs separately from the target checkout. In a
GitHub Actions deployment, it comes from the workflow commit. An online rollback
cannot execute a pre-fence target's old scripts; commits without the current
`deploy/DEPLOY_PROTOCOL`, or with an incompatible protocol value, are refused
before changing Docker state. The current protocol,
`shared-live-exact-sha-v4`, marks the one sandbox network: a v3 deployer cannot
install it, and no deployment rolls back across it (a host that ran the
previous network needs the
[one-time cleanup](#one-time-cleanup-when-upgrading-to-the-one-network)). It
also excludes ancestors that predate the rewritten schema baseline
(`0001_schema_baseline`, 2026-09-10): their Alembic chain is one the database
no longer records, so they are not schema-compatible rollback targets.
Database changes are expand-first. Never downgrade columns underneath a
still-running new API; roll code back to a schema-compatible commit first and
treat any later contract migration as a separate reviewed operation.

For downstream Actions triggered by `workflow_run`, the workflow definition
comes from that repository's default branch. Synchronize orchestrator changes
there before relying on a new rollback fence, and do not dispatch an older
default-branch workflow against an environment during that rollout window.

Migration `0049_host_drain_fence` introduced durable drain ownership and its V1
recipe marker. Additive migration `0051_host_drain_fence_epoch` establishes the
current permanent compatibility floor. Before activating the V2 control plane,
the deployment orchestrator must reject every candidate whose control-plane
recipe lacks `AGENT_DEPLOY_DRAIN_FENCE_V2`; after activation, do not downgrade
the application or schema below that release. The recipe retains
`AGENT_DEPLOY_DRAIN_FENCE_V1` only so an older orchestrator can perform the
bridge upgrade. V1 alone is not a valid rollback target once V2 has run.

Every host now has a non-null `drain_fence_epoch`. The API returns its canonical
UUID spelling and requires the same value, together with the expected owner
token, when acquiring or transferring a fence. A successful state change
rotates the epoch. An exact requested-token retry is checked first, so a lost
success response remains idempotent even though that success changed the epoch;
a different token carrying the old epoch is stale.

The database enforces the generation outside the API as well. Its `BEFORE
UPDATE` trigger rotates the epoch when `status`, `credential_hash`,
`drain_fence_token`, `drain_fence_status_before`, `drain_fence_active`, or
`compute_node_id` changes, and ignores an attempt to assign the epoch directly.
Its `BEFORE DELETE` guard rejects deletion while a drain fence is active. This
means an operator, reaper, old writer, or placement change invalidates every
previous snapshot rather than recreating a token-only ABA state. An older V1
control plane does not compare the epoch and is therefore not operationally
compatible, even though its writes still cause rotation.

Inspect fence state before rollback or recovery:

```sql
SELECT host, status, slots_used, drain_fence_token,
       drain_fence_status_before, drain_fence_active, drain_fence_epoch
FROM agent_runner_hosts
WHERE drain_fence_token IS NOT NULL;
```

The compatibility floor remains even when this query is empty: the current
control plane may create a receipt on its next automated deployment. If an
active fence is abandoned, redeploy the V2 fence-capable release and use its
token-conditional release (or the audited force override). If that is
impossible, stop the control plane and every deployment workflow, begin a
transaction, lock exactly one host row, and verify the expected token, epoch,
and `slots_used = 0`. Only then may an operator clear that one active fence:

```sql
BEGIN;
SELECT host, status, slots_used, drain_fence_token, drain_fence_active,
       drain_fence_epoch
FROM agent_runner_hosts
WHERE host = '<exact-host>'
FOR UPDATE;

UPDATE agent_runner_hosts
SET status = 'online',
    drain_fence_token = NULL,
    drain_fence_status_before = NULL,
    drain_fence_active = FALSE
WHERE host = '<exact-host>'
  AND status = 'draining'
  AND slots_used = 0
  AND drain_fence_token = '<expected-token>'::uuid
  AND drain_fence_epoch = '<expected-epoch>'::uuid
  AND drain_fence_active
RETURNING host, drain_fence_epoch;
COMMIT;
```

Require exactly one returned row. The returned epoch is the newly rotated
value; do not try to assign it yourself. To clear only an inactive receipt, use
the same exact host, token, and epoch predicates with `NOT drain_fence_active`;
change status only when the separately verified recovery requires it. Never
bulk-clear fence fields. If the owner, token, or epoch is uncertain, leave the
host draining and restore the V2 fence-capable control plane.

#### Cold starts and failed deployments

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
[the legacy database copy plan](../agents/specs/2026-09-18-legacy-database-copy.md).

### Rebuilding the agent database

This procedure preserves selected state from a deployment using the **retired
Alembic chain** and intentionally discards task history. It was rehearsed on
2026-09-10. Use [copy conversion](#preserving-an-old-chain-database) when history
must survive. This table selection predates later credential and configuration
additions; do not use it as a generic reset procedure for a current database.

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
(`docker-compose.sandbox.yml:294`); the code default
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

`deploy.sh` migrates before replacing the API. Each migration must therefore
remain compatible with the running version until replacement. The transitions
listed below require a maintenance window when upgrading from their older API
versions. This list describes those schema transitions; it does not mean every
later additive migration requires downtime. Review compatibility against the
actual source and target versions before choosing a rolling deploy.

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
- `0058_mcp_connector_invariant` repairs the singular connector route for MCP
  connections and adds a unique constraint on each connection's authorization
  mapping. Treat this revision as a stopped-writer boundary. Older binaries can
  recreate a missing mapping, leave a stale mapping after disconnect, or retain
  connector authority after changing a connection away from
  `user-connector`. Do not use a mixed-version rolling deploy for this
  revision. Instead:

  1. stop every control-plane, MCP relay and web-tier database writer;
  2. run `alembic upgrade 0058_mcp_connector_invariant` (or upgrade through
     `0058` to the intended later revision);
  3. deploy the invariant-aware control-plane, MCP relay and web-tier release;
     and
  4. resume writers only after every old binary has been replaced.

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

Retained chat state is the Docker container writable layer. On a daemon that
uses the containerd image store, the images live beside it under containerd.
Durable same-host operation puts both stores on a mounted data disk and leaves
the daemon paths at their defaults. `docker info` must keep reporting
`DockerRootDir=/var/lib/docker`, and `AGENT_RETAINED_DOCKER_DATA_ROOT` stays
`/var/lib/docker`. A custom `data-root`, `graph`, or containerd `root` is
rejected before the host starts.

Automatic SSH bootstrap does this from the server's sandbox storage path. See
[Sandbox storage path](operations-compute.md#sandbox-storage-path). It refuses
to move a nonempty store that is not already this bind.

A manual host uses the same layout. `<disk>` is an existing writable mount,
for example `/scratch/hybridInference-cloud-agent/sandbox`:

```text
<disk>/docker      bind -> /var/lib/docker
<disk>/containerd  bind -> /var/lib/containerd
```

Persist the binds in `/etc/fstab`:

```text
<disk>/docker /var/lib/docker none bind,nofail,x-systemd.requires-mounts-for=<disk> 0 0
<disk>/containerd /var/lib/containerd none bind,nofail,x-systemd.requires-mounts-for=<disk> 0 0
```

Give each daemon a drop-in, `/etc/systemd/system/docker.service.d/cloud-agent-storage.conf`
and `/etc/systemd/system/containerd.service.d/cloud-agent-storage.conf`:

```ini
[Unit]
RequiresMountsFor=<disk> /var/lib/docker
```

The containerd file names `/var/lib/containerd` instead of `/var/lib/docker`.
Run `systemctl daemon-reload`. A missing disk then keeps that daemon stopped
instead of opening an empty store at the bare path.

To move a store that already has data, copy it while the daemons are running,
stop Docker and containerd, and copy again with `--delete` until that copy
exits 0. Move the old directories aside, create empty mount points, bind-mount,
then start the daemons. Do not point `daemon.json` at the new path. Containers
with `restart=no` stay stopped across that restart and must be started by
hand. Delete the copies left aside only after a reboot has come back on these
binds and the host is serving.

The sandbox manager compares `AGENT_RETAINED_DOCKER_DATA_ROOT` with the
daemon-reported `DockerRootDir` and refuses a mismatch. It deliberately cannot
prove `findmnt` itself: giving the sandbox manager a host-root mount just to inspect
storage would violate the Docker-only boundary. The mount check belongs in
host provisioning, as the `RequiresMountsFor=` drop-ins above.

Do not bind `<disk>`, `/var/lib/docker`, `/var/lib/containerd`, `/workspace`,
or `/agent-state` into the sandbox manager or sandbox. Do not use generic `docker system
prune`, container TTL cleanup, `docker rm`, or image garbage collection against
`org.freeinference.chat-sandbox=true` objects. Inspect and operate them through
Docker (`docker inspect`, `docker exec`, `docker cp`, and the sandbox manager RPC), not by
walking overlay directories. Back up or move both graphs only with Docker and
containerd stopped, preserving ownership, hard links, extended attributes and
sparse files.

This makes `docker stop`/`docker start` survive runner, sandbox manager, Docker daemon,
and host reboots as long as the same binds return. It does **not**
make a missing container recoverable on another host: export to object/cold
storage and cross-host restore remain the explicitly deferred persistent
checkpoint phase. Until that lands, loss of these graphs is context
`LOST`, and the control-plane database still supplies the authoritative
thread→host→container identity needed to locate it.

A missed heartbeat, an offline/disabled host row, or a temporarily unreachable
sandbox manager is not evidence that this Docker state vanished. Keep the chat pinned
and unavailable, recover or re-enrol the same named host, and reconcile its
exact labelled container. Only an inspection on that owning Docker daemon that
proves the expected container/data-root is gone may transition the context to
`LOST`; never create a blank replacement on another host.

### How full is the sandbox storage?

The Servers table has a **Storage** column beside CPU and Memory: how much of
the filesystem holding the retained sandbox writable layers is in use, in `df`
terms. It answers a question the sandbox count cannot — `25 stopped` says
containers are retained, not how much disk they hold — and it observes one
filesystem, not one chat: a repository, its dependencies, uncommitted changes
and the native CLI session all live in a container's writable layer, and
stopping a container does not release it.

**Storage reserve for new sandboxes** is a runtime setting in Admin Settings and
each server's configuration. Its default is 1024 MiB. The runner uses a fresh
attested reading before accepting a turn that would create a sandbox. Below the
reserve it declines the handover, returns the untouched turn to the queue, and
reports a launch error with the available space and suggested remedies. Existing
retained sessions can resume and cleanup stays available. Free Docker storage or
adjust the reserve to restore new allocation.

The check does not reserve space or impose per-container quotas: existing or
concurrent writers can consume space afterward. Disabled or unverified storage
measurement remains explicitly unmeasured and does not block allocation; never
substitute another filesystem's available space for Docker writable-layer capacity.

#### What the reading measures

A runner is an ordinary container under
the same Docker daemon and storage driver as the sandboxes, so OverlayFS's
`statfs` semantics make `os.statvfs("/")` inside it describe the upper layer's
filesystem — the disk the writable layers are on. No host mount, no Docker
query, no child process. The figures are `df`'s: `used` is `total - free`,
`available` is what an unprivileged writer may still use, and the percentage is
taken over `used + available`. The blocks the filesystem reserves for root that
are still free are in neither figure — which is why the two do not add up to
the total, and why `total - available` is not used space.

#### Attesting the storage topology

Docker's `overlay2.size` (and a
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

#### Refreshing or disabling the metric

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

#### Measurement limits

The reading measures block space, so `available` does not promise
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

Runners pull work. Placement is enforced when a runner claims a job, rather
than through a dispatch request from the control plane. These rules apply:

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
  heartbeat and sandbox manager probe, not whether one runner happened to claim recently.

Adding a _second_ machine is not only this switch. The configured model and
MCP endpoints use the two relays on the sandbox's group bridge, with private
addresses otherwise blocked by the host rules. A runner box that is not the
gateway's host needs the two tunnels described next, and everything in
[§0 above](#0-the-sandbox-network-needs-its-host-rules-a-resolver-and-both-relays).
The gateway's public inference address may also be reachable; replay of a
leaked grant there is an [accepted risk](#mcp-prerequisite-and-accepted-inference-grant-risk).
Preflight refuses at startup rather than failing jobs at their first model or
MCP call, but it refuses _there_, on that machine, not here.

### Running runners on another machine

A second machine is worth adding for capacity, or because it is the one with
the hardware. What makes it more than "run the script over there" is how a
sandbox reaches the platform. Its group bridge routes to the public internet,
but only on TCP 80/443, and the host rules refuse private addresses except for
the group's fixed relays. The configured gateway and MCP endpoints use those
relays, SSH tunnels that lifecycle code finds on the relay network and attaches
to each group's bridge. A separate control-plane relay remains visible only to
the runner:

```text
runner host                                      service host(s)
┌──────────────────────────────────────┐
│ [sandbox, its own group bridge]      │
│   ├→ gateway-tunnel ─────────────────┼──ssh──→ inference :8080
│   ├→ mcp-tunnel ─────────────────────┼──ssh──→ MCP-only :8001
│   └→ host rules and NAT ─────────────┼────────→ public IPv4, TCP 80/443 only
│                                      │
│ [runner, internal network]           │
│   └→ control-plane-tunnel ───────────┼──ssh──→ control API :8000
└──────────────────────────────────────┘
```

Each tunnel is a relay, not a proxy — no CONNECT and no caller-chosen
destination — so nothing a sandbox sends can widen either route. Setup and the
agent share the group's bridge, so setup reaches package registries over the
same public TCP 80/443 as everything else. The control-plane tunnel never joins
a group bridge, and its address on the runner's network is a private one the
host rules refuse from every group.

**Why ssh rather than exposing the gateway's port.** Sandboxes carry scoped
inference grants and runners carry enrolled-host credentials. Binding either
service to the lab network puts those capabilities in cleartext on a shared
segment. The tunnel keeps each service listening on loopback.

```bash
deploy/host/sandbox_host.sh up       # relays, sandbox manager and the runner
deploy/host/sandbox_host.sh check    # probe relays and runner/sandbox_manager paths
deploy/host/sandbox_host.sh status
deploy/host/sandbox_host.sh down
```

`deploy/host/docker-compose.sandbox.yml` contains the runner, sandbox manager,
three purpose-scoped tunnels, and two networks: the stack's own network and the
route-less relay network. Runner and sandbox manager create group bridges through Docker.
This is the supported standalone topology. It does not start a gateway
`backend`, as the source repository's old overlay could do through its
`depends_on: backend` declaration.

| Variable | Notes |
| --- | --- |
| `AGENT_TUNNEL_*` | Inference relay destination, key, known-hosts file and ports; remote default `127.0.0.1:8080` |
| `AGENT_MCP_TUNNEL_*` | MCP-only relay's independent destination and SSH material; remote default `127.0.0.1:8001`. Do not point it at the full control API on 8000 |
| `AGENT_CONTROL_PLANE_TUNNEL_*` | Runner-only control API relay; remote default `127.0.0.1:8000`. It is deliberately absent from sandbox networks |
| `AGENT_*_TUNNEL_SSH_KEY` | Private key, mounted read-only. Each relay has its own key and its own account on the far machine, restricted as [described below](#authorizing-the-tunnels-on-the-far-machines); no relay needs a shell or permission to open another relay's port |
| `AGENT_*_TUNNEL_SSH_KNOWN_HOSTS` | Required host-key file for each relay. Host checking is not disableable: an unverified hop would hand scoped tokens and tunnel traffic to whatever answers there |
| `AGENT_GATEWAY_HOSTNAME` / `AGENT_MCP_HOSTNAME` | The two names a sandbox resolves on its group bridge, `agent-gateway` and `agent-mcp` by default. Do not replace them with service addresses: a relay named by IP is refused at startup |
| `AGENT_CONTROL_PLANE_HOSTNAME` | Runner-only control relay name, `agent-control-plane` by default |

Generate the known-hosts entry and compare its key fingerprint with a trusted
record for the service host:

```bash
ssh-keyscan -p 22 "<gateway-host>" > /etc/agent-tunnel/known_hosts
```

#### Authorizing the tunnels on the far machines

Prefer **Admin → Servers → Server setup → Automatic setup**
([compute runbook](operations-compute.md#automatic-setup)). It creates the
accounts below, writes and reads back their sshd policy, and refuses an
existing policy that would widen it. Authorize a tunnel by hand only when that
is not available, and then reproduce exactly what it generates.

Give each relay a dedicated account with no shell: the inference account on the
gateway machine, the MCP and control accounts on the control-plane machine.
Each account's `authorized_keys` holds one line:

```text
# gateway machine, inference account
command="",restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 AAAA... inference
# control-plane machine, MCP account
command="",restrict,port-forwarding,permitopen="127.0.0.1:8001" ssh-ed25519 AAAA... mcp
# control-plane machine, control account
command="",restrict,port-forwarding,permitopen="127.0.0.1:8000",permitlisten="172.24.0.1:18092" ssh-ed25519 AAAA... control
```

After `restrict`, `port-forwarding` re-enables remote (`-R`) as well as local
forwarding, and `permitopen` limits only the local destination. On its own,
any of these keys could open listeners on the far machine. So the control
key's `permitlisten` names its single reverse listener — the control-plane
machine's Docker bridge gateway and this host's `AGENT_SANDBOX_MANAGER_REVERSE_PORT` —
and each account gets its `Match User` block from
[`deploy/host/sshd-tunnels.conf.example`](../../deploy/host/sshd-tunnels.conf.example).
The inference and MCP blocks set `AllowTcpForwarding local` and
`PermitListen none`. The control block allows that one `PermitListen` with
`GatewayPorts clientspecified`. Install the blocks before any other `Match`
block that could apply to these accounts, run `sshd -t`, and confirm each
account with `sshd -T -C user=<account>,host=localhost,addr=127.0.0.1` before
reloading sshd.

Run `sandbox_host.sh check` from the host checkout. Sandbox-runtime containers
on the relay network must receive HTTP 200 from inference `/health` and MCP
`/healthz`. The live runner must pass full sandbox preflight, including host
table read-back, and reach the sandbox manager and control plane. Requests from the
host shell do not exercise these container paths.

Once it is up, the machine appears in **Admin → Servers** and can be selected
there.

#### The way back: the control plane reaching this host's sandbox manager

The control plane also needs a route back to this host's sandbox manager.

File listings, terminal traffic, and `git status` go **control plane → workspace
sandbox manager**. The sandbox manager runs beside the Docker daemon owning the workspace; the
host row holds the address reported at enrollment. The integrated local stack
shares a network, so
`http://sandbox-manager:8092` works there. The supported standalone host stack
does not share a network with the control-plane stack—even when the two
projects run on one physical machine—so that service name resolves nowhere.
An unreachable sandbox manager address produces **503**, including on
`terminals/suspend` during normal execution. The historical first deployment
failed three attempts at that point. Verify the route before admitting jobs.

The route cannot be opened from the other side: the control plane's machine
generally cannot reach this one at all. So it rides the connection this machine
already opens — a reverse forward on the control-plane tunnel:

```text
  runner host                                control plane host
┌──────────────────────────┐              ┌──────────────────────────┐
│ sandbox-manager         │◀─── -R ──────│ 172.24.0.1:18092         │
│        :8092             │              │  (docker bridge gateway) │
│                          │              │        ▲                 │
│ control-plane-tunnel ────┼──── ssh ────▶│        │ control-plane   │
└──────────────────────────┘              └────────┴─────────────────┘
```

| variable |  |
| --- | --- |
| `AGENT_SANDBOX_MANAGER_REVERSE_BIND` | The **control plane host's docker bridge gateway**. Not `127.0.0.1`, which its containers cannot see; not `0.0.0.0`, which publishes the sandbox manager to that machine's whole network behind one shared token. Find it with `docker network inspect <net> --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'` |
| `AGENT_SANDBOX_MANAGER_REVERSE_PORT` | A free port on that host |
| `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` | `http://<bind>:<port>` — what the runner reports, and what the control plane will dial |

The **control plane host's** sshd must apply `GatewayPorts clientspecified` to
the control account, as its block in
[`deploy/host/sshd-tunnels.conf.example`](../../deploy/host/sshd-tunnels.conf.example)
does, together with that account's `PermitListen` and the key's `permitlisten`
naming this exact address and port. Never set it to `yes`: that binds every
remote forward to the wildcard address whatever the client asked for, and
reports success, so each host's sandbox manager is published on all of that machine's
interfaces behind one shared token. With the default, `no`, sshd binds the
forward to loopback instead, where the control plane's containers cannot reach
it. A listener sshd refuses outright (a port in use, or one `PermitListen` does
not allow) makes ssh exit under `ExitOnForwardFailure`, and the container
restarts; the two wrong `GatewayPorts` values do not. Confirm on that machine
with `ss -ltn` that the port listens on the bridge address and nowhere else.

Also limit the reverse ports in that machine's firewall to its Docker bridge
interface. The bind address alone does not: Linux accepts a packet for any
local address on any interface, so a neighbour routing the bridge address
through the machine's LAN interface would still reach the listener. Find the
interface with `ip -o -4 addr show to <bind>`, then, for each reverse port (or
the range you assign), for example:

```bash
iptables -I INPUT -p tcp --dport 18092 ! -i br-0123456789ab -j DROP
```

Persist the rule the way that machine persists its firewall.

Set all three. The supported standalone host stack never publishes its sandbox manager
and does not share a Compose network with the control plane, even when both
stacks happen to run on the same physical machine. An advertise URL with no
tunnel behind it is an address nothing answers on, which is the same 503 with a
longer explanation.

Check from inside the control-plane container:

```bash
docker exec "<control-plane-container>" \
  python -c "import urllib.request; print(urllib.request.urlopen('http://<bind>:<port>/health', timeout=5).status)"
```

**HTTP 401 proves this route is reachable.** `urllib` reports it as an HTTPError
because the sandbox manager refuses an unauthenticated request. A DNS error or timeout
means the route failed.

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

**The Docker socket is the host authority.** Runner and sandbox manager can create,
start, stop, inspect, and exec in chat containers, so isolating those services
from users matters more than changing their Unix uid. The security boundary is
the retained sandbox (Kata for untrusted multi-tenant work; plain Docker only
for trusted repositories), not a host bind mount or a second per-turn
container.

### The sandbox network

A chat may carry a `setup_script`. It runs inside the retained container before
the first Agent CLI turn, on the same network the agent gets afterwards. Its
files persist in that container's Docker writable layer across follow-ups, idle
stop/start, and host service restarts; there is no host-side setup cache or
portable setup archive. Package managers reach their public registries directly
over HTTPS — there is no proxy to configure — and anything that needs another
port, Git over SSH for one, is refused. Long-lived platform and upstream
credentials stay outside the sandbox; member-scoped inference and MCP tokens
enter their CLI processes. The sandbox runs unprivileged, so `apt install`
does not work — use `pip --user`, a virtualenv, `npm ci`.

New chats can also select up to eight runtime plugins through **+ → Plugins**
or the create request's `plugins` field. This is thread configuration: follow-ups
keep the selections, and Fork copies them to the new member. Claude Code and
Codex use entries such as
`{"plugin": "review@example", "marketplace": "example/plugins"}`; their
marketplace source may be a GitHub repository or a credential-free public HTTPS
source (a Git repository for Codex). pi, OpenCode, and Kilo use npm package
entries such as `{"plugin": "@example/plugin@1.2.3"}` with no marketplace.
Exact npm versions are optional; ranges, tags, local paths, arbitrary install
commands, and credentials in source URLs are rejected.

Installation runs during member runtime initialization, before its resident CLI
accepts a prompt, using the pinned runtime version and the member's private
home/configuration. Successful installation is retained across process and
container restarts; failed installs can retry and never mark the selection
complete. Errors and timeouts fail startup with a bounded diagnostic. Plugins
use the group's existing network and unprivileged member identity. Project
configuration and automatic plugin discovery remain disabled where the runtime
supports those guards; explicitly selected plugins are loaded through the native
runtime mechanism, alongside the platform's MCP configuration.

There is one network, the same for every task and every phase, and nothing
selects it ([design](../agents/specs/2026-09-23-agent-public-egress-design.zh-CN.md)).
Each sandbox group — setup, the agent and every Fork sibling — gets one
routable Docker bridge, a `/29` from `AGENT_EGRESS_CHAT_NETWORK_POOL`. Its host
interface is named with this deployment's prefix, `ca` and five hex digits
derived from `AGENT_SANDBOX_NAMESPACE`, followed by eight derived from the
group. Docker routes and masquerades the bridge; this deployment's host
nftables tables decide what it reaches.

#### What it allows and refuses

Allowed:

- public IPv4 on TCP 80 and 443 — curl, Python HTTP clients, pip, npm, Git
  over HTTPS;
- the one resolver in `AGENT_EGRESS_DNS_SERVER`, on TCP and UDP 53. It is
  checked before the protected ranges, so a resolver on a private address or
  on this host works — on port 53 only;
- the group's own two relays, by exact bridge, address and port: the gateway
  tunnel for inference and the MCP tunnel for tools.

Refused:

- every address of this host, the bridge's own gateway included, except the
  resolver above when it runs here;
- every IPv4 address that is not ordinary public unicast — IANA's
  special-purpose blocks `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10`,
  `127.0.0.0/8`, `169.254.0.0/16` (cloud metadata among them), `172.16.0.0/12`,
  `192.0.0.0/24`, `192.0.2.0/24`, `192.88.99.0/24`, `192.168.0.0/16`,
  `198.18.0.0/15`, `198.51.100.0/24`, `203.0.113.0/24`, `224.0.0.0/4` and
  `240.0.0.0/4` — plus Azure's WireServer, `168.63.129.16`, a platform
  service on a public address, and whatever `AGENT_EGRESS_PROTECTED_CIDRS`
  adds, on any port;
- other groups, this deployment's or another's;
- every other port and protocol: UDP 443 (QUIC), ping, all IPv6;
- connections started from outside. A reply is accepted only for a connection
  the group opened, and one from a protected address is dropped even then;
  there is no blanket "established" accept.

The rules judge the address a packet is actually sent to, so a name that
resolves — or is rebound — to a private address is refused like the address
itself. Most refusals are answered at once, with a TCP reset or an ICMP
"administratively prohibited", so a blocked request fails immediately instead
of timing out; IPv6 is dropped silently.

**What it does not promise** (design §2). TCP 80/443 is a port rule: nothing
inspects what travels inside, so DNS over HTTPS or a tunnel on those ports is
not prevented. Nothing stops a sandbox sending the repository, or its own
short-lived tokens, to a public host. And the platform's own public addresses
may be reachable. MCP tool calls remain absent from the public main API;
public replay of inference grants is accepted as described below.

#### MCP prerequisite and accepted inference-grant risk

Design §3 records the 2026-09-25 decision to accept inference-grant leakage
and replay for real-user deployments, replacing the separate internal
inference entry as a release prerequisite.

**MCP isolation remains required.** Done in #327: the main API no longer
mounts the tool-call router, so the MCP tunnel must point at 8001 (see
[MCP servers](#mcp-servers)).

**Inference keeps its existing path.** The CLI holds its member's grant and
the inference tunnel keeps its current gateway target (8080 by default).
No separate gateway listener or host credential-substitution relay is needed
for this decision. Code or instructions encountered by a task may cause its
grant to leave the sandbox and be replayed through a reachable gateway API.
Those calls consume the owner's quota and platform inference resources,
subject to the owner's model permissions, grant expiry and revocation;
they acquire no other user's identity, MCP tools or platform-management rights.

A resident grant renews while its process remains live, including between
turns. The short TTL and default 30-minute idle stop are not absolute limits
measured from a leak. Existing stop, replacement, takeover and release paths
still revoke grants, with expiry as the fallback if revocation fails.

The v4 deployer update and old-network cleanup still apply. This decision
does not waive Kata/network acceptance, member isolation, long-lived
credential restrictions, account quota checks or other release gates.

#### The host tables

The rules are two nftables tables of this deployment's: an `inet` table for
routed traffic — to the internet, the host, another bridge — and a `bridge`
table for frames between a sandbox and its relays on the same bridge. Both are
named `cloud_agent_ca` plus the first five hex digits of the SHA-256 of
`AGENT_SANDBOX_NAMESPACE`, which on a host stack is the Compose project, so
with `COMPOSE_PROJECT` set:

```bash
digits="$(printf '%s' "$COMPOSE_PROJECT" | sha256sum | cut -c1-5)"
table="cloud_agent_ca$digits"
```

The runner and the sandbox manager hold no `NET_ADMIN` and are not in the host's network
namespace. They change host rules through a short-lived helper: their own
image, run with `nft` as its entrypoint in the host's network namespace, with
`NET_ADMIN` and no other capability and a read-only root filesystem — the
`host_nft` function below has the same shape. It runs under runc whatever the
daemon's default runtime is, because a Kata VM would change its own kernel's
rules, not the host's.

- **Installed before anything joins a bridge.** The runner's and the sandbox manager's
  startup preflight install the tables and read them back — every chain, its
  hook and its rule count, the protected set, the registered pool — and any
  difference stops the runner claiming and the sandbox manager starting. Group network
  work repeats the check and trusts a pass for 60 seconds.
- **Scoped to this deployment.** The tables recognise traffic by the bridge
  interface prefix. They never flush or edit another deployment's rules, so
  several deployments can share a host.
- **Relays by exact endpoint.** When a group's relays are attached, their
  bridge, address and port are written to a `relays` set in both tables, and
  removed with the bridge. The relay's own default route stays on its stack
  network, because it joins every group bridge at gateway priority -1.
- **Watched while groups run.** While any group of this deployment is
  running, the sandbox manager reads the tables back on every five-second sweep, logs
  `network_guard_rules_changed` when they differ from what it installed, and
  reinstalls whatever is missing. If that fails twice in a row, it logs
  `shared_chat_network_unguarded` and stops every running group — workspaces
  and native sessions kept — rather than let one go on without the boundary it
  was started behind. Waking a group reinstalls and verifies the rules first,
  and fails while they cannot be.
- **Kept by the kernel.** A runner exiting leaves the tables in place; a
  reboot clears them, and no group starts until they have been installed
  again.

To look at them, read-only, use the same shape of helper:

```bash
image="$(docker inspect --format '{{.Image}}' "${COMPOSE_PROJECT}-runner-1")"
host_nft() {
  docker run --rm --network host --runtime runc --cap-drop ALL \
    --cap-add NET_ADMIN --read-only --entrypoint nft "$image" "$@"
}
host_nft list table inet "$table"      # chains; each refusal has a counter
host_nft list table bridge "$table"
host_nft list set inet "$table" relays # relay endpoints, per live bridge
host_nft list tables                   # every deployment's tables here
```

Keep `--runtime runc`: on a host whose Docker default runtime is Kata the
listing would otherwise come from a VM. The counters are totals across all of
this deployment's groups. No per-connection log is kept (design §10).

#### Overlapping pools

Each deployment registers its pool in its own `inet` table, and after
installing it compares its pool with those of the `cloud_agent_*` tables the
kernel created before its own. Of two whose pools overlap, the later one
refuses, with `this deployment's pool … overlaps …, registered earlier by …`
and a request to give each deployment its own `AGENT_EGRESS_CHAT_NETWORK_POOL`.
The earlier one keeps running, so a neighbour started with a copied pool cannot
take a working deployment down, and because each checks after installing, two
starting together cannot both miss each other.

The pool must also stay clear of every network the host already routes.
Docker takes an explicit subnet as given — it checks other Docker networks,
never the host's own routes — so a group bridge inside a LAN, a VPN route or a
private service's network would add a more specific route and draw the host's
traffic for those addresses into a sandbox bridge. After installing, the guard
reads every IPv4 route in every table (`ip -4 route show table all`, through
the same helper with no capability at all) and refuses with
`this deployment's pool … overlaps the host route … on …`. The default route,
catch-alls wider than a `/8` such as a full-tunnel VPN's two `/1` halves, and
this deployment's own bridges are not conflicts. A network reached only
through the default gateway has no route of its own to find, so choose the pool
with the site's addressing in mind as well.

A retired deployment's tables stay until the host reboots, and keep its pool
registered. Once that deployment is gone for good, delete its two tables — its
own and no one else's — with the helper above:
`host_nft delete table inet cloud_agent_ca<its digits>`, then the same for
`bridge`. Never delete or flush a table a running deployment uses: Docker
routes and masquerades every group bridge, and the table is what limits it.
Anything that flushes the host's whole ruleset removes these tables too; they
are reinstalled at the next check, so do not run such a reload on a host with
active groups.

#### Choosing the resolver

`AGENT_EGRESS_DNS_SERVER` is one IPv4 address, and the only DNS a sandbox can
use. A public resolver works anywhere. A site resolver on a private address, or
on this host, works too, and becomes the one private address a sandbox can
reach — on port 53 only. It cannot be loopback (a sandbox cannot reach the
host's `127.0.0.53`), unspecified, or inside the pool.

Under runc, a sandbox is created with `--dns` set to it: Docker's embedded
resolver at `127.0.0.11` answers the relay aliases and forwards every other
name there. That setting is fixed when the container is created. A Kata guest
cannot see Docker's resolver, so after every activation the trusted guest-root
helper writes the address into the guest's `/etc/resolv.conf`, beside the
relay block in `/etc/hosts`. The in-guest check at every cold start confirms
both that the guest is configured with this resolver and that it answers: one
query for the root's name servers, which any working recursive resolver
answers, within three seconds.

#### Protecting more addresses

`AGENT_EGRESS_PROTECTED_CIDRS` takes comma-separated IPv4 blocks to refuse on
top of the built-in ones — an institution's services on public addresses, for
example. It can only add protection. The resolver stays reachable on port 53
even inside an added block, because DNS is checked first. A change reaches the
host when the runner and sandbox manager are redeployed with it: every install replaces
the protected set, and the read-back compares it with the configuration.

#### When something is refused

Settings first. `sandbox_host.sh prepare` reads them with the candidate
image's own parser before any runner is drained, and a refusal prints
`the sandbox network settings were refused (see the error above)` under one of
these:

- `AGENT_EGRESS_CHAT_NETWORK_POOL must name this deployment's group address pool`
  or `… must be RFC1918 space holding at least one /29` — unset, not private,
  or smaller than a `/29`;
- `AGENT_EGRESS_DNS_SERVER='…' must be one IPv4 resolver address` or
  `… must be reachable from a group bridge` — unset, not IPv4, loopback,
  unspecified, or inside the pool;
- `AGENT_GATEWAY_URL='…' names a relay by IP; use its network alias`, and the
  same for `AGENT_SANDBOX_MCP_URL` — each group bridge gives a relay a fresh
  address, so only its name carries over.

Preflight — until it passes, the runner claims nothing and the sandbox manager does not
start:

- `the relay network '…' does not exist` — the stack is not up, or
  `AGENT_RELAY_NETWORK` names something else;
- `` the relay network '…' is not `internal` `` — it must not route anywhere;
  let Compose recreate it as declared;
- `nft -f - failed: …` or `network guard helper could not run: …` — the
  helper could not install the rules: the host kernel lacks nftables support,
  or Docker refused the helper container;
- `the network guard runs its helper from this container's own image` — the
  runner finds its image by inspecting its own container, named by `HOSTNAME`;
  a custom `hostname:` breaks that;
- `… is missing chain …`, `… holds N rules, expected M`,
  `… is not hooked on …`, `… protects …, not the configured set` or
  `… registers pool …, not …`, each naming the table — the tables changed
  after they were installed, or a runner and a sandbox manager with different settings
  are overwriting each other. Redeploy both from one `.env`;
- `this deployment's pool … overlaps …` — another deployment's pool, or
  `… overlaps the host route … on …` — a network the host already has; see
  [Overlapping pools](#overlapping-pools);
- `the sandbox network needs Docker 28 or newer, client and daemon` — the
  relays' gateway priority needs both; see §0.

Group lifecycle. These fail the turn or operation that needed the group's
network; the workspace is kept:

- `relay alias '…' has no running container on relay network '…'` (or
  `multiple running containers`) — a tunnel is down or restarting, or two
  stacks share one relay network;
- `AGENT_EGRESS_CHAT_NETWORK_POOL … has no free /29 subnet` — every `/29` in
  the pool is taken, by this deployment's groups or by other Docker networks
  inside the same range;
- `group network check failed inside the sandbox: …` — the in-guest check
  after a cold start found one of:
  - `no default route`, `resolver is […], expected […]`;
  - `resolver … did not answer: …` or `… did not answer the query usefully` —
    the resolver is down, refuses recursion, or (under runc) the container was
    created with a resolver the rules no longer allow;
  - `relay host:port unreachable: …`;
  - `the host at …:9/udp was refused by ECONNREFUSED, not by the network
    rules`, `the metadata address 169.254.169.254:80 answered; the network
    rules are not in this guest's path` (or the same for `the platform
    address 168.63.129.16:80`), or `… was not refused within 2s` — the host
    and both platform addresses must be refused at once by the rules' own
    ICMP answer, and were not: the guest's traffic is not passing this
    deployment's tables.

  The sandbox is stopped, not deleted. Reaching the public internet is not
  part of the check — an outside outage is not a broken boundary;
- `fixed relay joined the chat network without its gateway priority` — the
  Docker daemon ignored `--gw-priority`; it needs Docker 28 or newer;
- `Kata guest network sync failed: …` — the guest-root helper could not write
  `/etc/hosts` or `/etc/resolv.conf`;
- `retained chat is attached to network '…', which is not its group bridge`,
  ending `needs the one-time cleanup` — a chat from the previous network; at
  sandbox manager startup it keeps the sandbox manager from starting. See the next section;
- `a running retained chat is not on exactly its group bridge` — a running
  guest is never rewired underneath it; stop it first.

In the sandbox manager's log, `chat_legacy_network_found` names a bridge of this
deployment from the previous per-phase topology; it is reported, never adopted
or repaired. `chat_group_relay_exceptions_left` means a removed bridge's relay
entries could not be withdrawn from the tables. They match nothing once the
bridge is gone, and if the helper still cannot run, the next activation fails.

### One-time cleanup when upgrading to the one network

Only for a host that ran the previous network — per-phase bridges and the
Squid proxy. A host that never did needs none of this. The new version adopts
nothing the old one left. It refuses a retained chat still attached to an old
network, with the message above, and because the sandbox manager stops every retained
chat of its deployment when it starts, one such container keeps the new sandbox manager
from starting at all; old bridges are only logged. The design treats the switch
as a step for development and test hosts with no users to carry over (§8), so
these steps discard the old retained chats. **Export any workspace you need
before step 4.**

**Remove only this deployment's resources.** Every listing below filters by
this deployment's own labels: the Compose project for the stack's own
containers, networks and volumes, and `org.freeinference.sandbox-namespace` —
the same Compose project, on a host stack — for chat containers and group
bridges. Read each listing before removing anything. A resource carrying
another project or namespace belongs to another deployment on this machine;
leave it, and leave anything you cannot attribute.

1. Drain the host (**Admin → Servers**) and wait until it runs nothing, then
   stop this stack:

   ```bash
   export COMPOSE_PROJECT="<this stack's Compose project>"
   deploy/host/sandbox_host.sh down
   ```

   If the checkout already holds the new release, add the two settings from
   step 7 to `.env` first: the new compose file does not render without them.

2. List this deployment's chat containers:

   ```bash
   docker ps -a \
     --filter label=org.freeinference.chat-sandbox=true \
     --filter "label=org.freeinference.sandbox-namespace=$COMPOSE_PROJECT" \
     --format '{{.ID}}  {{.Names}}  {{.Status}}  {{.Networks}}'
   ```

   Stopping the stack leaves chat containers as they were, so some may still
   be running. Every one of them predates the upgrade. The new version refuses
   any that is still attached to an old network, and none of them was created
   with `AGENT_EGRESS_DNS_SERVER` — under runc a container's resolver is fixed
   when it is created. So all of them go.

3. Export what you want to keep. A chat's workspace exists only in its
   container's writable layer, and `docker cp` reads a stopped container:

   ```bash
   docker cp "<container>:/workspace" "./<container>-workspace"
   ```

4. Remove the containers from step 2, by ID:

   ```bash
   docker rm -f "<id>" …
   ```

   This is the one sanctioned `docker rm` of chat containers; everywhere else
   [Retained Docker storage on a host](#retained-docker-storage-on-a-host)
   forbids it. Their chats cannot continue (their context reads `LOST`); new
   tasks get new containers.

5. List this deployment's chat networks, and remove the old ones:

   ```bash
   docker network ls -q \
     --filter label=org.freeinference.chat-sandbox-network=true \
     --filter "label=org.freeinference.sandbox-namespace=$COMPOSE_PROJECT" |
     xargs -r docker network inspect --format \
       '{{.Name}} {{.Internal}} {{index .Options "com.docker.network.bridge.name"}}'
   docker network rm "<name>" …
   ```

   Each line is a name, whether the network is internal, and its bridge
   interface name. An old `freeinference-chat-net-*` network is internal
   (`true`) and has no interface name; a group bridge of the new version is not
   internal and has a `ca…` one. Before the new version has run, every one
   listed is old.

6. Remove the proxy and the old Compose networks and volume. List what Compose
   created for this project:

   ```bash
   docker ps -a --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
     --format '{{.ID}}  {{.Names}}  {{.Label "com.docker.compose.service"}}'
   docker network ls --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
     --format '{{.Name}}  {{.Label "com.docker.compose.network"}}'
   docker volume ls --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
     --format '{{.Name}}  {{.Label "com.docker.compose.volume"}}'
   ```

   Remove, with `docker rm -f`, `docker network rm` and `docker volume rm`:
   the containers of the services `agent-egress-proxy` and
   `agent-egress-proxy-config`; the networks whose Compose key is
   `agent-egress`, `agent-egress-trusted` or `agent-egress-uplink` (named
   `agent-egress` or the old `AGENT_EGRESS_NETWORK_PLATFORM_ONLY`, and
   `<COMPOSE_PROJECT>-agent-egress-trusted` and `-uplink` unless `.env` named
   them); and the `agent-egress-proxy-config` volume, which held only the
   generated Squid configuration. Nothing else in these listings is part of
   the cleanup. A `down` run from the previous release will already have
   removed some of them.

7. Update `.env` from the new `deploy/host/.env.example`. Set
   `AGENT_EGRESS_CHAT_NETWORK_POOL`, which no longer has a default, and
   `AGENT_EGRESS_DNS_SERVER`. The old default pool was `10.240.0.0/16`: if two
   deployments on this machine both kept it, give one of them another, or the
   later one refuses to start. Delete the retired settings —
   `AGENT_EGRESS_SETUP_TIER`, `AGENT_EGRESS_AGENT_TIER`,
   `AGENT_EGRESS_ALLOWLIST`, `AGENT_EGRESS_NETWORK_*`, `AGENT_EGRESS_PROXY_*`,
   `AGENT_EGRESS_STOP_GRACE`, `AGENT_SANDBOX_NETWORK` and
   `AGENT_SANDBOX_ALLOW_OPEN_NETWORK`. Nothing reads them.

8. Undrain the host (**Admin → Servers**, or
   `POST /admin/agent/runner-hosts/<host>/undrain`) — with its runners stopped
   it claims nothing yet, and a drain is never cleared by a heartbeat — then
   deploy the new release as usual, with a deployer at
   `shared-live-exact-sha-v4`. Every runner's preflight installs and reads back
   the host tables before it claims. Confirm that `host_nft list tables` (from
   [The host tables](#the-host-tables)) shows this deployment's `inet` and
   `bridge` tables, then run a new task whose setup installs a package.

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

## Verified

This is the recorded verification baseline, including pre-split measurements.
It establishes what those runs exercised, not that a deployment has passed its
current release gates. Runtime pins and configuration in the operating sections
above describe the current service.

|  | How |
| --- | --- |
| Store: fencing, reaper, retained-group snapshot publish | dbtests against Postgres |
| Job API, SSE resume, worker endpoints | control-plane API and server suites |
| Inference grant attempt fencing | Live gateway. The same historical run exercised a per-job spend-cap 429; C5/C6 later removed that cap. Current inference uses the owner's gateway daily quota, which this service neither enforces nor verifies here. |
| Retained runner + runtime adapters | Docker-only local product stack with the exact Claude Code, Codex, Pi, OpenCode, and Kilo pins: every CLI passed `NEW → LIVE → docker stop → RESTORED` |
| Native Fork concurrency | every pinned CLI created a distinct child native session immediately; source and child were simultaneously `RUNNING` against one container/workspace and both crossed the local inference barrier |
| Publish: patch → branch → draft PR | real `git push` to a local bare repo |
| **A real job on a real model** | live Claude Code CLI × `deepseek-v4-flash` through the production gateway: docstring landed on disk, patch produced, 3 calls / 74,523 tokens / $0.0075 attributed to the job in `api_logs` |
| Retained Docker lifecycle | real mount-free (`Mounts=[]`) chat containers keep one writable layer and container id across group idle `docker stop` and later `docker start`; the phase bridge is released while stopped and recreated on wake, and publication capture replaces neither |
| Source import → edit → snapshot | the trusted runner imports a credential-scrubbed source archive into `/workspace`; real Git status/diff and the quiescent group publication snapshot complete without a host worktree or snapshot bind mount |
| Sandbox process boundary | real Docker: non-root Agent CLI member UIDs, dropped capabilities, and the closed agent egress of the time (since replaced by [one network](#the-sandbox-network)); this shared-kernel measurement does not establish the required Kata isolation for public multi-tenant use |
| The control plane on staging | `/v1/agent/*` live: job create/get/list/cancel, SSE stream, and an unauthenticated host refused at `/worker/claim` |
| MCP: per-user resolution, relay authorization, tool filtering | unit + API tests against a mocked upstream: one user's credential never resolves for another, a job cannot reach a server it was not granted, a dead fence loses tools, and a blocked tool never leaves the relay |
| **MCP: what `--strict-mcp-config` (hybridInference#1129) actually prevents** | the flag was added on the strength of the CLI's documented behaviour; this is the measurement. Real Claude Code 2.1.220 in a job's exact configuration (headless, `bypassPermissions`, cwd = a checkout carrying a `.mcp.json`): without the flag the CLI reports the repository's server alongside the platform's, with it only the platform's. No approval step was involved either way |
| MCP: the adapter's `--mcp-config` is accepted by the real CLI | the argv `ClaudeCodeRuntime.prepare` emits, run against the real binary: the platform's server appears in `system/init`, and a deliberately bogus flag in the same argv fails instantly, so the flags are being parsed rather than ignored |
| MCP: the token can stay out of argv | the same run with the header written as `Bearer ${AGENT_MCP_TOKEN}` and the variable exported: the CLI expanded it and the proxy authenticated the request, while the command line carried only the placeholder |
| **MCP: the proxy against a real hosted server** | the shipping router and filters against live `mcp.deepwiki.com` (which answers over SSE, so the streaming filter is the one that ran): `initialize` → `serverInfo: DeepWiki`, `tools/list` → its three real tools, `tools/call read_wiki_structure` → real documentation for `psf/requests`. Measured before the deployment-wide registry was replaced by the per-user catalogue (hybridInference#48); that deployment-wide catalogue has since been replaced by generic per-user connections, so this records the historical relay path rather than a current deployment configuration |
| MCP: the allowlist, on a real catalogue | same server, `tools: [read_wiki_structure]`: `tools/list` returned that one tool of the three, and `tools/call ask_question` came back `-32601` without the request leaving the relay. This exercised the historical tool-name allowlist; the then-current `github` catalogue instead declared `read_scope="endpoint"`. Current connections use discovered capabilities and per-session policies. |
| MCP: the authorization matrix, end to end | live token + granted server 200; live token + a server the job was not granted 403; a superseded attempt 401; an ordinary `hyi-` key 401; no credential 401 |
| MCP: Claude Code driving the proxy | the real CLI, pointed at the proxy, completed the whole handshake through it — `initialize`, `notifications/initialized`, the `GET` stream, `tools/list` — each reaching deepwiki and returning 200 |
| **A self-hosted runner completing a real job** | `ajob_04de5d509a25ded2` against staging: claimed → cloned `psf/requests` at `414f0513` (verified equal to that repo's HEAD) → Claude Code 2.1.220 in a Docker sandbox as uid 10001 → `Read` + `Edit` with `is_error: false` → 518-byte patch stored → `succeeded`. The patch was read back and matches the file the agent left on disk |

## Not verified

These are gaps in the recorded baseline, not a live inventory of staging or
production. Use them to plan acceptance checks and record new evidence in the
deployment that performs those checks.

- **Cross-model behavior.** The baseline includes one real job on one model.
  Its planned 2-runtime × 3-model matrix was not run. The service now supports
  five runtimes; the historical measurement does not establish conformance
  across their current model combinations.
- **MCP through a deployed sandbox.** The hosted DeepWiki checks used the
  shipping router and filters with a fake job store and a CLI outside a
  sandbox. The later [end-to-end test](#end-to-end-coverage) uses a real
  database, listener, and CLI with stub upstream and model endpoints. Neither
  record establishes the full deployment path: queue → runner → group bridge
  → MCP tunnel → `/v1/agent/mcp` → a user's real server.
- **Credentialed upstream acceptance.** DeepWiki required no credential.
  Tests establish that a configured per-user header reaches the upstream;
  the baseline does not establish that a real credentialed server accepts it.
- **Live-model tool behavior.** Hosted-server checks were client-driven, and
  the end-to-end test scripts the model response. They do not measure whether
  an actual deployed model chooses and uses the tools correctly.
- **Production readiness.** A named deployment's current status belongs to
  its downstream distribution. The gateway's former `/v1/agent/*` 404 was
  about routes deleted on 2026-08-09, not this service. Use the distribution's
  deployment records and the artifact contract in
  [the release README](../../deploy/release/README.md) and
  [`deploy/DEPLOY_PROTOCOL`](../../deploy/DEPLOY_PROTOCOL).
- **Historical staging model availability.** Of the 15 models then listed by
  `/v1/models`, only `glm-5.1` and `qwen3.6-35b` resolved for an agent-job
  token, and both reached `qwen3.6-35b`. The others returned
  `404 Model not found`. Recheck the configured model with an agent grant before
  using a failed first turn to diagnose the runner chain.
- **One-network acceptance on real traffic.** Unit tests cover rendered host
  rules, read-back, and group-bridge lifecycle. The baseline did not run design
  §9's real Kata guest tests against the public web and listening targets for
  every refused destination class. The 2026-09-25
  [grant replay decision](#mcp-prerequisite-and-accepted-inference-grant-risk)
  removed the separate inference entry as a release prerequisite; it supplied
  no new Kata evidence and waived no other gate.
- **Kata deployment acceptance.** Recorded jobs used the host kernel. The
  installer and runner preflight are implemented and unit-tested, including
  refusal when a daemon accepts the runtime but the sandbox still uses the host
  kernel. The original staging record did not include provisioning or a real
  Kata job. Use [the provisioning procedure](#provisioning-kata-on-a-runner-host)
  and record both guest isolation and relay reachability on the actual host.

The historical runtime probe distinguished these daemon responses:

| `--runtime` | Recorded result |
| --- | --- |
| unset (runc) | Runs |
| a name that does not exist | `unknown or invalid runtime name`: the daemon rejects it, so the flag is reaching the daemon |
| `io.containerd.kata.v2` | `failed to start shim`: the name is accepted and the shim attempted, but its binary was missing |

That ruled out a silently dropped Docker runtime flag for that probe. It did
not prove a Kata guest could start; the guest-kernel and network checks remain
necessary.

The source repository's deleted `agent-job-runner` workflow never executed on
GitHub and is no longer a gate. This repository's Actions build control-plane
images and publish neutral releases from green `dev` commits; job execution
uses enrolled hosts.

## Known gateway findings

These gateway behaviors were observed by the historical conformance suite.
Track their resolution in the gateway and recheck its deployed version:

- An upstream mid-stream disconnect is masked with a synthesized
  `finish_reason: stop` + `[DONE]`, so a client cannot detect truncation. For
  agent workloads that is a poisoning vector: truncated tool-call arguments
  look complete.
- An in-stream 429 error frame is typed `server_error`, so a client branching
  on `type` to decide whether to back off misclassifies a rate limit.

## Model behaviour observed on the first real run

The recorded first live-model run used `deepseek-v4-flash`. It replied _"I've added a docstring to the `greet` function.
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

| Source | Here | Why |
| --- | --- | --- |
| `ops/setup/setup_kata_runtime.sh` | `deploy/host/setup_kata_runtime.sh` | D7 |
| `ops/deploy/sandbox_host.sh` | `deploy/host/sandbox_host.sh` | D7 |
| `deploy/docker/Dockerfile.agent-sandbox` | `deploy/sandbox/Dockerfile.agent-sandbox` | D7 |
| `ops/deploy/agent_runner.sh up/preflight/status/down` | the standalone script's `up/preflight/check/status/down` | The co-located overlay was dropped (MIGRATION.md, open decisions, decided 2026-08-04). The standalone preflight validates an already-built image; `up` builds the target image and then repeats the authoritative checks. |
| `docker compose -f docker-compose.yml -f docker-compose.agent-runner.yml` | `docker compose -f deploy/host/docker-compose.sandbox.yml` | Same decision. There is no base compose here and no `backend` service. |
| "Use that script, not `agent_runner.sh`" | a note that the standalone compose is the only topology | Same decision. |
| The staging deploy-pipeline paragraph (`deploy_staging.sh`, the same-invocation requirement, the overlay attaching `backend` to the agent-egress network) | removed | It describes the _gateway's_ deploy pipeline driving the overlay that did not move. Keeping it would be an instruction nobody here can follow. |
| `AGENT_RUNNER_HOST` row naming `agent_runner.sh` and `deploy_staging.sh` | names the standalone script | Same. |
| `MCP_CONFIG_PATH` / `config/examples/mcp.example.yaml` | initially marked gateway-side | The move-only baseline still used the old gateway registry. Subsequent extraction work replaced it with this service's in-code catalogue, per-user connections and isolated MCP relay. |
| `pytest … test_agent_mcp.py test_agent_mcp_api.py` | initially `pytest tests/unit/test_agent_runtimes.py` | The move-only baseline left the proxy suites behind. They were subsequently ported and expanded in this repository. |

### Historical statements that remain historical

- **The pre-split verification record.** Where a row explicitly describes an
  old live measurement, it remains evidence of that historical run rather than
  a claim that a new deployment gate has been exercised. Current component
  ownership and operating instructions use the post-split topology.
- **Cutover inventory.** Secret provenance and one-time migration steps remain
  in [cutover.md](../agents/specs/cutover.md) and `ENVIRONMENT.md`; this page documents the
  resulting runtime topology.


## Reusable cloud environments

Migration `0065_environments` adds owner configuration, immutable encrypted
versions, fixed workspace bindings, isolated test leases and action results.
Control plane values use `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` with a separate
`hybridinference/environment-variable/v1` domain. Back up this key with the
existing credential store; changing it without re-encryption makes old bindings
unreadable. Secrets are not available through owner read/history APIs.

Repository environments are a standard product feature, available without an
admin switch. Settings, task selection and the Environment tab are always shown;
`/config` always returns environment limits. Legacy saved or environment values
for `repository_environments_enabled` / `AGENT_REPOSITORY_ENVIRONMENTS_ENABLED`
are ignored and no longer appear in the settings editor. Unconfigured tasks keep
base-environment behavior without preparation; configured workspaces still require
actual runner and sandbox manager support for `environment-v1`.

Upgrade every control-plane replica before creating any new environment-bound
workspace; mixed old/new admission, Fork and terminal handlers may silently lose
binding information. After the first binding, do not roll back to binaries that
cannot read it. To roll back, close those execution entrances, drain environment
tests/preparation, and keep bound workspaces on compatible hosts or fenced off.

Roll out the environment-capable runner and sandbox manager together. A claiming replica
must attest `environment-v1`, and the actual authenticated sandbox manager must advertise
it before receiving configuration. An older host releases jobs that require
configuration without consuming an attempt; it must never run them without
variables. Environment tests also obey the deployment pin, compute admission,
replica attestation and host slot limits, and receive read-only source credentials
without inference, MCP or repository write grants.

Preparation receipts live in the host credential directory under
`environment-preparation`, outside container mounts and source archives. They
contain binding/container/boot identities and masked steps, never variable values.
Keep this host state across sandbox manager restarts. Installation succeeds once per fixed
workspace; startup runs once per actual boot. A failed step remains visible in
the Environment tab and can be retried against the original binding.

Manual-action receipts live next to preparation state under `environment-actions`.
They reserve the run before launching a command and retain its masked result after
PTY eviction. Keep this directory across sandbox manager restarts; deleting it removes the
host's replay fence for requests whose control-plane acknowledgement was lost.
An interrupted launch without a recoverable result is reported as `unknown` and
is never automatically replayed. Inspect external effects before starting a new
run with a different idempotency key. These receipts do not promise exactly-once
side effects in external services.

Empty bindings still pin the original task's selection, but require neither
`environment-v1` nor preparation. Each environment test reserves one local slot
on a background worker so other free slots continue claiming coding tasks. Host
orphan queries are read-only and throttled; the control-plane reaper owns lease
expiry and history maintenance.

Cancellation fences the result immediately but keeps the leased slot until
cleanup is confirmed or the lease expires. An expired orphan no longer reserves
capacity; its returning host cannot claim new work until cleanup is acknowledged. The reaper expires leases/30-minute executions. On reconnect
the enrolled host receives only its own expired test IDs and removes the exact
labelled containers before acknowledging cleanup. Do not delete unrelated groups
or bypass lease/container identity checks to clear capacity. Archived, unreferenced
versions are collected after 30 days; existing workspace, test and publication
bindings keep their historical encrypted values for redaction/scanning.

Owner-provided credentials can be read and sent outside by user code through the
existing network. Known-value masking and trusted publish scans do not cover
encoded secrets, raw owner terminals/files, or direct Git pushes. Platform-held
long-lived credentials, reserved variable names and fixed egress remain protected.

Environment setup, startup and actions share executable package data in
`/workspace/.cloud-agent-tools`, excluded through Git's local exclude file. Member
credential HOME directories remain private. Explicit runtime refresh retains
installation inputs and applies from the next turn; already-running services keep
their old values. Startup recovery is an explicit allowance for one subsequent
human turn, never a permanent success receipt.
