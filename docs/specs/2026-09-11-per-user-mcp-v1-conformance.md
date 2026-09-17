# Per-user MCP: conformance against the design

- Updated: 2026-09-13.
- Baseline: PR #207's generic MCP foundation, integrated with the current
  capability-based storage architecture on `dev`. Migration order:
  `0007_attempt_turn_resources → 0008_generic_mcp_connections → 0009_mcp_refresh_lease → 0010_mcp_policy_revisions`.
- Design: [`2026-09-11-per-user-mcp-v1-design.zh.md`](2026-09-11-per-user-mcp-v1-design.zh.md).

This is an audit, not a plan. It states, dimension by dimension, what the code
does, where the evidence is, and what is missing — because "we implemented the
design" is a claim that has to be checkable, and the parts that are missing are
easier to argue about when they are written down.

The external conformance tests run the built sandbox image, real CLIs, relay
and PostgreSQL against synthetic model and MCP endpoints. They do not establish
production deployment acceptance or third-party OAuth interoperability.

## Codex/Kilo delivery scope

This PR enables Codex and Kilo on the same remote MCP path already used by the
other coding agents. The broader generic MCP design remains the reference for
stdio execution and optional capabilities that are still unimplemented.

| Accepted goal | Current implementation and evidence |
| --- | --- |
| Both CLIs directly supported | Codex 0.145.0 and Kilo 7.4.17 advertise `mcp=True`; the runtime API lists all five; no feature switch or preview tier |
| Reuse the same connection and permissions | All adapters target the `session` merged endpoint; catalogue, deny/confirm/allow decisions and current-attempt checks are shared |
| Approval continues the original request | The Tools panel resolves held invocations; the real five-CLI matrix covers allow, confirm and normalized names without manual retry |
| Follow-up, resume, fork and token rotation | The real CLI matrix verifies each native path and rejects the superseded member token |
| Existing deployments and retained sessions | Trusted runtime preparation installs Codex policy, reviewed adapter files and the patched Kilo executable before launch |
| Repository configuration cannot add MCP servers | Codex system policy and Kilo's patched legacy loader are exercised against injected repository configuration |
| Normal deployment | Apply migrations before starting matching control-plane, runner and sandbox releases; the CI sandbox cell exercises the built image |

The merged protocol endpoint supports resources and prompts, but the recorded
resource-read checks use a protocol client; they do not establish native UI or
resource/prompt invocation parity for every CLI. stdio execution, elicitation,
sampling, subscriptions, tasks and MCP Apps remain outside this CLI-enablement
delivery. The detailed tables below describe those limits.

## Compatibility dimensions (design §2)

| Dimension | State | Evidence | Missing |
| --- | --- | --- | --- |
| User-defined standard MCP; templates optional | Done | `POST /v1/agent/mcp/connections` takes any URL or launch spec; `_TEMPLATES` only prefills | — |
| Streamable HTTP | Done | `transports.StreamableHttpTransport`, 17 cases in `tests/unit/test_mcp_transports.py` | — |
| Legacy HTTP+SSE | Done | `transports.connect_legacy_sse`, 6 cases in `tests/unit/test_mcp_legacy_sse.py` | — |
| stdio, managed worker or user connector | **Configuration only** | launch plans, pinning and mount checks in `mcp/launch.py`; connector CRUD and binding in the API | no worker and no connector runtime; both refuse at use |
| Auth: none / static / OAuth | Done | `mcp/net_policy.py`, `mcp/oauth.py`, `mcp/refresh.py` | — |
| Auth: stdio env or secret reference | **Stored only** | `secret://NAME` recorded in the launch spec, ciphertexts in `set_mcp_launch_spec` | `launch.resolve_env` is never called: nothing resolves a reference into a process environment, because nothing starts one |
| Multiple instances per service | Done | no `UNIQUE(user_id, server)`; `test_the_same_service_twice_is_two_connections` | — |
| Dynamic tools / resources / resource templates / prompts | Done | `protocol.discover`, `_host_listing`, `_host_fetch`; `resources.resource_capability` resolves expanded template URIs under the selected resource policy | — |
| All five coding agents | **Supported** | `runtimes_supporting_mcp()`; every adapter uses the same merged endpoint and approval policy | — |
| Optional capabilities reported as native / bridge / unsupported | Done | `protocol.support_states`, stored in the snapshot | — |

### Protocol profiles (design §2, second table)

