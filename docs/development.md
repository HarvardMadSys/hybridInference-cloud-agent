# Developer guide

Use this guide to set up a checkout, find the code that owns a change, and
choose the checks it needs. Read [AGENTS.md](../AGENTS.md) for repository rules
and [CONTRIBUTING.md](../CONTRIBUTING.md) for the pull-request workflow. The
[README](../README.md) describes the product and deployment topology.

## Set up a checkout

Backend commands run from the repository root. Install Python 3.12 and
[`uv`](https://docs.astral.sh/uv/), then create the environment:

```bash
make setup-dev
make check
```

`make setup-dev` creates `.venv` and installs the project and development
dependencies with `uv sync`. It defaults to Python 3.12, the version used in
CI. The project supports Python 3.12 and 3.13. Run Python tools with `uv run`
from this checkout; package discovery already includes `apps/backend/`, so
you do not need to set `PYTHONPATH` for each package.

For frontend work, use Node.js 22, matching CI, and install the locked
dependencies:

```bash
cd apps/frontend
npm ci
```

To exercise sign-in, migrations, host enrollment, and real Agent CLIs together,
use the [complete local Docker stack](../deploy/local/README.md). From the
repository root:

```bash
./deploy/local/dev.sh up
./deploy/local/dev.sh smoke
```

The local-stack guide covers Docker prerequisites, ports, logs, stop/resume,
and running multiple isolated checkouts.

## Find the code that owns a change

The backend is installed as three packages under `apps/backend/`:

| Package | Owns |
|---|---|
| [`cloud_agent/`](../apps/backend/cloud_agent/) | Control plane: HTTP APIs, admission and scheduling, persistence, Gateway adapters, and publication. |
| [`cloud_agent_host/`](../apps/backend/cloud_agent_host/) | Host execution: claims, retained sandboxes, workspace/session broker, and Agent CLI adapters. |
| [`cloud_agent_shared/`](../apps/backend/cloud_agent_shared/) | Protocol versions, capability names, shared payloads and errors, profiling collection, and shared Git/archive algorithms. |

Within the control plane, HTTP handlers live in `api/`, reusable flows in
`application/`, persistence interfaces in `ports/`, and database implementations
in `storage/`. `workers/` owns background maintenance and reconciliation;
`bootstrap/` constructs resources and starts and stops them. `app.py` assembles
the main API and the separate MCP-only listener.

Use these starting points for common changes. Backend paths below are relative
to `apps/backend/`.

| Change | Start here |
|---|---|
| Job or conversation HTTP behavior | `cloud_agent/api/jobs.py`, `catalog.py`, `turns.py`, or `threads.py`. `api/routes.py` mounts the nine agent surfaces. |
| A new caller of task creation | `cloud_agent/application/job_admission.py`; `api/service_jobs.py` is an existing caller. |
| Workspace files, terminals, or Git views | `cloud_agent/api/workspace.py` and `workspace_broker_client.py`; host handling is in `cloud_agent_host/workspace_broker.py`. |
| A query or persistent field | The relevant `cloud_agent/ports/` interface and `storage/` implementation, plus an additive revision under `migrations/versions/` for schema changes. |
| Compute-resource lifecycle | `cloud_agent/application/compute_resources.py`, `compute_reconcile.py`, `workers/compute_resources.py`, and `providers/compute/`. |
| CLI behavior or version compatibility | `cloud_agent_host/runtimes.py` and `drivers.py`; shared pins are in `cloud_agent_shared/shared_protocol.py`, image pins in `deploy/sandbox/Dockerfile.agent-sandbox`. |
| Browser UI or a BFF route | `apps/frontend/src/`; browser API clients are in `src/lib/api/`. |

Keep changes with their owning module. Extract a flow when it needs an
independent test boundary, or a rule when multiple entry points share it.

## Keep the dependency boundaries

The deployment images ship different packages, so an import that works in a
checkout can still fail in production. Follow these boundaries:

- HTTP adapters call application flows and the storage capabilities they need.
  Application flows and workers must not import route modules. Some application
  flows use FastAPI's `HTTPException`; follow the actual interfaces rather than
  assuming that package is framework-independent.
- `ports/` defines capabilities without importing their implementations.
  `Depends(get_storage)` returns the `Storage` assembly: use the surface's
  narrow protocol and call a capability such as `store.audit` or
  `store.host_pool`. Repository methods belong to those capabilities.
- The host must not import the control plane, and the shared package must
  import neither implementation package. Shared Git subprocess and archive I/O
  belongs in `cloud_agent_shared/git/`.
- The control plane has three remaining host runtime-registry imports, pinned
  by [the import-boundary tests](../tests/unit/test_import_boundary.py). This
  transition allowlist may only shrink. Put new shared facts in
  `cloud_agent_shared/`.
- The Gateway is reached over HTTP only. Use the contracts in
  `gateway_contracts/`; do not import `serving.*`, access the Gateway database,
  or use its `API_KEY_SECRET` or `JWT_SECRET_KEY`. This service uses its own
  `AGENT_CONTROL_TOKEN_SECRET` and `AGENT_SESSION_SECRET`.

Bootstrap owns cross-component wiring. Its existing calls into
`api/mcp_relay.py` for MCP session lifecycle are a specific exception; application
flows and workers must keep the route-module boundary.

Changes to sandbox networking, credential scope, or isolation also need review
against [the security boundaries in AGENTS.md](../AGENTS.md#7-security-boundaries--do-not-relax-without-review).
Keep MCP tool calls on the MCP-only listener and long-lived credentials out of
sandboxes. Inference spend remains subject to Gateway per-user quota; there is
no per-task dollar budget.

## Run the relevant checks

During backend development, run the tests that exercise the changed behavior.
Before opening a pull request, run the complete required backend gate:

```bash
uv run pytest -q tests/servers/test_agent_jobs_api.py
make check
```

| Command | Checks |
|---|---|
| `make format` | Ruff formatting and automatic lint fixes; changes files. |
| `make lint` | Ruff format check, Ruff lint without fixes, and pydocstyle. |
| `make typecheck` | Mypy over all three backend packages. |
| `make test` | Pytest excluding `external` and `dbtest`, with four workers and each file kept on one worker. |
| `make check` | `format`, `lint`, `typecheck`, then `test`. Review any automatic fixes before committing. |
| `make test-db` | The PostgreSQL-backed `dbtest` tier. |
| `make test-all` | All tests except `external`, including `dbtest`. |

While this repository remains private, pull requests from branches in this same
repository temporarily run on the existing self-hosted pools. This explicitly
trusts everyone who can push such a branch with persistent-runner access; it is
a hosted-minute workaround, not an isolation boundary. Fork pull requests stay
on disposable GitHub-hosted VMs: ARM64 artifacts use `ubuntu-24.04-arm`, and
AMD64 artifacts use `ubuntu-24.04`. Pushes to `dev` and `main`, and authorized
manual dispatches, keep the same self-hosted routing as before. Every PR still
has only a read token, checkout does not persist it, image jobs use anonymous
registry pulls, and the per-PR buildx directory and builder name remain isolated
from the persistent builders used by trusted push and dispatch jobs. Real Kata
host qualification remains excluded from every PR and runs only on trusted
`dev` events.

This temporary policy must be removed before the repository is open-sourced.
GitHub issue [#464](https://github.com/HarvardMadSys/hybridInference-cloud-agent/issues/464)
tracks the release-blocking work: route every PR back to a disposable isolated
runner, then validate the runner-group controls below against an untrusted fork.
Repository visibility must not change until that issue is closed and its
acceptance check has passed.

Before enabling public fork workflows, configure GitHub's runner groups to allow
persistent pools only for the trusted workflow references on `refs/heads/dev`
and `refs/heads/main`, including the publication and manual synchronization
workflows. Deny PR merge/head refs and other workflow files. Verify those rules
with an untrusted fork workflow edit that requests a persistent runner: it must
remain ineligible. A fork can edit its workflow YAML, so the checked-in runner
expressions cannot enforce this restriction themselves. If the organization
cannot restrict runner groups by workflow reference, remove these persistent
runners from the repository's eligible groups and run trusted CI/publication
in a separate restricted repository, or replace them with credential-free
single-job disposable runners. Fork approval settings alone are insufficient:
allowing a test run must never grant access to publication's hosts or credentials.
These are GitHub administrator settings; repository tests cannot configure or
verify them.

The default backend test tier uses four workers in both CI and `make test`,
keeping each test file on one worker. Override the worker count when needed;
zero disables parallel execution for debugging:

```bash
make check PYTEST_WORKERS=0
```

Markers select the test tier. For example, `tests/servers/` contains HTTP-level
tests that can run without a deployed server. Tests marked `external` state
their prerequisites in the test module; these may include Docker, a real CLI,
or a configured Gateway. CI runs selected external checks in addition to the
default tier.

Database changes need `make test-db` against a dedicated test database. Fixtures
read `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_NAME`, `TEST_DB_USER`, and
`TEST_DB_PASSWORD`; `TEST_DB_NAME` must contain `_test_`. Check the result for
skipped tests: an unavailable database can cause fixtures to skip rather than
fail. Parallel workers need separate pre-created databases. See
[ENVIRONMENT.md](../ENVIRONMENT.md#database) for defaults and worker naming.

Deployment tests read sandbox CLI version settings. If your surrounding
development tool exports a different `CODEX_VERSION`, remove that inherited
value for the check:

```bash
env -u CODEX_VERSION make check
```

For frontend changes, run the following from the repository root:

```bash
cd apps/frontend
npm ci
npm run lint
npm run format:check
npm run type-check
npm run test
```

CI also runs these build guards. Run the relevant one locally when changing
server-only imports or runtime site configuration:

```bash
node scripts/check-server-only-guard.mjs
node scripts/check-runtime-site-config.mjs
```

The Makefile covers backend checks. CI runs the frontend checks separately and,
when image changes are selected, builds and smoke-tests both the root and
`/agents` web variants. See [CONTRIBUTING.md](../CONTRIBUTING.md#continuous-integration)
for the wider CI gate.

## Changing an API

`gateway_contracts/` contains two different kinds of HTTP contract:

- `*.openapi.yaml` describes the Gateway APIs this service consumes. When a
  client starts reading a new field or calling a new endpoint, verify the
  corresponding Gateway source and update the document in the same PR.
- `agent-control-plane.openapi.json` records the API this service publishes.
  An intentional API change requires a reviewed snapshot update in the same PR.

Regenerate the control-plane snapshot from the repository root:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

from cloud_agent.app import create_app

Path("gateway_contracts/agent-control-plane.openapi.json").write_text(
    json.dumps(create_app().openapi(), sort_keys=True, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)
PY

uv run pytest -q tests/unit/test_api_surface_contract.py tests/unit/test_contracts.py
```

Review the generated diff and explain any compatibility implications. The
surface tests also reject duplicate registrations and overlaps between the
nine agent routers. A snapshot update records a change; the behavioral tests
and review establish whether it is compatible. The
[contract guide](../gateway_contracts/README.md) explains what these checks can
verify and what still needs comparison with Gateway source.

## Packaging and documentation

When adding or moving a package, review package discovery in `pyproject.toml`,
mypy and linter roots, Docker `COPY` paths, and the path classifier in
`.github/workflows/ci.yml`. Verify the built images contain their dependencies;
checkout imports alone cannot establish that. CI builds and starts selected
images, and its classifier tests derive shared package paths from the
Dockerfiles.

Schema changes use explicit additive Alembic revisions. The current chain
starts at `migrations/versions/0001_schema_baseline.py`; keep schema creation out
of service startup. There is no SQLAlchemy model metadata for autogeneration.
For databases stamped with the pre-baseline chain, follow
[the rebuild runbook](operations.md#rebuilding-the-agent-database).

Update the guide that owns any changed configuration, command, or behavior:

| Topic | Document |
|---|---|
| Environment variables and runtime pins | [ENVIRONMENT.md](../ENVIRONMENT.md) |
| Operation and troubleshooting | [operations.md](operations.md) |
| Local development stack | [Local-stack guide](../deploy/local/README.md) |
| Release artifacts and validation | [Release guide](../deploy/release/README.md) |

Designs, completed migration checklists, and acceptance records document their
own baseline. Preserve that history and update current-use guides when the
implementation changes.
