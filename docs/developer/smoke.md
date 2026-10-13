# Staging end-to-end smoke

Use this runbook on a disposable staging deployment to verify the full path from
Gateway sign-in to a real pull request. The check covers a Cloud Agent control
plane, one runner host, and a disposable GitHub repository.

If you only need a local product check without provider keys, start with the
[complete local Docker stack](../../deploy/local/README.md):

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

The local stack exercises browser login, control-plane/host wiring, all five
pinned CLIs, and retained native sessions with deterministic substitutes for
external services. It does not test live provider behavior or GitHub
publication; this staging runbook checks those boundaries separately.

**Recorded staging status:** On 2026-08-06, job `ajob_8b0be5f9e7ff9ad5`
verified enrollment, job creation and remote claim, sandbox manager workspace access,
tool-calling execution, success, four grant renewals, and revocation after
settlement. That record did not verify real PR publication, mid-run
cancellation, or the full fail-closed sequence. See
[MIGRATION.md](../../MIGRATION.md),
[cutover.md](../agents/specs/cutover.md) and the dated baseline below. Newer local lifecycle
checks do not fill those staging acceptance gaps.

## What a passing run proves

A completed, passing run proves identity sign-in, explicit Agent access, enrollment,
inference grants, sandbox execution, and GitHub publication for the recorded
revision and environment. A successful job alone does not prove publication,
because the publisher processes an immutable workspace snapshot asynchronously.

The run does not cover migrated ownership/data, concurrency, host loss, or every
isolation boundary. Use [cutover](../agents/specs/cutover.md),
[deployment reliability](deployment-reliability.md) and
[native context recall](context-recall.md) for those separate checks. The Gateway
contracts belong to the Gateway; verify its deployed routes before starting.

## 0. Before you start

- [ ] Use a disposable staging deployment with Docker/Compose, a checkout and
      enough memory for the selected CLI. The local all-five-CLI smoke recommends
      at least 8 GiB; public multi-tenant staging requires a Kata-capable host.
- [ ] Record the exact release SHA and the Gateway environment. API, host,
      sandbox manager and sandbox must satisfy the shared deployment/version checks.
- [ ] Verify `GET /v1/identity/jwks` succeeds on the staging Gateway:

      ```bash
      curl -sS -o /dev/null -w '%{http_code} %{url_effective}\n' \
        "$GATEWAY_BASE_URL/v1/identity/jwks"
      ```

- [ ] With the configured dispatch credential, verify
      `/internal/model-catalog?user_id=<test-user>` and
      `/internal/users/<test-user>/agent-access`. The test account must receive
      `agent.use`, and the catalog must include the chosen model. A missing
      access endpoint or rejected dispatch credential blocks the run. The retired
      `/internal/mcp-registry` and `/internal/users/<id>/status` are not prerequisites.
- [ ] Have an account that can complete Gateway browser sign-in, plus
      infrastructure administration access for host enrollment.
- [ ] Choose a disposable GitHub repository and configure write/PR permission
      through the deployment's supported GitHub App or publishing credential.
      Record its base branch. Do not use a repository others depend on.

## 1. Configure

Use separate, private environment files for the control plane and host. Start
from [`control-plane/.env.example`](../../deploy/control-plane/.env.example) and
[`host/.env.example`](../../deploy/host/.env.example); [ENVIRONMENT.md](../../ENVIRONMENT.md)
is the reference. Keep the Agent database separate from the Gateway database.

Generate deployment-owned secrets once and keep them out of version control:

```bash
for name in AGENT_SESSION_SECRET AGENT_CONTROL_TOKEN_SECRET AGENT_SOURCE_CONTROL_ENCRYPTION_KEY; do
  printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"
done
```

Keep the encryption key stable so saved connections remain decryptable.
`GATEWAY_GRANT_DISPATCH_TOKEN` is the shared service credential; it must match
on Agent/Gateway and differ between staging and production. Agent receives
neither Gateway signing keys nor its account database credentials.