| Profile | State | Evidence |
| --- | --- | --- |
| 2024-11-05 legacy HTTP+SSE | Done | announcement parsed, validated, and routed on one reader |
| 2025 Streamable HTTP | Done | session header, JSON / SSE replies, era probe |
| 2026-07-28 stateless | Done | per-request `_meta`, `server/discover`, `resultType`, no session or GET stream; the CLI replies it sends are refused rather than invented |

## §8.1 Protocol and capability adaptation

| Requirement | State | Note |
| --- | --- | --- |
| Separate upstream / downstream negotiation through an explicit adapter | Done | three profiles in `protocol.PROFILES`, era detected rather than assumed |
| tools / resources / prompts native first, controlled adaptation otherwise | Done | the merged host serves all three; the pi bridge is the controlled adaptation for a CLI with no MCP client |
| Types, binary / media and size limits presented truthfully | Done | `host.ContentBlock` carries `truncated` and names `unsupportedContent` rather than returning an empty result |
| Correlate by request id, method and context — including cancel and progress | **Partial** | the transports correlate replies by id and tolerate progress frames before the answer. On the merged endpoint a `notifications/cancelled` and an upstream progress notification are both dropped: the surface is request/response, so a cancel that arrives on a later HTTP request has nothing to reach |
| Upstream resource identifiers preserved; merged resource identity includes its connection | Done | Lists and protocol resource results use `mcp-resource://<connection-id>/<original-uri>`; reads strip only that prefix and send the original URI unchanged to its owning connection |
| Opaque session / event handles bound to connection, revisions, epoch, member, attempt, owner lease generation | Partial | live session keys include connection, member and lease generation; reuse also compares configuration, authorization and credential fingerprints. The optional `agent_mcp_transport_sessions` repository exists, but the production merged host is constructed without that store. Durable cross-process session ownership and event replay are not implemented |
| Rebuild the protocol session when the owner is superseded, without resending dispatched work | Done for process-local sessions | a new attempt or lease receives a new key; superseded sessions close without replay. Listener maintenance also reclaims finished jobs and removed connections against the live database fence |
| Bounded request, frame, cumulative response and events | Done | `jsonrpc.MAX_MESSAGE_BYTES`, `sse.MAX_EVENT_BYTES`, `transports.MAX_RESPONSE_BYTES` |
| SSE handles LF / CRLF and fragmentation, and closes on overflow | Done | `mcp/sse.py`; 6 cases |
| Separate finite timeouts for execution, confirmation and subscription | Done | a held call has the four-clock contract in `mcp/invocations.py`, and execution takes a per-connection `request_timeout_s` (5-900s) that both the relay's client and the host's transport honour. Unset means the 120-second default, which is what every existing row got from the migration |

### Optional capabilities (design §8.1, second table)

| Capability | State | Note |
| --- | --- | --- |
| Elicitation | **Not implemented** | the host answers a server that asks with a protocol error rather than inventing consent; there is no user form |
| Sampling | **Not implemented** | refused with a diagnosis; no model authorization path |
| Roots | Empty authorized set | legacy/Streamable HTTP server requests and modern input continuations both receive an empty list; no workspace path is exposed |
| Subscriptions / Tasks / MCP Apps | **Not implemented**, reported | `support_states` marks them `unsupported` so a client is told rather than left to hang |

## §4–§7, §8.3, §9

| Area | State |
| --- | --- |
| §4.1 add / import / templates decoupled | Done: JSON and TOML dialects, preview before save, masked secrets |
| §4.2 auth and connection diagnosis | Done: staged diagnostics recorded on the connection; OAuth with discovery, registration precedence, PKCE, `resource`, single-use state, refresh and revocation. A refresh is claimed under a lease with a generation (`0009`), so two workers cannot spend one rotating token, and a superseded refresher's write matches no row. The lease is released early only for a failure that could not have reached the issuer or a refusal the issuer named itself; a timeout, a gateway's 5xx or a 2xx carrying no usable token response keeps it, because in all three the token may already have rotated |
| §4.3 network constraints for arbitrary URLs | Done: resolve-then-check every address, credential bound to an origin, redirects refused |
| §5 stdio, local and private MCP | Configuration and validation only; see above |
| §6 connection model and authorization versions | Done: `0008`, five version columns, config / authorization / capability / policy revisions, legacy name list mapped and quarantined. `credential_version` moves only when a credential is written |
| §7 dynamic capabilities and read-write authorization | Done: `deny` / `confirm` / `allow` per capability, fingerprints, held calls with a single-winner claim |
| §8.3 attempt, follow-up and fork | Done: bindings read per call, fork copies the selection, the claim carries aliases |
| §9 API and interface contract | Done, including the connector API; 19 routes in `create_app()` |
| §10 migration and deployment | Normal deployment, no feature switch; retained containers install reviewed helpers and Kilo executables before launch |
| §11 quality gate | `make check`, database tests, web checks, and `tests/external/test_resident_mcp_parity.py`; run commands below |

