# Per-user MCP: conformance against the design

- Date: 2026-09-11.
- Baseline: `ba27315` on `realtmxi/codex/mcp-v1-design`, plus the review fixes
  through `4c8fb37`, the two P2 items below (`0008_mcp_refresh_lease`) and the
  recheck fixes; merged with `dev` at `59bf2cc`, which the migrations now sit on
  top of (`0006_host_capacity → 0007_generic_mcp_connections →
  0008_mcp_refresh_lease`).
- Design: [`2026-09-11-per-user-mcp-v1-design.zh.md`](2026-09-11-per-user-mcp-v1-design.zh.md).

This is an audit, not a plan. It states, dimension by dimension, what the code
does, where the evidence is, and what is missing — because "we implemented the
design" is a claim that has to be checkable, and the parts that are missing are
easier to argue about when they are written down.

Three things are deliberately not in scope here: whether the *deployment* works
(no staging run has been done), whether a *real* third-party server interoperates
(the end-to-end test uses a stub upstream), and the two runtimes whose blockers
are outside this repository.

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
| Dynamic tools / resources / prompts | Done | `protocol.discover`, `_host_listing`, `_host_fetch`; the merged endpoint's handshake advertises all three families, which is what makes a client ask |
| All five coding agents | **3 of 5** | `runtimes_supporting_mcp()`; all three are given the merged endpoint as a single entry | Codex and Kilo; see below |
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
| External URI is the server's own resource identifier | Done | `resources/list` publishes the upstream URIs; routing is a lookup in the catalogue |
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
| §4.2 auth and connection diagnosis | Done: staged diagnostics recorded on the connection; OAuth with discovery, registration precedence, PKCE, `resource`, single-use state, refresh and revocation. A refresh is claimed under a lease with a generation (`0008`), so two workers cannot spend one rotating token, and a superseded refresher's write matches no row. The lease is released early only for a failure that could not have reached the issuer or a refusal the issuer named itself; a timeout, a gateway's 5xx or a 2xx carrying no usable token response keeps it, because in all three the token may already have rotated |
| §4.3 network constraints for arbitrary URLs | Done: resolve-then-check every address, credential bound to an origin, redirects refused |
| §5 stdio, local and private MCP | Configuration and validation only; see above |
| §6 connection model and authorization versions | Done: `0007`, five version columns, config / authorization / capability / policy revisions, legacy name list mapped and quarantined. `credential_version` moves only when a credential is written |
| §7 dynamic capabilities and read-write authorization | Done: `deny` / `confirm` / `allow` per capability, fingerprints, held calls with a single-winner claim |
| §8.3 attempt, follow-up and fork | Done: bindings read per call, fork copies the selection, the claim carries aliases |
| §9 API and interface contract | Done, including the connector API; 19 routes in `create_app()` |
| §10 migration and deployment | Done as a deployment switch plus the operating notes; no staging run |
| §11 quality gate | `ruff check`, `ruff format --check`, `pydocstyle`, 3038 non-database and 355 database tests, the web app's own gate (type-check, eslint, prettier, 1184 tests), and one external end-to-end test |

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
| `codex` 0.145.0 | No | `/etc/codex/requirements.toml` gates MCP by name **and exact URL identity** (`RawMcpServerUrlIdentity { url }`; no prefix or regex matcher in the pinned binary), the file is baked into the image, and the relay URL is a runtime value. The merged host is the shape that *can* satisfy it — one fixed name and URL — but shipping it is a packaging change, not an adapter flag |
| `kilo` 7.4.17 | No | `KILO_DISABLE_PROJECT_CONFIG` does not cover the fork's legacy migrator, which reads `<project>/.kilo/mcp.json` unconditionally. A repository could hand the agent tools of its own choosing, so the runtime refuses rather than promise a bound it does not have |

## What "done" would still require

1. **Codex**: build the image — or mount a config over `/etc/codex/` — with that
   deployment's exact relay URL (`<base>/v1/agent/mcp`, one fixed name) in the
   allowlist. Needs a docker build to verify.
2. **Kilo**: close the legacy MCP path, either upstream or by not exposing
   `.kilo/mcp.json` to the sandbox.
3. **stdio execution**: a managed worker and a user connector runtime. New
   components, not changes to this repository.
4. **Elicitation**: a user form, a persisted request, and a continuation that
   carries the answers back — which the merged endpoint can already express
   (`inputResponses` / `requestState` are implemented on the client side).
5. **Cancellation and progress on the merged endpoint**: the surface would have
   to hold a call open across HTTP requests, or report them as unsupported
   rather than accepting the notification silently.
6. **Deployment acceptance**: a staging run, a real OAuth round trip, and
   interoperation with two real third-party servers and one real stdio server.

## How to check this document

```bash
uv run pytest -q -m "not external and not dbtest"   # 3038
uv run pytest -q -m dbtest                          # 355, needs Postgres
uv run pytest -q tests/external/test_agent_mcp_end_to_end.py  # 1, needs npm and Postgres
```

The external test is the one that crosses every seam at once: a real relay
listener, a real database, and the pinned pi driving the merged endpoint against
a stub upstream. It skips rather than fails without Postgres, npm or the
network.
