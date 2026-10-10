# Contributing

Read [AGENTS.md](AGENTS.md) before changing this repository. The
[developer guide](docs/development.md) covers local setup, code ownership,
dependency boundaries, and checks for common changes.

## The short version

1. Branch from `dev` using `<user>/<scope>/<name>` and keep the change focused
   on one feature, fix, or documentation update.
2. Run tests that cover changed behavior and add coverage where needed. Preserve
   test strength; fix a regression rather than weakening an assertion to pass.
3. Update the affected contracts, migrations, and current-use documentation.
4. Run `make check` and any additional checks required by the change. Review
   the automatic formatting and lint fixes it makes.
5. Open a pull request targeting `dev`, describe the resulting behavior, and
   record the checks you actually ran and their results.

Use conventional commit subjects and PR titles: `type(scope): summary`.
Pull requests are squash merged, so the PR title becomes the commit subject on
`dev`. `main` advances through the manual
[fast-forward workflow](.github/workflows/sync-main.yml).

Treat every commit as public and never commit secrets, tokens, or keys. Use the
[PR checklist](.github/pull_request_template.md) to review the diff before
submission. The completed repository extraction is documented in
[MIGRATION.md](MIGRATION.md); its move-only restrictions are historical, while
the service and security boundaries in `AGENTS.md` still apply.

## Checks before review

Run backend commands from the repository root:

```bash
make setup-dev
make check
```

`make setup-dev` is the initial environment setup. `make check` formats, lints,
type-checks all three Python packages, and runs tests excluding `external` and
`dbtest`.

For frontend changes, also run:

```bash
cd apps/frontend
npm ci
npm run lint
npm run format:check
npm run type-check
npm run test
```

CI also runs the server-only and runtime site-configuration build guards. Run
the relevant guard locally when changing those boundaries, following the
[developer guide](docs/development.md#run-the-relevant-checks).

Use the [developer guide](docs/development.md#run-the-relevant-checks) to select
database and external tests. Database changes require the `dbtest` tier against
a dedicated PostgreSQL test database. Include an additive Alembic migration
for schema changes; service startup must not create or alter the schema.

Changes to environment variables, Compose files, Docker packaging, or workflows
need the tests that verify their wiring. Document new variables in
[ENVIRONMENT.md](ENVIRONMENT.md). Keep documentation alongside a change when
its commands or behavior change.

## Continuous integration

[CI](.github/workflows/ci.yml) classifies changed paths and selects backend,
frontend, and image jobs. It runs Python 3.12 and Node.js 22. The `CI Gate`
requires the classifier to succeed and every other job to succeed or be
deliberately skipped.

Selected jobs cover backend formatting, lint, mypy, the default and PostgreSQL
test tiers, frontend checks, secret scanning, Compose rendering, image builds,
and image smoke tests. CI also exercises selected real CLI/session and MCP
flows. The real-Kata candidate check runs for relevant `dev` commits after
review. Pull-request image jobs build and smoke-test without publishing.

The current workflow runs on self-hosted pools, including pull requests. The
runner policy must change before accepting untrusted public-fork PRs on these
machines; do not infer a safe public-fork policy from the current private-repo
routing. Release publication has its own
[workflow](.github/workflows/publish.yml) and [guide](deploy/release/README.md).

## Contracts

`gateway_contracts/*.openapi.yaml` records the Gateway HTTP APIs consumed by
this service. Verify changes against the corresponding Gateway source. When
you change a client's endpoints or fields, update the relevant contract in the
same PR and identify the Gateway change it tracks. Tests pin this service's
side; they cannot verify the current implementation of another repository.

`gateway_contracts/agent-control-plane.openapi.json` records this service's own
API. For intentional changes, regenerate it using
[the developer-guide command](docs/development.md#changing-an-api), review the
diff, and explain compatibility implications in the PR.

Shared Python protocols in `apps/backend/cloud_agent_shared/` are a separate
contract between the control plane and hosts. Review protocol versions,
capabilities, and runtime pins together when changing that boundary.
