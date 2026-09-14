# Per-user MCP: conformance against the design

- Updated: 2026-09-13.
- Baseline: PR #207's generic MCP foundation, integrated with the current
  capability-based storage architecture on `dev`. Migration order:
  `0007_attempt_turn_resources → 0008_generic_mcp_connections → 0009_mcp_refresh_lease`.
- Design: [`2026-09-11-per-user-mcp-v1-design.zh.md`](2026-09-11-per-user-mcp-v1-design.zh.md).

This is an audit, not a plan. It states, dimension by dimension, what the code
does, where the evidence is, and what is missing — because "we implemented the
design" is a claim that has to be checkable, and the parts that are missing are
easier to argue about when they are written down.

The external conformance tests run the built sandbox image, real CLIs, relay
and PostgreSQL against synthetic model and MCP endpoints. They do not establish
production deployment acceptance or third-party OAuth interoperability.

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
| Opaque session / event handles bound to connection, revisions, epoch, member, attempt, owner lease generation | Done | the handle is `mcp1.<digest>` over connection, member and lease generation, and `McpHost` records every session it opens in `agent_mcp_transport_sessions` with those columns. The row is a *record*, not the session — a socket cannot be handed back by a table — and what it buys is finding a session from elsewhere |
| Rebuild the protocol session when the owner is superseded, without resending dispatched work | Done | the key includes the lease generation, so a takeover produces a new session; `McpHost.reap_superseded` closes the superseded one, deletes its record and clears any record another instance left under that member. The map no longer grows for the lifetime of the process |
| Bounded request, frame, cumulative response and events | Done | `jsonrpc.MAX_MESSAGE_BYTES`, `sse.MAX_EVENT_BYTES`, `transports.MAX_RESPONSE_BYTES` |
| SSE handles LF / CRLF and fragmentation, and closes on overflow | Done | `mcp/sse.py`; 6 cases |
| Separate finite timeouts for execution, confirmation and subscription | Done | a held call has the four-clock contract in `mcp/invocations.py`, and execution takes a per-connection `request_timeout_s` (5-900s) that both the relay's client and the host's transport honour. Unset means the 120-second default, which is what every existing row got from the migration |

### Optional capabilities (design §8.1, second table)

| Capability | State | Note |
| --- | --- | --- |
| Elicitation | **Not implemented** | the host answers a server that asks with a protocol error rather than inventing consent; there is no user form |
| Sampling | **Not implemented** | refused with a diagnosis; no model authorization path |
| Roots | Partial | answered with the empty set: the platform exposes no workspace root, which is the conservative reading of "only roots the user authorized" |
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
server-supplied templates. `tests/unit/test_mcp_resource_templates.py` checks
operators, encoded boundaries, invalid inputs and adversarial patterns;
`tests/servers/test_mcp_resource_routes.py` checks routing and authorization
through the API, including pending approval invalidation.

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

- `make check`: 3,618 tests passed, plus formatting, lint and mypy.
- PostgreSQL tier: 407 tests passed.
- Web: 1,298 tests passed; type-check, ESLint and changed-file Prettier checks passed.
- Built Linux arm64 sandbox: five pinned CLIs, each with `allow` and `confirm`.
  All fifteen cases passed, including long normalized tool names. Across 66
  tool calls the suite checks results, approval without a second prompt, live
  follow-up, native resume, native Fork, token rotation and refusal of superseded
  tokens. Codex exec and Kilo run are checked
  alongside the resident app-server/serve paths. Kilo's reviewed build is also
  installed through the retained-container upgrade path under dropped capabilities.
- All fifteen cases also read an expanded resource template using the member's
  current credentials, with confirmation and invocation completion checked for
  `confirm` cases. These additional resource reads use the test protocol client.
- Resource/template/relay regression suite: 146 passed. The template matcher
  also accepted all 383 nonempty expansions in the public
  [URI Template test corpus](https://github.com/uri-templates/uritemplate-test)
  (`spec-examples`, `spec-examples-by-section`, and `extended-tests`). Empty
  expansions are refused because resource reads require a nonempty URI.
- Repository-supplied MCP files do not execute their injected command; Kilo's
  resolved configuration contains only the selected platform relay.

The sandbox image CI cell runs this same suite against its built amd64 image
using a disposable PostgreSQL container. Its model and upstream MCP services
are local protocol fixtures, not third-party accounts.
