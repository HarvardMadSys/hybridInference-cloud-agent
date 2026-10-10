# Developer guide

This guide describes the current source layout and the workflow for changing
it. The [README](../README.md) explains the product and deployment topology;
[CONTRIBUTING.md](../CONTRIBUTING.md) covers pull requests.

## Set up a checkout

Run backend commands from the repository root. Install Python 3.12 and `uv`,
then create the environment and run the quality gate:

```bash
make setup-dev
make check
```

`make setup-dev` uses Python 3.12 by default, matching CI. The Python packages
are installed from `apps/backend/`; their import names remain `cloud_agent`,
`cloud_agent_host`, and `cloud_agent_shared`. Use `uv run` from this checkout
instead of adding individual source directories to `PYTHONPATH`.

Frontend work uses Node.js 22, matching CI, and npm. Install its dependencies
with `npm ci` in `apps/frontend/`. For an end-to-end local environment, including
sign-in, a database, an enrolled host, and the sandbox lifecycle, use the
[local Docker stack](../deploy/local/README.md). Its guide covers prerequisites,
startup, smoke tests, and running multiple isolated stacks.

## Find the code that owns a change

The three backend packages are under `apps/backend/`:

| Package | Responsibility |
|---|---|
| [`cloud_agent/`](../apps/backend/cloud_agent/) | Control plane: HTTP APIs, job admission and scheduling, persistence, Gateway adapters, and publication. |
| [`cloud_agent_host/`](../apps/backend/cloud_agent_host/) | Host execution: claims, sandbox lifecycle, workspace broker, and Agent CLI adapters. |
| [`cloud_agent_shared/`](../apps/backend/cloud_agent_shared/) | Protocol versions, capability names, payload definitions, shared errors, and the `git/` algorithms used by both sides. |

Within the control plane:

| Area | Responsibility |
|---|---|
| [`api/`](../apps/backend/cloud_agent/api/) | HTTP routes, request dependencies, authorization, and response mapping. `routes.py` mounts the nine agent surfaces; handlers live in their surface modules. Other surfaces, such as admin and MCP, have their own routers. |
| [`application/`](../apps/backend/cloud_agent/application/) | Reusable flows and rules: task creation/admission, native-session Fork materialization, and residency credentials. |
| [`ports/`](../apps/backend/cloud_agent/ports/) | `Protocol` interfaces describing the persistence capabilities callers need. |
| [`storage/`](../apps/backend/cloud_agent/storage/) | Database implementations and the `Storage` object that assembles the capabilities. |
| [`workers/`](../apps/backend/cloud_agent/workers/) | Background attempt/grant maintenance and snapshot loops. |
| [`bootstrap/`](../apps/backend/cloud_agent/bootstrap/) | Resource construction, listener lifespan, and starting/stopping background tasks. |
| [`app.py`](../apps/backend/cloud_agent/app.py) | Main and MCP-only application factories and listener middleware. |

Common starting points:

| Change | Start here |
|---|---|
| A job or conversation HTTP operation | `cloud_agent/api/jobs.py`, `catalog.py`, `turns.py`, or `threads.py`, according to the surface. |
| A second entry point for task creation | `cloud_agent/application/job_admission.py`; `api/service_jobs.py` shows an existing caller. |
| Workspace files, terminals, or Git views | `cloud_agent/api/workspace.py` and `cloud_agent/workspace_broker_client.py`; host-side handling lives in `cloud_agent_host/workspace_broker.py`. |
| A query or persistent field | The relevant `cloud_agent/ports/` interface and `storage/` implementation; schema changes also need an additive revision under `migrations/versions/`. |
| Runtime behavior or CLI compatibility | `cloud_agent_host/runtimes.py` and the relevant driver; shared pins are in `cloud_agent_shared/shared_protocol.py` and image pins in `deploy/sandbox/Dockerfile.agent-sandbox`. |
| Browser UI or a BFF route | `apps/frontend/src/`; API clients are under `lib/api/`. |

Backend paths in this table are relative to `apps/backend/`. Keep a change in
the module that owns it. Extract a flow when it has a useful independent
test boundary, or a rule when multiple entry points share it; a long handler
alone does not require a new service layer.