## Runtimes (design §8.2)

Every adapter that reports MCP points its CLI at `/v1/agent/mcp` — the merged
host — as a single entry named `session`. The per-connection route
(`/v1/agent/mcp/<alias>`) is not used by any of them: it forwards one
connection's protocol untouched, which is a service a legacy SSE server cannot
answer (its SSE address replies 405 to the POST) and a surface that cannot merge
or namespace several connections.

| Runtime | MCP | Why |
| --- | --- | --- |
| `claude-code` 2.1.220 | Yes | `--mcp-config` with `--strict-mcp-config`; the entry is `http` at the merged endpoint, the token referenced as `${AGENT_MCP_TOKEN}` |
| `opencode` 1.18.9 | Yes | reviewed wrapper writes `OPENCODE_CONFIG`; `OPENCODE_DISABLE_PROJECT_CONFIG` covers the project file, the project directory and its plugins |
| `pi` 0.73.1 | Yes | reviewed `pi-mcp-bridge.ts` loaded with `-e`; `--no-extensions` shuts out every other source. Verified end to end against the pinned CLI, at the merged endpoint |
| `codex` 0.145.0 | Yes | Trusted guest-root bootstrap installs the exact deployment relay URL into system requirements; exec and app-server share token-env configuration. App-server verifies MCP initialization before starting the turn. |
| `kilo` 7.4.17 | Yes | The image builds a pinned source revision with the reviewed legacy-loader isolation patch. The wrapper supplies the platform relay, and serve verifies its connection before reporting ready. |

## What "done" would still require

1. **stdio execution**: a managed worker and a user connector runtime. New
   components, not changes to this repository.
2. **Elicitation**: a user form, a persisted request, and a continuation that
   carries the answers back — which the merged endpoint can already express
   (`inputResponses` / `requestState` are implemented on the client side).
3. **Cancellation and progress on the merged endpoint**: the surface would have
   to hold a call open across HTTP requests, or report them as unsupported
   rather than accepting the notification silently.
4. **Deployment acceptance**: a staging run, a real OAuth round trip, and
   interoperation with two real third-party servers and one real stdio server.
5. **Durable protocol sessions**: cross-process ownership and event replay;
   the shipping merged host's live transports are process-local.

## How to check this document

```bash
make check
uv run pytest -q -m dbtest  # needs a disposable PostgreSQL database
MCP_TEST_SANDBOX_IMAGE=<built-image> uv run pytest -q -m external tests/external/test_resident_mcp_parity.py
```

The external parity test crosses the runtime boundaries: a real relay listener,
a real database, and all five pinned CLIs driving the merged endpoint against
synthetic model and MCP endpoints. Each case also reads an expanded resource
template through the relay using the member's current token. That resource
request is made by the test's protocol client, not by the native CLI. The suite
requires the built sandbox image and a disposable PostgreSQL database.

## Resource identity and templates

The merged endpoint qualifies every static resource and resource template with
its connection ID. The prefix remains stable when bindings are reordered, and
two connections can expose the same original URI without one hiding the other.
Read results, resource links and embedded resources in tool or prompt results
use the same prefix. URI percent encoding is preserved byte for byte when
forwarding to the upstream server. The per-connection route keeps original URIs.

An exact static resource uses its own rule. Otherwise the concrete URI must
match a discovered template selected for this session. An explicitly denied
matching template refuses the request, and multiple selected matches return an
ambiguity error instead of guessing a policy. Confirmation records both the
concrete upstream URI and MCP method, so approving one resource read cannot
approve a different URI or a subscription. Dispatch rechecks the current
connection, policy and attempt fence.

