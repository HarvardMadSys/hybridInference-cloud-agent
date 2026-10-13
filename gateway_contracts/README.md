# The gateway contracts this service depends on

The three YAML documents describe the Gateway HTTP APIs consumed by Cloud Agent.
They record the cross-repository boundary: Cloud Agent uses HTTP, never imports
Gateway modules, and never holds the Gateway's signing secrets.

| Document | Gateway router | Cloud Agent consumer |
|---|---|---|
| [identity.openapi.yaml](identity.openapi.yaml) | `serving/servers/routers/identity.py` | [identity.py](../apps/backend/cloud_agent/identity.py), [gateway.py](../apps/backend/cloud_agent/gateway.py), [api/session.py](../apps/backend/cloud_agent/api/session.py) |
| [inference-grants.openapi.yaml](inference-grants.openapi.yaml) | `serving/servers/routers/agent_grants.py` | [grants.py](../apps/backend/cloud_agent/grants.py) |
| [internal-lookups.openapi.yaml](internal-lookups.openapi.yaml) | `serving/servers/routers/internal_lookups.py` | [gateway.py](../apps/backend/cloud_agent/gateway.py) |

This directory also contains the JSON snapshot of Cloud Agent's own public API,
[described below](#the-one-document-here-that-is-ours).

## What these are, and what they are not

The YAML contracts are hand-written from Gateway source. They document the
agreed interface independently of a running deployment. Only endpoints consumed
by this service are included; they are not a complete Gateway API reference.

The application does not load these documents or validate every response against
OpenAPI at runtime. Consumer tests and review check agreement with the contracts.
Runtime clients handle compatible additions such as optional fields; the live
contract tests can still flag undeclared additions for documentation review.

## Source commits

Each document's `info.x-source` records the repository, branch, source files,
and, when available, commit used to write it. These references are provenance,
not a statement that your Gateway deployment runs that version.

A route check recorded on 2026-09-10 found all three routers on the Gateway's
`origin/main`:

| Document | Routes checked |
|---|---|
| `identity.openapi.yaml` | `/v1/identity/jwks`, `/v1/identity/code`, `/v1/identity/token` |
| `inference-grants.openapi.yaml` | Grant mint, renew, revoke, and usage routes |
| `internal-lookups.openapi.yaml` | `/internal/model-catalog` and `/internal/users/{user_id}/agent-access` |

That historical check does not establish readiness of a current deployment.
When a Gateway router changes, compare the contract with its merged source and
run the relevant consumer and live contract tests. The local suite cannot read
or verify files in the Gateway repository.

Agent admission is described in `internal-lookups.openapi.yaml`; it is not a
fourth contract. The client uses `/internal/users/{user_id}/agent-access` rather
than the user-status lookup. Profile uses the existing user-status endpoint
only to display the owner's current email; its role and status fields never
authorize Agent access. Before connecting an older Gateway, read
[agent access policy](../docs/agent-access-policy.md) for deployment policy,
rollout requirements, and fail-closed behavior.

## What these do not cover

Response schemas alone do not establish grant timing or the meaning of a
refusal. Keep these dependencies aligned with the Gateway:

- `GATEWAY_MAX_TTL_S = 900` and `GATEWAY_RENEWAL_STEP_S = 300` in `grants.py`
  mirror the Gateway's maximum and default grant TTL. The contract records them
  as `MAX_GRANT_TTL_S` and `DEFAULT_GRANT_TTL_S` under `x-constants`. Requests
  above the maximum are clamped, and renewal resets expiry using the renewal
  step, so a mismatch can shorten a live grant without a failed call.
- `DEAD_GRANT_REFUSALS` in `cloud_agent/workers/attempts.py` contains
  `unknown_grant`, `grant_not_live`, and `subject_unavailable`. The worker uses
  the error type to distinguish a dead grant from a Gateway configuration or
  authentication failure. HTTP status alone is insufficient: a 404 can mean
  either a dead grant or an unconfigured internal API.

`test_grant_constants_match_contract.py` compares the client constants with the
YAML document. `test_grant_lifecycle.py` checks them against the client's renewal
cadence and pins the dead-grant error types. The opt-in live grant lifecycle test
checks the deployed Gateway's timing; local tests cannot establish that it uses
these values.

## The one document here that is ours

[agent-control-plane.openapi.json](agent-control-plane.openapi.json) records the
API served by Cloud Agent. It is generated from `create_app().openapi()` and
committed with sorted keys so API changes can be reviewed.

`tests/unit/test_api_surface_contract.py` compares the served schema with this
snapshot and fails on disagreement. It does not regenerate the file. For an
intentional API change, use the command in the
[developer guide](../docs/development.md#changing-an-api), review the diff, and
commit it with the implementation change.

## Checking them

From the repository root, run the local document and snapshot checks:

```bash
uv run pytest -q \
  tests/unit/test_contracts.py \
  tests/unit/test_grant_constants_match_contract.py \
  tests/unit/test_api_surface_contract.py
```

The contract checks validate YAML documents as OpenAPI, verify that their
`x-pinned-by.consumer` test paths exist, and check that `/internal/` and `/v1/`
path literals in `gateway.py` and `grants.py` appear in a document. CI runs these
checks. They establish agreement within this repository, not agreement with a
live Gateway.

To check a deployment, set `AGENT_CONTRACT_GATEWAY_URL` and
`AGENT_CONTRACT_DISPATCH_TOKEN`, then run:

```bash
uv run pytest -q -m external \
  tests/external/test_gateway_access_contract.py \
  tests/external/test_gateway_contracts_live.py
```

These probes check JWKS keys and typed refusals using randomly named missing
accounts. The grant lifecycle case skips unless `AGENT_CONTRACT_SUBJECT` names
an account selected for the test. Setting that variable opts into minting,
renewing, and revoking a real grant; the test revokes it during cleanup.
See [smoke testing](../docs/smoke.md) for the wider deployment checks.
