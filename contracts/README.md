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

Each document names, in `info.x-source`, the branch and file it was written
from. The identity, grant and lookup routers were all still on unmerged
branches when these were written (see MIGRATION.md, "Cross-repo readiness"), so
the reference is a branch rather than a commit on `dev`. **When those land,
re-verify each document against the merge commit** — a review can change a
contract, and these were written against the version proposed rather than the
version accepted.

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
