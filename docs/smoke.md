# Single-host end-to-end smoke (task E11)

For a deterministic, provider-key-free local product run, use
[`deploy/local/dev.sh`](../deploy/local/README.md) first. That Docker-only stack
has been executed end to end against the real browser/control-plane/runner and
all five pinned Agent CLI binaries, including `NEW → LIVE`, idle `docker stop`,
and same-container `RESTORED`. This document remains the separate staging
gateway + real GitHub publishing runbook; the local substitute intentionally
does not claim to prove an external provider or pull-request publication.
Bring up the control plane and one runner host on a single machine, point them
at a **staging** gateway, and drive one real job from an empty workspace to a
pull request on a scratch repository.

> **The external staging-to-GitHub runbook below remains partly executed.** See
> the dated log at the bottom for exactly what reached staging. The Docker-only
> local stack linked above now separately proves image builds, migrations,
> browser/control-plane/host wiring, retained sandboxes, and all five CLI
> session paths; it does not prove an external provider or real pull-request
> publication.
>
> The remaining staging steps were written from code and deployment files.
> Until someone runs them and fills in the log, treat each as a claim to check,
> not a route known to work — and fix any incorrect step in the same sitting.
> The still-unverified boundary here is the live external gateway/GitHub path,
> not whether the standalone images and retained topology can run locally.

## What this proves, and what it does not

**Proves:** the two processes start, find each other, authenticate to a gateway
they do not share secrets with, and can carry one job from creation to a
published PR — including the parts that only exist after the split (identity
sign-in, an inference grant, a host enrolment).

**Does not prove:** anything about migrated data (that is H1 and the cutover
smoke in [cutover.md](cutover.md)), anything about concurrency or host loss
(G7), or that the gateway's side is deployed — at the time of writing the
gateway contracts live in unmerged pull requests, so **the very first thing to
check is that they are live on the staging gateway you are pointing at**.

## 0. Preconditions

- [ ] A machine with Docker, ≥8 GB RAM, and a checkout of this repository.
- [ ] **The staging gateway serves the contracts it still owns.** JWKS must
      answer:
      ```bash
      curl -sS -o /dev/null -w '%{http_code} %{url_effective}\n' \
        "$GATEWAY_BASE_URL/v1/identity/jwks"
      ```
      Then, with the dispatch token, check
      `/internal/model-catalog?user_id=<a real user>` and
      `/internal/users/<id>/status`. The old `/internal/mcp-registry` endpoint
      was removed: MCP configuration is owned by this service and requiring
      that path would make the smoke fail before it began. A 404 on either
      remaining gateway lookup means the gateway side has not landed; **stop
      here** — everything below will fail closed correctly.
- [ ] A scratch GitHub repository this run may open pull requests against, and
      a token that can. Not a repository anyone depends on: the point of this
      exercise is that a machine writes to it.
- [ ] A staging gateway account you can sign in with.

## 1. Configure

Generate this deployment's own secrets. **They are its own** — none is derived
from anything the gateway holds, and if you find yourself copying a value out
of the gateway's environment, that is the split being undone:

```bash
for name in AGENT_SESSION_SECRET AGENT_CONTROL_TOKEN_SECRET AGENT_SOURCE_CONTROL_ENCRYPTION_KEY; do
  printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"
done
```

The dispatch token is the one value shared with the gateway, and it must be the
same string on both sides and different per environment:

```bash
printf 'GATEWAY_GRANT_DISPATCH_TOKEN=%s\n' "$(openssl rand -hex 32)"
```

Write those plus the gateway URLs and the Postgres password into an env file.
[ENVIRONMENT.md](../ENVIRONMENT.md) is the full list; the compose file refuses
to render without the required ones, so an omission fails immediately rather
than at first use.

- [ ] `docker compose -f deploy/control-plane/docker-compose.yml --env-file <file> config`
      renders without error.

## 2. Start the control plane

```bash
docker compose -f deploy/control-plane/docker-compose.yml --env-file <file> up -d
```

- [ ] `migrate` exits 0. It runs `alembic upgrade head`; the service creates no
      tables itself, so a failure here means the API will not start — which is
      the intended behaviour, not a race to wait out.
- [ ] `curl -sS localhost:8000/healthz` answers.
- [ ] The log shows the reaper starting. Without it nothing recovers a lost
      attempt, and its absence is invisible until a job hangs.

## 3. Sign in — the identity flow end to end

