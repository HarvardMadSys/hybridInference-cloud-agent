# Contributing

Read `AGENTS.md` first: it is the onboarding guide for people and coding
agents alike, and it names the quality gates.

## The short version

- Branch from `dev`; pull requests target `dev`. `main` only ever
  fast-forwards from `dev`.
- Run `make format` and make sure `make test` passes before opening a PR.
  The web app has its own gates: `npm run lint`, `npm run type-check`,
  `npm run test` under `apps/frontend/`.
- Commit subjects are conventional (`type(scope): summary`). PRs are squash
  merged, so the PR title becomes the commit subject on `dev`.
- A change to an environment variable, a compose file or a workflow is
  incomplete until the tests that pin them pass — several exist precisely
  because "written but not wired" has bitten this repository before.

## Pull requests from forks

CI runs pull requests on GitHub-hosted runners and never on the self-hosted
pool, so a fork's PR gets the same checks as anyone else's. Deployments are
not triggered by pull requests.

## Contracts

`gateway_contracts/*.openapi.yaml` describe the gateway HTTP contracts this service
consumes. They are transcriptions of the gateway's routers, and tests pin what
this service reads against what they declare. If you change what the client
reads, change the contract file in the same PR and say which gateway change it
tracks.
