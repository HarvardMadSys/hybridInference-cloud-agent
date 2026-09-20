# The gateway contracts this service depends on

Three OpenAPI documents describing **the gateway's** side of the split, not
this service's. They exist because after the split those calls are the only
way this repository can answer questions it used to answer by importing, and a
cross-repo dependency that lives only in two codebases' heads is one that
drifts silently.

| Document | Gateway module | Consumed here by |
|---|---|---|
| [`identity.openapi.yaml`](identity.openapi.yaml) | `serving/servers/routers/identity.py` | [`identity.py`](../apps/backend/cloud_agent/identity.py), [`api/session.py`](../apps/backend/cloud_agent/api/session.py) |
| [`inference-grants.openapi.yaml`](inference-grants.openapi.yaml) | `serving/servers/routers/agent_grants.py` | [`grants.py`](../apps/backend/cloud_agent/grants.py) |
| [`internal-lookups.openapi.yaml`](internal-lookups.openapi.yaml) | `serving/servers/routers/internal_lookups.py` | [`gateway.py`](../apps/backend/cloud_agent/gateway.py) |

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

All three are served by the gateway's `main`. Checked 2026-09-10 by reading the
route decorators at `origin/main`, not by a schema re-review:

| Document | Served by |
|---|---|
| `identity.openapi.yaml` | `apps/backend/serving/servers/routers/identity.py` — `/v1/identity/{jwks,code,token}`. |
| `inference-grants.openapi.yaml` | `apps/backend/serving/servers/routers/agent_grants.py` — mint / renew / revoke / usage. |
| `internal-lookups.openapi.yaml` | `apps/backend/serving/servers/routers/internal_lookups.py` — `/internal/model-catalog` and `/internal/users/{user_id}/agent-access`; it also still serves `/internal/users/{user_id}/status`, which this document does not describe. |

**Which branch a router is on is not the durable question.** `main` is a
fast-forward mirror of `dev` (`.github/workflows/sync-main.yml`), so an endpoint
that is on `dev` alone is a lag of hours to days rather than a state worth
recording — a table that says so goes stale between writing and review. What a
reader needs is whether *the gateway they are pointing at* serves the route: an
older deployment may not, and
[`docs/agent-access-policy.md`](../docs/agent-access-policy.md) carries the
rollout, the compatibility path, and the fail-closed behaviour in that case.

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
  `x-constants` — as `MAX_GRANT_TTL_S` and `DEFAULT_GRANT_TTL_S`, deliberately
  not under this client's names — but that is a declaration rather than a check:
  each number also lives in the gateway and in `grants.py`, and comparing the
  copies is left to tests rather than to anything in this directory.
- **Which refusals mean "this grant is dead".** `DEAD_GRANT_REFUSALS`
  (`unknown_grant`, `grant_not_live`, `subject_unavailable`) mirrors the
  gateway's error *types*. The status code alone is not enough: the gateway
  answers 404 for both a dead grant and an unconfigured internal API, and 401
  for a rotated dispatch token.

`tests/unit/test_grant_lifecycle.py` pins both mirrors against this service's
own cadence — the requested lifetime must fit under the maximum, and the renewal
loop and the give-up must fit inside the step. It cannot reach the gateway, so it
would not notice the gateway changing one of them: the symptom is a sandbox
losing its authorization mid-run — far from the constant that moved.

## The one document here that is ours

[`agent-control-plane.openapi.json`](agent-control-plane.openapi.json) is not a
gateway contract. It is the API **this** service serves, generated from
`create_app().openapi()` and committed key-sorted so that a change to the
published shape is a diff somebody chose. `tests/unit/test_api_surface_contract.py`
compares the served document with the committed file and fails when they
disagree, catching a route changing its parameters or disappearing even if
the behavioural tests still pass. The test does not overwrite the snapshot.

It is checked in rather than produced during CI on purpose: a document generated
at test time agrees with whatever the code does by construction, which is the
opposite of a contract. Regenerate it deliberately, in the pull request that
changes the API, with the command in the
[developer guide](../docs/development.md#changing-an-api) or that test's failure
message.

## Checking them

```bash
uv run pytest -q tests/unit/test_contracts.py
```

CI runs these same tests. They validate each document as OpenAPI, check that
every consumer test path cited in an `x-pinned-by` list exists, and verify that
every `/internal/` or `/v1/` path literal in `gateway.py` and `grants.py` appears
in some document. They cannot check whether the documents describe the
gateway's current implementation; that is what the `x-source` references and
the paired tests are for.

A fourth family is consumed but not documented here as a separate document:
`/internal/users/{user_id}/agent-access`, which lives in
`internal-lookups.openapi.yaml` and replaced the retired
`/internal/users/{id}/status`. See
[the policy rollout and verification notes](../docs/agent-access-policy.md)
before upgrading the Agent against an older Gateway.
