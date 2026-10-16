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

Earlier runs of this checklist, from the 2026-08 extraction, are kept as a
[dated record](../agents/reviews/2026-08-04-staging-smoke-runs.md). They do not
show that any current deployment passes it.

## What a passing run proves

A completed, passing run proves identity sign-in, explicit Agent access, enrollment,
inference grants, sandbox execution, and GitHub publication for the recorded
revision and environment. A successful job alone does not prove publication:
the owner asks for it, and the publisher then processes an immutable workspace
snapshot asynchronously.

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
      `/internal/mcp-registry` is not a prerequisite, and
      `/internal/users/<id>/status` only supplies the Profile page's email.
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
- [ ] Confirm the turn succeeds with the expected file change. Nothing is
      published until you ask: choose **Create PR** in the conversation, wait
      for the publication to finish, and open the real **draft pull request**.
      Check target branch, author and diff; record its URL. Execution success
      alone is insufficient.
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
- [ ] Restore any failure-injection settings on staging and record the result
      as described under [Log](#log).

## Log

Record results while running: the date and time (UTC), result and notes for
each numbered section, plus the source SHA, environment, host, job and PR
identifiers. Keep the record with the deployment's own acceptance evidence.
