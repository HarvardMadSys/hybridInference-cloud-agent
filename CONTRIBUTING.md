# Contributing

The [developer guide](docs/development.md) covers setup, code ownership,
dependency boundaries, and checks for common changes. [AGENTS.md](AGENTS.md)
records the repository rules for people and coding agents alike.

## The short version

- Branch from `dev`; pull requests target `dev`. `main` only ever
  fast-forwards from `dev`.
- Run `make check` before opening a PR: it formats, lints, type-checks all
  three Python packages, and runs the default test tier.
  For frontend changes, run `npm ci`, then `npm run lint`,
  `npm run format:check`, `npm run type-check`, and `npm run test` under
  `apps/frontend/`. `make check` does not run these frontend checks.
- Commit subjects are conventional (`type(scope): summary`). PRs are squash
  merged, so the PR title becomes the commit subject on `dev`.
- A change to an environment variable, a compose file or a workflow is
  incomplete until the tests that pin them pass — several exist precisely
  because "written but not wired" has bitten this repository before.

## Continuous integration

The repository's current CI uses self-hosted runner pools, including for pull
requests. Changed paths select the backend, frontend, and image jobs; the
`CI Gate` check verifies their results. The runner policy must be changed
before accepting untrusted public-fork pull requests on those machines.
See [the workflow](.github/workflows/ci.yml) for the current routing and checks.
Pull-request jobs build and smoke-test images without publishing them.

## Contracts

`gateway_contracts/*.openapi.yaml` describe the gateway HTTP contracts this service
consumes. They are transcriptions of the gateway's routers, and tests pin what
this service reads against what they declare. If you change what the client
reads, change the contract file in the same PR and say which gateway change it
tracks.

`gateway_contracts/agent-control-plane.openapi.json` instead pins this service's
own API. For an intentional API change, regenerate and review that snapshot in
the same PR, following the [developer guide](docs/development.md#changing-an-api).
The shared Python protocol definitions live in
`apps/backend/cloud_agent_shared/`; they are separate from these HTTP documents.
