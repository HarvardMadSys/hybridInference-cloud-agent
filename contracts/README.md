# The gateway contracts this service depends on

Three OpenAPI documents describing **the gateway's** side of the split, not
this service's. They exist because after the split those calls are the only
way this repository can answer questions it used to answer by importing, and a
cross-repo dependency that lives only in two codebases' heads is one that
drifts silently.

| Document | Gateway module | Consumed here by |
|---|---|---|
| [`identity.openapi.yaml`](identity.openapi.yaml) | `serving/servers/routers/identity.py` | `cloud_agent/identity.py` (E7), `api/session.py` (E8) |
| [`inference-grants.openapi.yaml`](inference-grants.openapi.yaml) | `serving/servers/routers/agent_grants.py` | `cloud_agent/grants.py` (E9) |
| [`internal-lookups.openapi.yaml`](internal-lookups.openapi.yaml) | `serving/servers/routers/internal_lookups.py` | `cloud_agent/gateway.py` (E4, E6, E7, E8) |

## What these are, and what they are not

**Hand-written from the gateway's source, not generated from a running
gateway.** Generating them would make the document agree with whatever is
deployed, which is the opposite of what a contract is for: the value is that it
says what was *agreed*, so a gateway that stops honouring it produces a
disagreement someone can see rather than a client that quietly breaks.

**They are not validated against at runtime.** Nothing in this service loads
them. A client that drifts from its contract is caught by review and by the
tests named in each document's `x-pinned-by`, not by a schema check on every
call — a wire-level validator here would fail closed on a gateway that added an
optional field, which is a compatible change.

**Only the endpoints this service calls appear.** The gateway's public API is
much larger; none of the rest is a dependency of this repository, and listing
it would invite someone to treat this directory as a mirror of the gateway.

## Source commits

Each document names, in `info.x-source`, the branch and file it was **written
from**. That is a historical record, not a claim about where the code lives now,
and it is kept because a contract's value is that it says what was *agreed* at a
known point — a later reader can diff the two.

Two of the three have landed on the gateway's `main`; the third is in flight:

| Document | Status |
|---|---|
| `identity.openapi.yaml` | Landed — `apps/backend/serving/servers/routers/identity.py` serves `/v1/identity/{jwks,code,token}` on the gateway's `main`. |
| `inference-grants.openapi.yaml` | Landed — `apps/backend/serving/servers/routers/agent_grants.py` serves mint / renew / revoke / usage on the gateway's `main`. |
| `internal-lookups.openapi.yaml` | **In flight** — the gateway's `main` still serves `/internal/users/{user_id}/status`; the `/internal/users/{user_id}/agent-access` endpoint this document describes is on its `dev`. See [`docs/agent-access-policy.md`](../docs/agent-access-policy.md) for the rollout, the compatibility path, and the fail-closed behaviour against an older gateway. |

**When the gateway changes one of these routers, re-verify the document against
the merged source.** A review can change a contract, and a document describing
the version proposed rather than the version accepted is exactly the silent
drift this directory exists to prevent. Nothing here checks that for you —
`tests/unit/test_contracts.py` pins this repository's side only.

## What these do not cover

Two dependencies never appear in a response body, so no schema here can check
them:

- **The gateway's own clamps.** `grants.py` mirrors `GATEWAY_MAX_TTL_S` (900)
  and `GATEWAY_RENEWAL_STEP_S` (300) from the gateway's `serving/grants.py`. A
  request for more than the maximum is clamped *down* silently, and every
  renewal re-times the grant to the gateway's step — so either one would shorten
  a live grant without any call failing. The document declares both under
  `x-constants`, but that is a declaration rather than a check: the number also
  lives in the gateway and in `grants.py`, and comparing the copies is left to
  tests rather than to anything in this directory.
- **Which refusals mean "this grant is dead".** `DEAD_GRANT_REFUSALS`
  (`unknown_grant`, `grant_not_live`, `subject_unavailable`) mirrors the
  gateway's error *types*. The status code alone is not enough: the gateway
  answers 404 for both a dead grant and an unconfigured internal API, and 401
  for a rotated dispatch token.

`tests/unit/test_grant_lifecycle.py` pins `grants.py`'s copies against each
other. It cannot reach the gateway, so it would not notice the gateway changing
one of them: the symptom is a sandbox losing its authorization mid-run — far
from the constant that moved.

## Checking them

```bash
uv run python -m openapi_spec_validator contracts/*.yaml
```

CI runs exactly that. It checks the documents are well-formed OpenAPI; it
cannot check they describe the gateway, which is what the `x-source` references
and the paired tests are for.

Agent admission now also depends on `/internal/users/{user_id}/agent-access`.
See [the policy rollout and verification notes](../docs/agent-access-policy.md)
before upgrading the Agent against an older Gateway.
