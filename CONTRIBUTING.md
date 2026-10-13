# Contributing

Before changing this repository, read [AGENTS.md](AGENTS.md). See the
[developer guide](docs/development.md) for local setup, code ownership,
dependency boundaries, and checks for common changes.

## The short version

1. Create a branch from `dev` named `<user>/<scope>/<name>`. Keep the change
   focused on one feature, fix, or documentation update.
2. Run tests that cover the behavior you changed, and add coverage where
   needed. Keep the tests strong: fix regressions instead of weakening
   assertions just to make them pass.
3. Keep affected contracts, migrations, and current-use documentation up to
   date.
4. Run `make check` and any other checks the change requires. Review the
   formatting and lint fixes it applies automatically.
5. Open a pull request against `dev`. Describe the resulting behavior, and
   list the checks you actually ran and their results.

Use conventional commit format for commit subjects and PR titles:
`type(scope): summary`. Pull requests are squash merged, so the PR title
becomes the commit subject on `dev`. `main` advances through the manual
[fast-forward workflow](.github/workflows/sync-main.yml).

Treat every commit as public. Never commit secrets, tokens, or keys. Before
submitting, use the [PR checklist](.github/pull_request_template.md) to review
the diff. [MIGRATION.md](MIGRATION.md) documents the completed repository
extraction. Its move-only restrictions are historical, but the service and
security boundaries in `AGENTS.md` still apply.

## Checks before review

Run these backend commands from the repository root:

```bash
make setup-dev
make check
```

Use `make setup-dev` for the initial environment setup. `make check` formats,
lints, and type-checks all three Python packages. It also runs tests, excluding
the `external` and `dbtest` tiers.

For frontend changes, also run:

```bash
cd apps/frontend
npm ci
npm run lint
npm run format:check
npm run type-check
npm run test
```

CI also runs build guards for server-only and runtime site configuration. If
you change those boundaries, follow the
[developer guide](docs/development.md#run-the-relevant-checks) and run the
relevant guard locally.

Use the [developer guide](docs/development.md#run-the-relevant-checks) to choose
the database and external tests you need. Database changes require the
`dbtest` tier against a dedicated PostgreSQL test database. Schema changes
need an additive Alembic migration. Service startup must not create or alter
the schema.

If you change environment variables, Compose files, Docker packaging, or
workflows, run the tests that verify their wiring. Document new variables in
[ENVIRONMENT.md](ENVIRONMENT.md). Update the documentation in the same change
when commands or behavior change.

## Continuous integration

[CI](.github/workflows/ci.yml) classifies the changed paths and chooses the
backend, frontend, and image jobs to run. A pull request is compared with its
base, and a push with the newest `agent-*` release, so a released commit has had
every change since the previous release checked. It uses Python 3.12 and
Node.js 22. The `CI Gate` requires the classifier to succeed. Every other job
must either succeed or be deliberately skipped.

Secret scanning and the default test tier run for every change: the tier's
tests also read documentation, `ENVIRONMENT.md` and the web app's sources.
Depending on the paths changed, other jobs cover backend formatting, lint,
mypy, the PostgreSQL test tier, frontend checks, Compose rendering, image
builds, and image smoke tests. CI also exercises selected real
CLI/session and MCP flows. After review, the real-Kata candidate check runs for
relevant `dev` commits. Pull-request image jobs build and smoke-test without
publishing.

Every pull request, from a fork or from a branch here, runs on disposable
GitHub-hosted runners; pushes to `dev` and `main` run on the self-hosted pools.
Checked-in runner routing is not a security boundary: a fork can edit it. Issue
[#464](https://github.com/HarvardMadSys/hybridInference-cloud-agent/issues/464)
tracks the runner-group restrictions that are, and the
[developer guide](docs/development.md#run-the-relevant-checks) describes them.
Release publication has its own
[workflow](.github/workflows/publish.yml) and [guide](deploy/release/README.md).

## Contracts

`gateway_contracts/*.openapi.yaml` records the Gateway HTTP APIs that this
service consumes. Check changes against the corresponding Gateway source. If
you change a client's endpoints or fields, update the relevant contract in the
same PR. Identify the Gateway change that it tracks. Tests pin this service's
side of the contract, but they cannot verify the current implementation in
another repository.

`gateway_contracts/agent-control-plane.openapi.json` records this service's own
API. For an intentional change, regenerate it with
[the developer-guide command](docs/development.md#changing-an-api). Review the
diff, and explain the compatibility implications in the PR.

The shared Python protocols in `apps/backend/cloud_agent_shared/` form a
separate contract between the control plane and hosts. When you change that
boundary, review the protocol versions, capabilities, and runtime pins
together.