This is the step that exercises C1–C4 and E7/E8 together, and the first place a
missing gateway contract shows up as a human-visible failure.

- [ ] Open the web UI, choose sign-in, and land on the **gateway's**
      `/authorize` page.
- [ ] Approve. You return to this service with a session.
- [ ] `GET /v1/session/me` reports your gateway user id and role.
- [ ] Nothing in the control plane's logs or database contains a gateway
      session token. It exchanged a code for a short-lived identity token,
      minted its own cookie, and kept neither.

## 4. Enrol the host

The runner does not authenticate with a shared password; it is issued a
credential bound to the name an admin chose for it.

- [ ] `POST /admin/agent/runner-hosts/enroll-token` with the host's name,
      using `ADMIN_TOKEN`.
- [ ] Start the host stack from
      `deploy/host/docker-compose.sandbox.yml` with that token.
- [ ] `GET /admin/agent/runner-hosts` shows the host `online` with a broker URL.
- [ ] Confirm the token cannot be reused under a different name — register a
      second machine under a typo of the first and watch it be refused
      **without spending the token**. This is the protection that stops a typo
      evicting a live host.

## 5. One job, all the way

- [ ] Connect the scratch repository under Integrations.
- [ ] Create a job against it with a small, checkable instruction ("add a
      CONTRIBUTING.md saying X").
- [ ] It reaches `queued`, then the host claims it.
- [ ] **A grant was minted for the attempt.** The control plane's log records
      it; the gateway's `agent_grants` row names the same external job and
      attempt. This is the moment the split is doing its job: the sandbox holds
      a capability the gateway issued, and no key of the gateway's.
- [ ] Events stream to the browser over SSE while it runs.
- [ ] The agent's model calls succeed — proving the grant's `allowed_models`
      clamp resolved to something the account can actually reach.
- [ ] It reaches `succeeded`, and **a pull request is open on the scratch
      repository**, authored by the configured git identity.
- [ ] Open a terminal on the finished workspace; files list.

## 6. The parts that only fail later

Cheap to check now, expensive to discover at cutover:

- [ ] **Renewal.** Leave a job running longer than the grant TTL (300 s by
      default). It must keep working. If it dies at ~5 minutes, the renewal
      loop is not running and every long job on this deployment is broken.
- [ ] **Revocation.** Cancel a job mid-run; the grant is revoked within a
      reaper pass rather than living to its TTL.
- [ ] **Fail-closed.** Point `GATEWAY_BASE_URL` at a port nothing listens on
      and restart. Sign-in must refuse and job creation must refuse. If either
      *succeeds*, something is failing open and that is a stop-everything
      finding, not a smoke-test nit.
- [ ] **The gateway holds no attempt state.** Stop the control plane with a job
      running. Nothing at the gateway ends the attempt; the grant simply
      expires. This is the design — the fence is here, not there.

## 7. Tear down

- [ ] `docker compose ... down -v` on both stacks.
- [ ] Delete the scratch repository's branches, or the repository.
- [ ] Revoke the dispatch token and the scratch repo token if this machine is
      not staying.

## Log

Fill this in **while running**, not afterwards. An entry written from memory is
the thing this file exists to replace.

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

Run **without a container runtime** — no Docker daemon was available — so the
service was started directly against a local Postgres instead of through
`deploy/control-plane/docker-compose.yml`. That covers the process and the
schema; it does not cover the image, the compose wiring, or anything with a
sandbox in it.

**Verified**

- `alembic upgrade head` against an empty database reaches `0001_schema_baseline`
  and creates 24 tables (re-verified on the rewritten baseline, 2026-09-10). The bare `postgresql://` DSN the compose file produces
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

Called `internal_lookups.user_status` and `internal_lookups.model_catalog`
directly, with stub stores, and fed what came back to this service's readers.

| Case | Result |
|---|---|
| active user | `_may_sign_in` → yes |
| suspended user | refused |
| unknown user — the response omits `status`, `role` and `email` | refused, and the later `.get()` reads return the defaults rather than raising |
| catalog `models` / `aliases` | parsed as expected |

**Two drifts found in `contracts/internal-lookups.openapi.yaml`**, both fixed:

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
| Sections 3–7 | A staging gateway serving the identity and grant contracts, a host, and a scratch repository. |
| The two-relay egress topology | Two machines and the tunnels between them. `docker compose config` renders it and the network assertions hold, which is not the same as traffic flowing. |

**Corrections made to this runbook during the run:** none. Sections 1 and 2
were accurate as written.
