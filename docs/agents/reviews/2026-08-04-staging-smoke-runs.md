# Staging end-to-end smoke: recorded runs (2026-08)

Moved on 2026-10-16 from the [staging smoke runbook](../../developer/smoke.md).
These are the runs recorded while the service was extracted from the gateway
repository. They describe those revisions and that staging environment, not a
current deployment.

## Recorded staging status

**Recorded staging status:** On 2026-08-06, job `ajob_8b0be5f9e7ff9ad5`
verified enrollment, job creation and remote claim, sandbox manager workspace access,
tool-calling execution, success, four grant renewals, and revocation after
settlement. That record did not verify real PR publication, mid-run
cancellation, or the full fail-closed sequence. See
[MIGRATION.md](../../../MIGRATION.md),
[cutover.md](../specs/cutover.md) and the partial run below. Newer local lifecycle
checks do not fill those staging acceptance gaps.

## Historical log

Blank cells are steps those runs did not verify.

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
> exercised `internal_lookups.user_status`, which this service then stopped
> calling for admission. (The Profile page later used it again, only to read
> the account email; `gateway_contracts/internal-lookups.openapi.yaml` records
> that use.) Admission now comes from
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