`mcp/resource_templates.py` matches RFC 6570 expansions with bounded template,
URI, expression, cache and compiled-pattern sizes. RE2 avoids backtracking on
the lexical check. A bounded variable search then uses `uritemplate` to verify
that one consistent assignment recreates the entire URI, including repeated
variables, scalar prefixes, lists, maps and omitted variables. Ambiguous searches
stop after 16,384 steps and refuse authorization instead of falling through to
another template's rule. Unselected templates do not enter this search.
`tests/unit/test_mcp_resource_templates.py` checks
operators, encoded boundaries, invalid inputs and adversarial patterns;
`tests/servers/test_mcp_resource_routes.py` checks routing and authorization
through the API, including pending approval invalidation.

Disabled session bindings are refused before loading or refreshing credentials,
including direct-route handshakes and stream operations. Disabled or disconnected
connections also skip refresh. The merged catalogue continues listing and calling
healthy connections; the API regressions assert that the inactive connection
never enters refresh or reaches its upstream server.

Merged catalogues read stored discovery snapshots without contacting OAuth
endpoints. A tool call, prompt fetch or resource read refreshes only its selected
connection, so a slow issuer cannot block healthy connections. After that wait,
dispatch rechecks the current attempt lease, binding, policy and connection
revisions. Revocation or rebinding during refresh refuses the operation. A failed
connection returns an MCP error while the merged endpoint remains usable.

When another worker holds the refresh lease, a still-valid access token remains
usable immediately. An already-expired token waits only for that holder's result,
bounded by the refresh lease. An absent or exhausted lease produces a refusal;
the waiter never submits a second refresh or releases another worker's lease.
Cancellation propagates without dispatching an upstream operation.

Each connection has a unique alias within a conversation. Creation and selection
APIs reject duplicate explicit aliases and repeated connection IDs; generated
aliases reserve explicit names before choosing a bounded suffix. The Tools panel
also avoids duplicate names and offers a corrected selection for previously
stored duplicates. Until saved, ambiguous legacy aliases refuse dispatch instead
of selecting the first connection. Other connections remain usable.
New conversations also enable resource-only and prompt-only selections, validate
their policies, and leave a selection disabled when every capability is denied.

Saving a selection locks the conversation's binding set in one transaction.
Changed and reattached connections receive a fresh database-allocated policy
revision, and their pending or approved invocations expire immediately. Unchanged
connections retain their revisions and approvals; already dispatching calls
retain their outcomes. Concurrent saves cannot combine two different selections.
The Tools editor locks selection controls during refresh, attachment and saving,
including when the panel is reopened before an operation completes.

Both listeners periodically reclaim process-local upstream sessions after a job
ends or its connection is removed, disabled or deleted. Each maintenance pass
uses the current attempt fence and selection, limits concurrent database lookups,
and preserves sessions when an authority lookup fails or times out. A stale
cleanup result cannot close a replacement opened while the lookup was pending.
Live forked jobs retain their independent sessions. The MCP-only listener runs
this maintenance without running the control plane's grant reaper or publisher.
Overdue pending and approved invocations also expire during maintenance.

Session shutdown separately bounds graceful upstream termination and local
cleanup; cancellation of the caller cannot skip the latter. Transports release
their own HTTP clients and legacy SSE readers on successful close, failed
handshake, invalid endpoint announcement and cancellation, while leaving borrowed
clients open. Listener shutdown closes the remaining host sessions and refuses
connections that finish opening after shutdown starts.

Connection tests close their diagnostic host on success, error and cancellation.
OAuth returns to the existing `/integrations` page using the configured web
base, independently of the registered API callback base. The page shows failure
details or, after verifying a stored credential, success and the next Test step.
Discovery snapshots and status updates compare the configuration, authorization,
capability and credential versions they observed; a changed connection, rotated
token or newer result returns a conflict instead of overwriting the current
catalogue or status. A diagnostic that refreshes its own token publishes against
the refreshed version. OAuth callbacks commit their draft status with the new
credential, so they cannot reset a subsequent successful test. Target edits
invalidate the old catalogue and pending OAuth flows in the same transaction as
the configuration change, before new-target discovery or authorization can
publish. Renames preserve the tested state and catalogue.
Optional capability negotiation advertises only roots. Elicitation is reported
as unsupported until there is a user interaction handler.

