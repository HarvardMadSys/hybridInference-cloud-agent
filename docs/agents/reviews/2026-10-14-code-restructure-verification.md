# Code restructure verification

The restructure preserves the public wire contracts while separating domain
models, runtime adapters, application flows, profiling, and frontend modules.
Verification on 2026-10-14 found and repaired a public type-reflection regression.
The results below apply to the stated source revisions and test environments.

## Revisions and scope

- [PR #620](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/620)
  restructured `64c5ef34` into `bf6ca00f9dadf519ba9b9ae41b5570cc719cb7c0`.
  Its 21 non-skipped hosted CI checks succeeded; Kata qualification was skipped.
- The follow-up was tested at
  `70c27ec0db84dad4f3bdc106a677bd241162ba1a`, based on `42604d0a`.
  That base includes intervening feature changes. Current model and OpenAPI
  counts therefore must not be presented as parity with the pre-refactor tree.

The original restructure split API schemas, host runtime adapters, host value
types and HTTP clients, shared operation profiling, frontend API operations,
and job-detail UI into focused modules. It retained public import paths and
moved lifecycle and policy ownership out of HTTP routers and repositories.

## Original restructure parity

Audits against the original parent and the integrated dev base checked schema
declarations, public exports, both listener contracts, host/client behavior,
runtime dialects, profiling, and frontend operations. Schema declaration ASTs
and both generated OpenAPI documents matched the integrated original base.
Host and runtime declaration comparisons found only the intended module/import
changes and shared runtime-catalog lookups. Frontend API exports retained exact
parity. Profiling audits preserved collection boundaries, frozen records,
default-off behavior, clock replacement, and bounded background export.

A final reflection comparison checked all 48 callable profiler exports plus
public methods and dataclass constructors. It found no newly failing
`typing.get_type_hints` calls or changed normalized annotations. Both revisions
retain the pre-existing unresolved `Iterator` annotation on `attributed_stage`.
The corresponding host audit compared 109 classes, functions, public methods,
properties, and generated constructors and found no new resolution failures.
Three constructors retain pre-existing unresolved `Callable` annotations; the
inspection model's additional `tmpfs` field comes from intervening upstream work.

The publisher cancellation repair was a deliberate behavior fix: cancelling
the publisher while it joined its cancelled heartbeat previously swallowed the
owner's cancellation and resumed the loop. The same event-controlled probe
against `64c5ef34` returned `published` with `cancelled=False`; the merged code
returned cancellation with `cancelled=True`. The regression verifies that
publication succeeds and shutdown still terminates the publisher.

## Follow-up repairs and regression coverage

- **Runtime reflection:** moving the registry left `AgentRuntime` available only
  under `TYPE_CHECKING`, so `typing.get_type_hints(get_runtime)` could raise
  `NameError`. Importing the already-used base class at runtime restores public
  factory introspection. The regression checks resolved annotations for host
  factory/discovery functions and shared discovery functions.
- **Deployment import names:** ordinary package-direction checks could miss
  `apps.backend.*` imports. Such imports resolve in a checkout but fail in the
  runner's flattened package layout. A new guard scans all three production
  backend packages. Negative cases cover absolute, parent-module, star,
  deferred, and `TYPE_CHECKING` imports; positive cases preserve installed
  names, valid relative imports, and unrelated lookalike namespaces.
- **Six public submission cases:** create-job tests cover lost responses and
  temporary server failures without an automatic resend; idempotency-conflict
  rotation for both supported error envelopes; retention after an unrelated
  conflict; and recovery across reload followed by retirement after success.
  They inspect actual request IDs, bodies, responses, and call counts through
  the public API facade. Existing Fork tests separately cover ambiguous retries,
  independent request scopes, durable failure, and caller-selected request IDs.
- **Interpreter-exit export:** a real child process holds publication in flight
  until interpreter shutdown, then exits without an explicit caller flush.
  The test verifies one complete, frozen JSONL record with the expected
  measurement, run, operation, and job identity. Disabling the exporter's exit
  drain in the negative control loses the required record and fails the test.
- Credential documentation now identifies the control plane as the token
  minter and the host as the injector, and includes authorized `GH_TOKEN` and
  owner-selected environment values in the sandbox boundary.

No existing assertion, performance threshold, timeout, skip requirement, or
security check was weakened. New cases add coverage to the existing contracts.

## Packaging and deployment-boundary probes

At the follow-up revision, source-archive inspection included all **46** checked
refactored source files. An offline wheel build and fresh wheel-only import probe
included **20 schema files**, **143 resolved models**, and **170 public exports**. The main
listener produced **212 paths / 305 components**; the MCP listener produced
**3 paths / 2 components**. These are current-revision counts, not a claim that
intervening upstream API additions disappeared.

Independent import-blocking probes established that:

- both control-plane listeners construct their contracts with the host
  implementation unavailable;
- the runner, retained chat sandbox, five runtime adapters, and profiler import
  with the control-plane implementation unavailable.

Both image layouts recursively include the relevant packages, and package
discovery includes the new subpackages. These source, wheel, and import probes
do not substitute for a fresh build of every release image or deployment.

## Local execution evidence

| Check | Result | Evidence |
|---|---|---|
| Final complete backend gate | Passed: 10,240 tests; 6 root-required skips covered separately below; format/lint/docstrings/types passed | `/tmp/cloud-agent-postmerge-backend-alone.log` |
| Final complete PostgreSQL tier | Passed: 1,495 tests, 0 skipped | `/tmp/cloud-agent-postmerge/database-alone/summary.txt` |
| Frontend lint, format, types and build guards | Passed; server-only guard and both runtime-configuration variants | `/tmp/cloud-agent-postmerge/frontend/summary.txt` |
| Complete frontend tests on Node.js 22 | 3,762 passed across all 210 files with one isolated worker | `/tmp/cloud-agent-postmerge-frontend-single-worker.log` |
| Root-required cases in an isolated container | 6 passed; checkout mounted read-only, network disabled, working directory `/tmp` | `/tmp/cloud-agent-postmerge-final-root-tests.log` |
| Real member filesystem/UID permission cases | 3 passed, 0 skipped | `/tmp/cloud-agent-postmerge-member-permissions.log` |
| Pinned Claude native-session cases | 9 passed, 0 skipped; Claude Code 2.1.220 / SDK 0.2.148 | `/tmp/cloud-agent-postmerge-native-context.log` |
| Developer documentation build | Passed with warnings treated as errors | `/tmp/cloud-agent-postmerge-docs.log` |

The native-session cases use the pinned CLI/SDK and local protocol fixtures;
they do not establish live provider correctness. This verification did not run
Kata qualification or make live-provider deployment claims.

Initial complete runs hit two unchanged timing assertions:

- `test_retained_deploy.py::test_a_partly_spent_fence_shrinks_the_call_bound`
  measured 19.55 seconds against the existing `< 10` assertion. It passed
  independently in 6.90 seconds.
- `test_measurement_acceptance_db.py::test_congestion_keeps_answering_and_the_queue_counts_are_correct`
  measured 1.012 seconds against the existing `< 1.0` assertion.

Both standalone complete-tier reruns passed, including the original timing
assertions. Neither assertion nor its timeout was changed to obtain a pass.
The initial failures remain recorded alongside the final rerun evidence.

The initial frontend run used two workers and ran all 3,762 tests. It passed
3,759 and reached the existing five-second timeout in three cases: admin settings
keyboard navigation, JobDetail inspector focus restoration, and closed tool
details during draft changes. The frontend lint, formatting, and TypeScript
checks and both build guards passed. All three timed-out cases passed together
with one isolated worker (2.37 seconds of test execution, 8.65 seconds total).
The complete frontend rerun with that worker limit passed all 3,762 tests across
210 files. It changed scheduling only: test-file isolation, assertions, and
the original five-second timeout remain in force. The initial `make ci-frontend`
process returned failure for those three Vitest timeouts; its other five checks
passed. The complete single-worker Vitest rerun resolves that test-stage failure.

## Essential reproduction commands

Run from the repository root with Python 3.12, `uv`, Docker for the database
tier, and Node.js 22 for the frontend:

```bash
umask 022
env -u CODEX_VERSION make check
env -u CODEX_VERSION make ci-database
make ci-frontend
# If elapsed-time tests exceed their limits while running in parallel,
# rerun every file with one isolated worker and keep the original limits.
(cd apps/frontend && npm test -- --maxWorkers=1)
make docs
```

The initial frontend gate used CPU affinity `0,1`, giving Vitest two workers;
the complete rerun used `--maxWorkers=1` without a CPU-affinity restriction.
Neither invocation changed test expectations. `make ci-backend` is the check-only formatter/linter variant
of the backend gate. Run full tiers separately when evaluating timing-sensitive
failures, and retain their logs and JUnit counts, including skips.

The added contracts can be exercised independently:

```bash
uv run pytest -q tests/unit/test_application_boundaries.py \
  tests/unit/test_runtime_catalog.py tests/unit/test_operation_profiling_shutdown.py
cd apps/frontend
npm exec vitest run src/lib/api/__tests__/agentSubmissions.test.ts
```

The external permission and native-session cases require their declared local
image/CLI prerequisites. A skipped run is not evidence that those boundaries
were exercised; the recorded runs above had zero skips.
