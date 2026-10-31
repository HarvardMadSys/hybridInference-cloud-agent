# Staging end-to-end smoke

Use this runbook on a disposable staging deployment to verify the full path
from account sign-up to a real pull request. The check covers a Cloud Agent
control plane, one runner host, a model provider the tester holds a key for,
and a disposable GitHub repository.

If you only need a local product check without provider keys, start with the
[complete local Docker stack](../../deploy/local/README.md):

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

The local smoke creates the first administrator with the setup code the
control plane logs, signs in with the account session, reads administration,
and checks that nothing runs before the owner connects a model. It does not
test live provider behavior or GitHub publication; this staging runbook
checks those boundaries separately.

Earlier runs of this checklist, from the 2026-08 extraction, are kept as a
[dated record](../agents/reviews/2026-08-04-staging-smoke-runs.md). They used
the retired Gateway sign-in and do not show that any current deployment passes.

## What a passing run proves

A completed, passing run proves account sign-up and verification, waiting-list
approval, administration, a verified model connection, enrollment, sandbox
execution on the owner's connection, and GitHub publication for the recorded
revision and environment. A successful job alone does not prove publication:
the owner asks for it, and the publisher then processes an immutable workspace
snapshot asynchronously.

The run does not cover concurrency, host loss, or every isolation boundary.
Use [deployment reliability](deployment-reliability.md) and
[native context recall](context-recall.md) for those separate checks.

## 0. Before you start

- [ ] Use a disposable staging deployment with Docker/Compose, a checkout and
      enough memory for the selected CLI. The local all-five-CLI smoke recommends
      at least 8 GiB; public multi-tenant staging requires a Kata-capable host.
- [ ] Record the exact release SHA. API, host, sandbox manager and sandbox must
      satisfy the shared deployment/version checks.