## Keep the dependency boundaries

- HTTP adapters call application flows and the storage capabilities they need.
  Application flows and workers must not import route modules. Some flows
  still use FastAPI's `HTTPException`; the application package is not a fully
  framework-independent domain layer.
- `ports/` describes capabilities without importing their implementations.
  `Depends(get_storage)` returns the capability assembly, so follow the
  surface's narrow storage protocol and call, for example, `store.audit` or
  `store.host_pool`; a repository method is not a method on the assembly.
- The host must not import the control plane: the runner image does not ship
  it. The shared package must import neither implementation package. Within
  the shared package, the `git/` subpackage is the designated home for the
  subprocess and archive I/O used by the shared Git algorithms.
- Control-plane imports from the host are limited to three runtime-registry
  queries, recorded in `tests/unit/test_import_boundary.py`. Shared facts
  belong in `cloud_agent_shared/`; adding to that allowlist is a dependency
  change to review, not a routine way to fix an import failure.
- The Gateway is a separate service reached over HTTP. Its contract mirrors
  are in `gateway_contracts/`; its database, Python modules, and signing
  secrets are not dependencies this service may take.

Bootstrap owns cross-component wiring. Its existing MCP session lifecycle
calls into `api/mcp_relay.py` remain an exception; they are not a pattern for
application or worker code to copy.

## Run the relevant checks

For a fast backend iteration, select the tests that exercise the changed
behavior, then run `make check` before opening the PR:

```bash
uv run pytest -q tests/servers/test_agent_jobs_api.py
make check
```

`make check` runs formatting, lint, mypy over all three backend packages, and
pytest excluding the `external` and `dbtest` markers. It can change formatting.
`make lint` and `make typecheck` are available separately. `tests/servers/`
contains HTTP-level tests; the directory name does not mean they all require a
running deployment. Test markers determine which tier runs.

Database changes also need `make test-db` against a dedicated test database.
The fixtures read `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_NAME`,
`TEST_DB_USER`, and `TEST_DB_PASSWORD`; the name must contain `_test_`.
See [the database environment settings](../ENVIRONMENT.md#database). Tests
marked `external` have their own prerequisites, such as Docker or a configured
Gateway, documented in the relevant test and environment inventory.

Deployment tests read the sandbox CLI version variables. If the surrounding
development tool exports its own `CODEX_VERSION`, remove that inherited value
for the test invocation with `env -u CODEX_VERSION make check`; it is not the
sandbox's reviewed version pin.

For frontend changes, run these from the repository root:

```bash
cd apps/frontend
npm ci
npm run lint
npm run format:check
npm run type-check
npm run test
```

The backend Makefile does not run these frontend checks. CI also checks the
frontend's server-only boundary and runtime site configuration and, when the
image jobs are selected, builds and smoke-tests the root and `/agents` images.

## Changing an API

Two kinds of HTTP documents live under `gateway_contracts/`:

- The YAML files describe the Gateway APIs this service consumes. Update them
  against the corresponding Gateway source when changing those clients.
- `agent-control-plane.openapi.json` pins the API this service publishes.
  When an intentional change alters that API, regenerate the snapshot from
  the repository root, review its diff, and include it in the same PR:

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

The surface checks also reject duplicate registrations and overlaps between
the nine agent routers. A failed snapshot comparison needs an explanation of
the API change; regenerating the file alone does not establish compatibility.
See the [contract guide](../gateway_contracts/README.md) for the limits of these
checks.

## Packaging and documentation

When adding or moving a package, check package discovery in `pyproject.toml`,
type-checker and linter roots, Docker COPY paths, and the path classifier in
`.github/workflows/ci.yml`. Checkout imports alone do not prove the built images
contain a dependency. CI builds and starts the selected images, and the
classifier test derives packages shared by both images from their Dockerfiles.

Use [ENVIRONMENT.md](../ENVIRONMENT.md) for configuration,
[operations.md](operations.md) for operational behavior and troubleshooting,
and the [release guide](../deploy/release/README.md) for release artifacts.
Update these current-use documents when their commands or behavior change.
Designs, completed migration checklists, and acceptance records describe their
own baseline; preserve that history rather than rewriting it as current setup
instructions.
