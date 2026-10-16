# Sandbox operations: recorded verification baseline

Moved on 2026-10-16 from the end of the
[operations runbook](../../developer/operations.md), where it had stood since
the runbook moved into this repository on 2026-08-04. It records what
historical runs exercised, the gaps they left, and how the runbook changed
when it moved. None of it describes a current deployment; the runbook's
[runtime verification](../../developer/operations.md#runtime-verification)
section lists the checks to run today.

## Verified

This is the recorded verification baseline, including pre-split measurements.
It establishes what those runs exercised, not that a deployment has passed its
current release gates. Runtime pins and configuration in the operating sections
above describe the current service.

|  | How |
| --- | --- |
| Store: fencing, reaper, retained-group snapshot publish | dbtests against Postgres |
| Job API, SSE resume, worker endpoints | control-plane API and server suites |
| Inference grant attempt fencing | Live gateway. The same historical run exercised a per-job spend-cap 429; C5/C6 later removed that cap. Current inference uses the owner's gateway daily quota, which this service neither enforces nor verifies here. |
| Retained runner + runtime adapters | Docker-only local product stack with the exact Claude Code, Codex, Pi, OpenCode, and Kilo pins: every CLI passed `NEW → LIVE → docker stop → RESTORED` |
| Native Fork concurrency | every pinned CLI created a distinct child native session immediately; source and child were simultaneously `RUNNING` against one container/workspace and both crossed the local inference barrier |
| Publish: patch → branch → draft PR | real `git push` to a local bare repo |
| **A real job on a real model** | live Claude Code CLI × `deepseek-v4-flash` through the production gateway: docstring landed on disk, patch produced, 3 calls / 74,523 tokens / $0.0075 attributed to the job in `api_logs` |
| Retained Docker lifecycle | real mount-free (`Mounts=[]`) chat containers keep one writable layer and container id across group idle `docker stop` and later `docker start`; the phase bridge is released while stopped and recreated on wake, and publication capture replaces neither |
| Source import → edit → snapshot | the trusted runner imports a credential-scrubbed source archive into `/workspace`; real Git status/diff and the quiescent group publication snapshot complete without a host worktree or snapshot bind mount |
| Sandbox process boundary | real Docker: non-root Agent CLI member UIDs, dropped capabilities, and the closed agent egress of the time (since replaced by [one network](../../developer/operations.md#the-sandbox-network)); this shared-kernel measurement does not establish the required Kata isolation for public multi-tenant use |
| The control plane on staging | `/v1/agent/*` live: job create/get/list/cancel, SSE stream, and an unauthenticated host refused at `/worker/claim` |
| MCP: per-user resolution, relay authorization, tool filtering | unit + API tests against a mocked upstream: one user's credential never resolves for another, a job cannot reach a server it was not granted, a dead fence loses tools, and a blocked tool never leaves the relay |
| **MCP: what `--strict-mcp-config` (hybridInference#1129) actually prevents** | the flag was added on the strength of the CLI's documented behaviour; this is the measurement. Real Claude Code 2.1.220 in a job's exact configuration (headless, `bypassPermissions`, cwd = a checkout carrying a `.mcp.json`): without the flag the CLI reports the repository's server alongside the platform's, with it only the platform's. No approval step was involved either way |
| MCP: the adapter's `--mcp-config` is accepted by the real CLI | the argv `ClaudeCodeRuntime.prepare` emits, run against the real binary: the platform's server appears in `system/init`, and a deliberately bogus flag in the same argv fails instantly, so the flags are being parsed rather than ignored |
| MCP: the token can stay out of argv | the same run with the header written as `Bearer ${AGENT_MCP_TOKEN}` and the variable exported: the CLI expanded it and the proxy authenticated the request, while the command line carried only the placeholder |
| **MCP: the proxy against a real hosted server** | the shipping router and filters against live `mcp.deepwiki.com` (which answers over SSE, so the streaming filter is the one that ran): `initialize` → `serverInfo: DeepWiki`, `tools/list` → its three real tools, `tools/call read_wiki_structure` → real documentation for `psf/requests`. Measured before the deployment-wide registry was replaced by the per-user catalogue (hybridInference#48); that deployment-wide catalogue has since been replaced by generic per-user connections, so this records the historical relay path rather than a current deployment configuration |
| MCP: the allowlist, on a real catalogue | same server, `tools: [read_wiki_structure]`: `tools/list` returned that one tool of the three, and `tools/call ask_question` came back `-32601` without the request leaving the relay. This exercised the historical tool-name allowlist; the then-current `github` catalogue instead declared `read_scope="endpoint"`. Current connections use discovered capabilities and per-session policies. |
| MCP: the authorization matrix, end to end | live token + granted server 200; live token + a server the job was not granted 403; a superseded attempt 401; an ordinary `hyi-` key 401; no credential 401 |
| MCP: Claude Code driving the proxy | the real CLI, pointed at the proxy, completed the whole handshake through it — `initialize`, `notifications/initialized`, the `GET` stream, `tools/list` — each reaching deepwiki and returning 200 |
| **A self-hosted runner completing a real job** | `ajob_04de5d509a25ded2` against staging: claimed → cloned `psf/requests` at `414f0513` (verified equal to that repo's HEAD) → Claude Code 2.1.220 in a Docker sandbox as uid 10001 → `Read` + `Edit` with `is_error: false` → 518-byte patch stored → `succeeded`. The patch was read back and matches the file the agent left on disk |

## Not verified

These are gaps in the recorded baseline, not a live inventory of staging or
production. Use them to plan acceptance checks and record new evidence in the
deployment that performs those checks.

- **Cross-model behavior.** The baseline includes one real job on one model.
  Its planned 2-runtime × 3-model matrix was not run. The service now supports
  five runtimes; the historical measurement does not establish conformance
  across their current model combinations.
- **MCP through a deployed sandbox.** The hosted DeepWiki checks used the
  shipping router and filters with a fake job store and a CLI outside a
  sandbox. The later [end-to-end test](../../developer/operations.md#end-to-end-coverage) uses a real
  database, listener, and CLI with stub upstream and model endpoints. Neither
  record establishes the full deployment path: queue → runner → group bridge
  → MCP tunnel → `/v1/agent/mcp` → a user's real server.
- **Credentialed upstream acceptance.** DeepWiki required no credential.
  Tests establish that a configured per-user header reaches the upstream;
  the baseline does not establish that a real credentialed server accepts it.
- **Live-model tool behavior.** Hosted-server checks were client-driven, and
  the end-to-end test scripts the model response. They do not measure whether
  an actual deployed model chooses and uses the tools correctly.
- **Production readiness.** A named deployment's current status belongs to
  its downstream distribution. The gateway's former `/v1/agent/*` 404 was
  about routes deleted on 2026-08-09, not this service. Use the distribution's
  deployment records and the artifact contract in
  [the release README](../../../deploy/release/README.md) and
  [`deploy/DEPLOY_PROTOCOL`](../../../deploy/DEPLOY_PROTOCOL).
- **Historical staging model availability.** Of the 15 models then listed by
  `/v1/models`, only `glm-5.1` and `qwen3.6-35b` resolved for an agent-job
  token, and both reached `qwen3.6-35b`. The others returned
  `404 Model not found`. Recheck the configured model with an agent grant before
  using a failed first turn to diagnose the runner chain.
- **One-network acceptance on real traffic.** Unit tests cover rendered host
  rules, read-back, and group-bridge lifecycle. The baseline did not run design
  §9's real Kata guest tests against the public web and listening targets for
  every refused destination class. The 2026-09-25
  [grant replay decision](../../developer/operations.md#mcp-prerequisite-and-accepted-inference-grant-risk)
  removed the separate inference entry as a release prerequisite; it supplied
  no new Kata evidence and waived no other gate.
- **Kata deployment acceptance.** Recorded jobs used the host kernel. The
  installer and runner preflight are implemented and unit-tested, including
  refusal when a daemon accepts the runtime but the sandbox still uses the host
  kernel. The original staging record did not include provisioning or a real
  Kata job. Use [the provisioning procedure](../../developer/operations.md#provisioning-kata-on-a-runner-host)
  and record both guest isolation and relay reachability on the actual host.

The historical runtime probe distinguished these daemon responses:

| `--runtime` | Recorded result |
| --- | --- |
| unset (runc) | Runs |
| a name that does not exist | `unknown or invalid runtime name`: the daemon rejects it, so the flag is reaching the daemon |
| `io.containerd.kata.v2` | `failed to start shim`: the name is accepted and the shim attempted, but its binary was missing |

That ruled out a silently dropped Docker runtime flag for that probe. It did
not prove a Kata guest could start; the guest-kernel and network checks remain
necessary.

The source repository's deleted `agent-job-runner` workflow never executed on
GitHub and is no longer a gate. This repository's Actions build control-plane
images and publish neutral releases from green `dev` commits; job execution
uses enrolled hosts.

## Known gateway findings

These gateway behaviors were observed by the historical conformance suite.
Track their resolution in the gateway and recheck its deployed version:

- An upstream mid-stream disconnect is masked with a synthesized
  `finish_reason: stop` + `[DONE]`, so a client cannot detect truncation. For
  agent workloads that is a poisoning vector: truncated tool-call arguments
  look complete.
- An in-stream 429 error frame is typed `server_error`, so a client branching
  on `type` to decide whether to back off misclassifies a rate limit.

## Model behaviour observed on the first real run

The recorded first live-model run used `deepseek-v4-flash`. It replied _"I've added a docstring to the `greet` function.
It now reads: ..."_ immediately after receiving a tool result with
`is_error: true` saying the write had been denied. Nothing had changed on
disk.

This is why the runner reports what the tools did rather than what the agent
says about them: it collects errored `tool_result` entries, emits them into
the owner's stream, and refuses to record a run as successful when it produced
no patch _and_ a tool failed. A model's own account of its work is not
evidence.

## What changed when this moved

Task H5 moved this page from the source repository's
`docs/developer/agent-sandbox-operations.md` at the freeze commit `764a6f97`.
Under the migration's move-as-is rule the initial edits were **paths and the
artifacts that did not move**. The table below records that baseline; later
feature work has intentionally brought the live operational sections above up
to date.

| Source | Here | Why |
| --- | --- | --- |
| `ops/setup/setup_kata_runtime.sh` | `deploy/host/setup_kata_runtime.sh` | D7 |
| `ops/deploy/sandbox_host.sh` | `deploy/host/sandbox_host.sh` | D7 |
| `deploy/docker/Dockerfile.agent-sandbox` | `deploy/sandbox/Dockerfile.agent-sandbox` | D7 |
| `ops/deploy/agent_runner.sh up/preflight/status/down` | the standalone script's `up/preflight/check/status/down` | The co-located overlay was dropped (MIGRATION.md, open decisions, decided 2026-08-04). The standalone preflight validates an already-built image; `up` builds the target image and then repeats the authoritative checks. |
| `docker compose -f docker-compose.yml -f docker-compose.agent-runner.yml` | `docker compose -f deploy/host/docker-compose.sandbox.yml` | Same decision. There is no base compose here and no `backend` service. |
| "Use that script, not `agent_runner.sh`" | a note that the standalone compose is the only topology | Same decision. |
| The staging deploy-pipeline paragraph (`deploy_staging.sh`, the same-invocation requirement, the overlay attaching `backend` to the agent-egress network) | removed | It describes the _gateway's_ deploy pipeline driving the overlay that did not move. Keeping it would be an instruction nobody here can follow. |
| `AGENT_RUNNER_HOST` row naming `agent_runner.sh` and `deploy_staging.sh` | names the standalone script | Same. |
| `MCP_CONFIG_PATH` / `config/examples/mcp.example.yaml` | initially marked gateway-side | The move-only baseline still used the old gateway registry. Subsequent extraction work replaced it with this service's in-code catalogue, per-user connections and isolated MCP relay. |
| `pytest … test_agent_mcp.py test_agent_mcp_api.py` | initially `pytest tests/unit/test_agent_runtimes.py` | The move-only baseline left the proxy suites behind. They were subsequently ported and expanded in this repository. |

### Historical statements that remain historical

- **The pre-split verification record.** Where a row explicitly describes an
  old live measurement, it remains evidence of that historical run rather than
  a claim that a new deployment gate has been exercised. Current component
  ownership and operating instructions use the post-split topology.
- **Cutover inventory.** Secret provenance and one-time migration steps remain
  in [cutover.md](../specs/cutover.md) and `ENVIRONMENT.md`; this page documents the
  resulting runtime topology.