- [ ] Start from a new, empty Agent database. Run
      `python -m cloud_agent.identity_namespace --check` against it first; it
      must report an empty database and change nothing
      ([cutover](upgrade-notes.md#independent-accounts-cutover)).
- [ ] Have a mail transport the deployment may use for test addresses you
      control (or turn email verification off for the run), and a provider API
      key the test account may spend.
- [ ] Choose a disposable GitHub repository and configure write/PR permission
      through the deployment's supported GitHub App or publishing credential.
      Record its base branch. Do not use a repository others depend on.

## 1. Configure

Use separate, private environment files for the control plane and host. Start
from [`control-plane/.env.example`](../../deploy/control-plane/.env.example) and
[`host/.env.example`](../../deploy/host/.env.example); [ENVIRONMENT.md](../../ENVIRONMENT.md)
is the reference.

Generate deployment-owned secrets once and keep them out of version control:

```bash
for name in AGENT_LOCAL_SESSION_SECRET AGENT_SETTINGS_ENCRYPTION_KEY \
  AGENT_CONTROL_TOKEN_SECRET AGENT_SOURCE_CONTROL_ENCRYPTION_KEY; do
  printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"
done
```

Keep the encryption keys stable so saved connections and settings remain
decryptable. None of them is shared with any other service.

- [ ] Set `AGENT_WEB_BASE_URL`, `AGENT_PUBLIC_API_URL`, the web base path and
      browser API path. Set no `GATEWAY_*`, `IDENTITY_*` or `AGENT_AUTH_MODE`:
      the control plane and `deploy.sh` refuse them.
- [ ] Set explicit Compose project names, sandbox image, enrolled host name,
      sandbox manager credential/reverse route, both tunnel destinations and keys,
      and the host's non-overlapping egress pool/resolver.
- [ ] Render configuration privately using the active environment file. Export
      `AGENT_DEPLOYMENT_SHA` for direct control-plane Compose commands; the
      deployment scripts supply it themselves. Rendered configuration contains
      secrets, so do not paste it into acceptance logs.

## 2. Start the control plane

Use the deployment process in [deployment reliability](deployment-reliability.md).
For a cold source deployment, its `prepare` and `prepare-database` phases build
images, start Postgres, check the identity namespace read-only, migrate, and
bootstrap the API and MCP listener needed for host readiness. Complete
activation after the host publishes readiness in section 4.

- [ ] Migrations exit successfully at the checkout's current head. The API does
      not create missing tables at startup.
- [ ] Main API `/healthz` responds on port 8000 by default.
- [ ] MCP-only `/healthz` responds on port 8001 by default; its tunnel targets
      that listener.
- [ ] The control-plane log has `agent_local_setup_pending` with a one-time
      setup code. Do not paste the code into the run log.

## 3. Accounts — setup, sign-up, approval

- [ ] Open `/setup/administrator`, exchange the setup code for the first
      administrator and sign in. Setup is closed afterwards: a second attempt
      answers 409 `setup_complete`.
- [ ] `GET /v1/session/me` returns the administrator's `id` and
      `agent_access: {allowed, permissions}` with `agent.admin`; there is no
      `auth_mode`. The session cookie is `__Host-cloud_agent_local_session`.
      Avoid recording cookies or token bodies in the run log.
- [ ] In a second browser, sign up a test account, follow the verification
      link, and apply. It sees the waiting list and cannot create a task.
- [ ] As the administrator, approve the application under **Admin →
      Accounts**. The test account now holds `agent.use`.

## 4. Enrol the host

- [ ] Issue `POST /admin/agent/runner-hosts/enroll-token` with the chosen `host`
      name using infrastructure admin access. The response reveals a single-use
      enrollment token; put it in the host's private configuration.
- [ ] Deploy the host through `deploy/host/deploy.sh` for the same target release.
      The host process prepares/attests the sandbox image, starts tunnels and
      sandbox manager, probes the MCP relay and publishes the completed-generation
      marker. Directly starting runner Compose containers does not complete this gate.
- [ ] `GET /admin/agent/runner-hosts` shows a fresh online heartbeat, expected
      deployment capability and reachable sandbox manager URL. Check the reverse
      sandbox manager route from the control plane.
- [ ] Complete control-plane activation and confirm its readiness check succeeds
      for the intended host/API SHA.

## 5. One job, all the way

- [ ] As the test account, add a model connection under **My settings → Model
      connections** with the provider's Base URL, the tester's key and a model
      id, and choose **Verify and save**. A failed check saves nothing.
- [ ] Connect/authorize the scratch repository under **Integrations**.
- [ ] Create a task on its intended base branch with a small verifiable change,
      such as adding a `CONTRIBUTING.md` containing a specified sentence.
- [ ] Observe `queued` then a host claim. Record job/attempt/host identifiers.
      The turn names its model and connection; no reasoning-effort control is
      offered for it.
- [ ] Confirm events stream to the browser and at least one model/tool call
      succeeds, and that the run's usage footer shows reported tokens and says
      the provider bills it.
- [ ] Confirm the turn succeeds with the expected file change. Nothing is
      published until you ask: choose **Create PR** in the conversation, wait
      for the publication to finish, and open the real **draft pull request**.
      Check target branch, author and diff; record its URL. Execution success
      alone is insufficient.
- [ ] List files and open a terminal on the retained workspace through the app.

## 6. The parts that only fail later

Run failure injection only in the disposable staging deployment and restore its
original settings after each case.

- [ ] **Cancellation:** cancel a separate running turn. Verify worker
      acknowledgment and terminal cancellation.
- [ ] **Revocation:** as the administrator, withdraw the test account's access
      while a turn runs. Its work stops, its streams end within 15 minutes, and
      a new task is refused; restoring access does not restart the old turn.
- [ ] **Fail closed:** stop the database briefly, then attempt fresh sign-in and
      a protected owner request. Neither may gain access; `/me` shows a
      temporary failure without exposing protected content. Restore it and
      verify recovery.
- [ ] **Control-plane loss:** stop the disposable API while a task runs. After
      restoring it, verify lease/reaper handling.
- [ ] Check live follow-up and same-container idle restore separately using
      [context recall](context-recall.md#verification-before-accepting-a-deployment).

## 7. Tear down

- [ ] Stop the disposable stacks using their recorded project names. Keep data
      until acceptance evidence has been captured. Volume deletion is permanent;
      use `down -v` only for a stack whose database is intentionally disposable.
- [ ] Delete scratch branches/repository and the test account's model
      connection, and revoke the test provider key and repository credentials
      when the deployment is retired.
- [ ] Restore any failure-injection settings on staging and record the result
      as described under [Log](#log).

## Log

Record results while running: the date and time (UTC), result and notes for
each numbered section, plus the source SHA, environment, host, job and PR
identifiers. Keep the record with the deployment's own acceptance evidence.