- [ ] Set the exact `GATEWAY_ISSUER`, JWKS/authorize URLs, callback allowlist,
      web origin/base path and browser API path. For existing data, prepare the
      [identity binding](minimal-identity.md#upgrade-an-existing-deployment).
- [ ] Set explicit Compose project names, sandbox image, enrolled host name,
      sandbox manager credential/reverse route, all three tunnel destinations and keys,
      and the host's non-overlapping egress pool/resolver.
- [ ] Render configuration privately using the active environment file. Export
      `AGENT_DEPLOYMENT_SHA` for direct control-plane Compose commands; the
      deployment scripts supply it themselves. Rendered configuration contains
      secrets, so do not paste it into acceptance logs.

## 2. Start the control plane

Use the deployment process in [deployment reliability](deployment-reliability.md).
For a cold source deployment, its `prepare` and `prepare-database` phases build
images, start Postgres, migrate/check the identity namespace, and bootstrap the
API and MCP listener needed for host readiness. Complete activation after the
host publishes readiness in section 4. Preserve the deployment's project and
configuration root throughout these phases.

- [ ] Migrations exit successfully at the checkout's current head. The API does
      not create missing tables at startup.
- [ ] Main API `/healthz` responds on port 8000 by default.
- [ ] MCP-only `/healthz` responds on port 8001 by default; its tunnel targets
      that listener.
- [ ] Reaper and publication workers are running. General health alone does not
      establish working grant renewal or publication.

## 3. Sign in — the identity flow end to end

- [ ] Open the web app and start sign-in. The browser reaches the Gateway's
      authorize page, returns through Agent's callback and establishes a session.
- [ ] `GET /v1/session/me` returns the test user's `id` and
      `agent_access: {allowed, permissions}`. Account roles/profile fields are
      absent. Confirm explicit `agent.use` before creating a task.
- [ ] Confirm the Agent session is signed with its own secret and carries the
      configured issuer. No Gateway session token is retained in Agent storage.
      Avoid recording cookies or token bodies in the run log.

## 4. Enrol the host

- [ ] Issue `POST /admin/agent/runner-hosts/enroll-token` with the chosen `host`
      name using infrastructure admin access. The response reveals a single-use
      enrollment token; put it in the host's private configuration.
- [ ] Deploy the host through `deploy/host/deploy.sh` for the same target release.
      The host process prepares/attests the sandbox image, starts tunnels and
      sandbox manager, probes both relays and publishes the completed-generation marker.
      Directly starting runner Compose containers does not complete this gate.
- [ ] `GET /admin/agent/runner-hosts` shows a fresh online heartbeat, expected
      deployment capability and reachable sandbox manager URL. Check the reverse sandbox manager
      route from the control plane.
- [ ] Complete control-plane activation and confirm its readiness check succeeds
      for the intended host/API SHA.
- [ ] If testing enrollment refusal, use a separate disposable token: redemption
      under a different host name must fail without consuming it. Record the
      refusal; do not attempt to reenroll an unrelated live host.

## 5. One job, all the way

- [ ] Connect/authorize the scratch repository under **Integrations**.
- [ ] Create a task on its intended base branch with a small verifiable change,
      such as adding a `CONTRIBUTING.md` containing a specified sentence.
- [ ] Observe `queued` then a host claim. Record job/attempt/host identifiers and
      the non-secret grant ID. Verify the sandbox receives scoped inference
      authority for the account's chosen model.
- [ ] Confirm events stream to the browser and at least one model/tool call
      succeeds.
- [ ] Confirm the turn succeeds with the expected file change. Wait for snapshot
      publication and open its real **draft pull request**. Check target branch,
      author and diff; record its URL. Execution success alone is insufficient.
- [ ] List files and open a terminal on the retained workspace through the app.

## 6. The parts that only fail later

Run failure injection only in the disposable staging deployment and restore its
original settings after each case.

- [ ] **Renewal:** keep a turn running across several renewal passes. Initial
      grant TTL is 120 seconds (30-second cadence × four passes); a Gateway
      renewal buys 300 seconds. Verify actual renewal and continued model calls.
      A quick successful turn never exercises this boundary.
- [ ] **Cancellation/revocation:** cancel a separate running turn. Verify worker
      acknowledgment, terminal cancellation, unusable abandoned member
      credentials and grant-revocation evidence. Revocation accelerates expiry;
      TTL remains the backstop if its HTTP call fails.
- [ ] **Fail closed:** make the Gateway unavailable, then attempt fresh sign-in
      and a new protected owner request/task creation. Neither may gain access.
      Existing-session `/me` must show a temporary failure without exposing
      protected content. Restore reachability and verify recovery.
- [ ] **Control-plane loss:** stop the disposable API while a task runs. The
      Gateway does not own or end the attempt; renewal ceases and the grant
      expires. After restoring the API, verify lease/reaper handling here.
- [ ] Check live follow-up and same-container idle restore separately using
      [context recall](context-recall.md#verification-before-accepting-a-deployment).

## 7. Tear down

- [ ] Stop the disposable stacks using their recorded project names. Keep data
      until acceptance evidence has been captured. Volume deletion is permanent;
      use `down -v` only for a stack whose database is intentionally disposable.
- [ ] Delete scratch branches/repository and revoke test-only repository and
      dispatch credentials when the deployment is retired.
- [ ] Restore any failure-injection settings on staging and append the dated
      result table below.

## Log

Record results while running. The table below is the historical baseline; append
a dated table for a new acceptance run, including its source SHA, environment,
host, job and PR identifiers. Blank cells are unverified historical steps.

| Step | Run at (UTC) | Result | Notes |
|---|---|---|---|
| 0. Preconditions | | | |
| 1. Configure | | | |
| 2. Control plane up | 2026-08-04 | **pass, without Docker** | See below. |
| 3. Sign in | | partial | Token/JWKS contract verified with real code on both sides; browser flow not run. See below. |
| 4. Host enrolled | | | |
| 5. Job → PR | | | |
| 6. Renewal / revoke / fail-closed | | | |
| 7. Tear down | | | |

## Partial run, 2026-08-04

This is a historical run, with a migration observation rechecked on 2026-09-10.
Its revision and table counts are dated evidence, not the current migration head.

Run **without a container runtime** — no Docker daemon was available — so the
service was started directly against a local Postgres instead of through
`deploy/control-plane/docker-compose.yml`. That covers the process and the
schema; it does not cover the image, the compose wiring, or anything with a
sandbox in it.

**Verified**

- `alembic upgrade head` against an empty database reaches
  `0004_residency_on_membership`, the head of the chain that replaced the old
  one on 2026-09-10, and leaves 24 tables. The baseline revision
  `0001_schema_baseline` creates 25; `0004` drops `agent_thread_residencies`.
  (Re-verified on the rewritten baseline, 2026-09-10.) The bare `postgresql://` DSN the compose file produces
  is the one that was used, which is what the driver rewrite exists for.
- The process starts and `GET /healthz` answers 200.
- **It stays up with the gateway completely unreachable** — `GATEWAY_BASE_URL`
  pointed at a closed port for the whole run. The reaper's renewal pass runs on
  a 30-second loop and had several passes; nothing crashed, and health stayed
  200. This is the fail-closed behaviour asserted in unit tests, seen end to
  end for the first time.
- `GET /v1/session/me` answers 401 rather than resolving a session, which is
  the same property from the outside: no gateway, no identity, no access.

### The identity contract, both halves real (same day)

Section 3 needs a browser, a database and a deployed control plane. The part of
it that a cutover most depends on — that a token this gateway mints is one this
service will accept — does not: it needs only the two repositories' real code
and an HTTP hop between them.

Run with the gateway's `mint_identity_token` and `public_jwks` on one side and
this service's `verify_identity_token` on the other, the JWKS fetched over
loopback HTTP rather than handed across in-process. Throwaway RSA keys,
generated in a scratch directory and deleted afterwards; nothing was written to
any host.

| Case | Result |
|---|---|
| a token the gateway signed | accepted, `sub` intact |
| signed by a key absent from JWKS | refused — cannot resolve the signing key |
| empty token | refused |
| signature altered | refused |
| `GATEWAY_ISSUER` not the minted `iss` | refused |
| **rotation:** token minted before the swap, verified after | accepted via the retiring key |
| **rotation:** token minted after the swap | accepted via the new key |

`aud` agrees by inspection as well as by test: the gateway stamps
`CLOUD_AGENT_CLIENT_ID = "cloud-agent"` and this service requires
`AUDIENCE = "cloud-agent"`. Each repository tested its own half of that string
and neither compared them.

The rotation rows are the ones worth having. Rotation is the operation that
happens under time pressure, and until now `IDENTITY_JWT_RETIRING_PUBLIC_KEYS`
was documented but never exercised against a real verifier.

**What this does not show.** No browser, no `/authorize` page, no PKCE code
exchange, no database, no session cookie — the token contract holds, which is
necessary for section 3 and not sufficient for it.

### The lookup contract, against real responses (same day)

The identity run above compared real code on both sides. This does the same for
the two internal lookups, and for a different reason: the grant contract's
transcription had gone stale in the same direction as the client, so comparing
the two records proved nothing. A response from the gateway's own route
function is a second *source*.

> **This section belongs to the pre-split gateway and is kept as history.** It
> exercised `internal_lookups.user_status`, a route this service no longer
> calls and no longer has a contract for. Admission now comes from
> `/internal/users/{user_id}/agent-access`, whose decision is
> `{user_id, allowed, permissions}` and deliberately carries no account
> profile — so the `status` / `role` / `email` rows below describe a retired
> route rather than current behaviour. What still applies is the method:
> compare the contract against a real route function, not against the client.

Called `internal_lookups.user_status` and `internal_lookups.model_catalog`
directly, with stub stores, and fed what came back to this service's readers.

| Case | Result |
|---|---|
| active user | `_may_sign_in` → yes |
| suspended user | refused |
| unknown user — the response omits `status`, `role` and `email` | refused, and the later `.get()` reads return the defaults rather than raising |
| catalog `models` / `aliases` | parsed as expected |

**Two drifts found in `gateway_contracts/internal-lookups.openapi.yaml`**, both fixed:

- `/internal/mcp-registry` was still a documented path. The gateway stopped
  serving it at the ownership amendment.
- The catalog's `aliases` field was not declared at all, though the endpoint
  has returned it since the alias work.

The status endpoint's schema was already exact, including the three fields the
unknown-user branch omits.

**Not covered, and why**

| Step | Needs |
|---|---|
| The control-plane image | A Docker daemon. CI's `Build control-plane` job passes, which proves the Dockerfile but not the compose wiring. |
| Sections 3–7 | A staging gateway serving the identity, grant and agent-access contracts, a host, and a scratch repository. |
| The two-relay topology | Two machines and the tunnels between them. `docker compose config` renders it and the network assertions hold, which is not the same as traffic flowing. |

**Corrections made to this runbook during the run:** none. Sections 1 and 2
were accurate as written.