A real Kilo confirm run exposed an intermittent socket-close failure on its
first tool request; only four of the five expected calls reached the upstream.
The wrapper now requests `Connection: close` on the relay hop, avoiding reuse of
idle sockets without replaying a tool call. The Kilo scenarios verify distinct
TCP connections for protocol requests as well as exact upstream invocation counts.
This uses Bun's documented
[per-request connection-pooling control](https://bun.sh/docs/runtime/networking/fetch).

## Approval and native-session continuity

Confirmation keeps the original MCP request open. The conversation shows a
pending decision beside the composer and opens the Tools pane to review it.
Approval claims the invocation once under the current attempt, connection and
policy fence, then returns the result to the waiting CLI. Denial, cancellation,
expiry or changed authorization prevents dispatch. Waiting does not hold a
database transaction. CLI timeouts cover the ten-minute approval window plus
the maximum configured fifteen-minute upstream timeout.

A new attempt restarts the member's native process with its new MCP token and
resumes the same native session. Fork retains the selected bindings and launches
under the child's own fence. No dispatched external write is automatically retried.

## Recorded validation (2026-09-13)

- Connection API suite: 57 passed, including six OAuth-return cases for success
  and failure under root, prefixed and separate API/web origins. MCP page/API
  interaction suite: 48 passed, including authorization feedback and Test continuation.
- Credential/connection lifecycle regression suite: 103 passed, with seven new
  unit cases for token rotation before and after discovery, a losing refresh,
  successful preflight refresh, late OAuth completion and discovery after an edit.
- Diagnostic/protocol regression suite: 159 passed, including 16 new cases
  covering diagnostic cleanup, stale success and failure results, optional
  capability declarations and roots responses over both HTTP transports.
- MCP PostgreSQL suite: 58 passed, including eight additional cases for credential
  rotation, fenced authorization status and target-edit invalidation, alongside
  changed connection versions and competing diagnostic snapshots.
- `make check`: 3,867 tests passed, plus formatting, lint and mypy.
- Real Kilo after the socket-reuse fix: all three allow/confirm/name-normalization
  cases passed, including fresh TCP connections, exact invocation counts,
  approval continuation, follow-up, resume and fork. Wrapper/deployment unit
  coverage: 88 passed.
- Session lifecycle/transport/relay regression suite: 111 passed, including 29
  new ownership, cancellation and maintenance cases. These cover completed jobs,
  live forks, disconnected connections, failed or slow authority lookups,
  concurrent replacement, bounded cleanup concurrency and listener shutdown.
- Refresh/relay/approval regression suite: 173 passed. Cases cover an issuer
  that never responds without blocking another connection, authority changes
  during refresh across tools, resources, prompts and direct routes, pending
  approval invalidation, current-credential dispatch, expired access tokens
  during refresh contention and cancellation without releasing another lease.
- PostgreSQL tier: 431 tests passed, including 58 MCP store regressions for
  permission changes, reattachment, unchanged approvals, concurrent saves and
  upgrading the policy revision allocator above historical approval versions,
  diagnostic version fences, concurrent discovery results, credential rotation,
  atomic authorization status and target-edit invalidation.
- Web: 1,305 tests passed; type-check, ESLint and changed-file Prettier checks passed.
  The 13 session-panel cases include duplicate-name repair and selection controls
  during pending operations. New-conversation API cases also cover resource-only
  and prompt-only permissions, including denial and invalid policy values.
- Built Linux arm64 sandbox: five pinned CLIs, each with `allow` and `confirm`.
  All fifteen cases passed, including long normalized tool names. Across 66
  tool calls the suite checks results, approval without a second prompt, live
  follow-up, native resume, native Fork, token rotation and refusal of superseded
  tokens. Codex exec and Kilo run are checked
  alongside the resident app-server/serve paths. Kilo's reviewed build is also
  installed through the retained-container upgrade path under dropped capabilities.
- All fifteen cases also read an expanded resource template using the member's
  current credentials, covering both an ordinary template and a repeated
  reserved-path variable: 30 resource reads in total. Confirmation and invocation
  completion are checked for `confirm` cases. These additional resource reads
  use the test protocol client.
- Resource/template/relay regression suite: 271 passed. The template matcher
  also accepted all 383 nonempty expansions in the public
  [URI Template test corpus](https://github.com/uri-templates/uritemplate-test)
  (`spec-examples`, `spec-examples-by-section`, and `extended-tests`). Empty
  expansions are refused because resource reads require a nonempty URI.
- Repository-supplied MCP files do not execute their injected command; Kilo's
  resolved configuration contains only the selected platform relay.

The sandbox image CI cell runs this same suite against its built amd64 image
using a disposable PostgreSQL container. Its model and upstream MCP services
are local protocol fixtures, not third-party accounts.
