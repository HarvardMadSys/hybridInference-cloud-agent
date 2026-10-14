# Side Chat: PR #275 review implementation and handoff

This self-contained document records the requested corrections to
[PR #275](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/275),
original head `b40921a`, branch `codex/web/side-chat`, base `dev`. The review
describes “30 items” but contains **33 numbered subitems**; every original ID is
retained below. These resolutions are implemented in this PR. Paths and line
numbers refer to the revised code; use the named functions if later edits move
them. Read AGENTS.md and CONTRIBUTING.md before changing code.

## Product requirements to preserve

- Place **Side Chat immediately after Tools** in the existing Git / Terminal /
  Files / Tools workspace tab row, using the existing toggle and layout.
- Open to an empty transcript and bottom composer. No welcome/context text,
  extra start modal, separate top-level interaction or new route.
- Reuse the native fork API, pinning the main history boundary on opening and
  waiting for that turn to settle if needed. Earlier history stays in the native
  child; later main turns are independent. Do not introduce transcript replay.
- Keep a durable child in the same writable sandbox. It has independent
  messages, model/effort, send/stop, event stream and tool permissions. Closing
  a pane does not delete it; it remains visible in the project list.

## Corrections in priority order

### P0-1

Entry point: `cloud_agent/api/routes.py:2098`.

Return the hydrated owner read after both initial follow-up creation and
idempotent replay. The command row lacks retained membership, so serializing it
directly commits the turn but makes the browser reject its identity. Keep the
browser guard; add the real PostgreSQL-to-HTTP contract test described below.

### P1-1

Entry point: `web/src/components/features/agents/useSideChatSession.ts:16`.

Persist a caller-owned operation ID before POST. forkAgentJob must not clear or
rotate explicit IDs. Persist a usable child locator before validating it; an
incomplete receipt retains the unknown operation for replay, while an invalid
child identity retries reading the same child.

### P1-2

Entry point: `web/src/components/features/agents/useSideChatSession.ts:178`.

Distinguish new, unknown, busy and failed operations. Only an ambiguous outcome
bypasses mutable liveness checks to recover the SAME ID. Known failures recheck
identity, archive/context state and native eligibility before rotation. The
storage repository now refuses new archived-thread forks under lock, after
allowing existing-operation replay.

### P1-3

Entry point: `web/src/components/features/agents/useSideChatSession.ts:94`.

Use capped delays of 3, 6, 12, 24 and 30 seconds. Recheck the anchor with GET
before each eligible POST; never POST while the anchor remains busy. A group
conflict can still require POST to discover, but uses the same backoff. Pause
when hidden or inactive.

### P1-4

Entry point: `web/src/components/features/agents/useSideChatSession.ts:24`.

Remove the module cache and validate durable locators. A confirmed deleted child
clears its locator and offers Retry. Corrupt records, changed sources and
invalid membership offer explicit Start a new side chat. Do not silently replace
an unknown durable result or discard successful receipts after transient read
failures.

### P1-5

Entry point: `web/src/components/features/agents/useSideChatSession.ts:108`.

Use localStorage and a Web Lock spanning read, fork and receipt persistence. A
second window re-reads under the same lock. Scope by encoded owner and main
thread; listen for storage changes. Coordination is per browser profile/origin,
not account-global across devices.

### P1-6

Entry point: `web/src/components/features/agents/useSideChatSession.ts:205`.

Preserve the approved one-click tab. Show a creation toast and retain the
existing durable child sidebar title marker (fork). This selects the review’s
visibility alternative instead of adding a modal or another start interaction.
The child shares writable files and is not temporary.

### P2-1

Entry point: `web/src/components/features/agents/SideChat.tsx:276`.

Own Escape from the active Side Chat tab, panel and document body in capture
phase; prevent the main handler and stop only the child. Textareas/inputs keep
their keyboard behavior. Test a running main conversation with a real
owner/source identity as well as child stop.

### P2-2

Entry point: `web/src/components/features/agents/SideChat.tsx:101`.

Track touched model/effort controls. Seed inherited child values only when
untouched, so preparation cannot overwrite a user’s selection.

### P2-3

Entry point: `web/src/components/features/agents/SideChat.tsx:224`.

Keep send beside stop while running and queue through existing follow-up
semantics. While preparing, Enter explains the state and retains the draft.
Shared keyboard handling preserves Shift+Enter and IME behavior.

### P2-4

Entry point: `web/src/components/features/agents/SideChat.tsx:99`.

Bind the open tool panel to a job ID, not a boolean. Adopting another turn
returns to chat rather than silently repointing permissions to a different job.

### P2-5

Entry point: `web/src/components/features/agents/conversationJob.ts:31`.

Use currentAttemptNo, falling back to the last known attempt after its active ID
is released. Do not interleave every retry in the current answer.

### P2-6

Entry point: `web/src/components/features/agents/conversationJob.ts:22`.

Share the main owner-note rule. Suppress successful bookkeeping for
done/needs_review; keep useful failure details and the existing missing-details
fallback.

### P2-7

Entry point: `web/src/components/features/agents/SideChat.tsx:302`.

Render local validation and action failures directly, translating sideChat keys.
Do not reinterpret them with ownerFailureMessage, which classifies backend run
failures.

### P2-8

Entry point: `web/src/components/features/agents/SideChat.tsx:189`.

Renumber only displayNo after filtering inherited turns. Preserve native turnNo,
event/job IDs and lifecycle filtering.

### P2-9

Entry point: `web/src/components/features/agents/useSideChatSession.ts:101`.

Keep waiting status stable during rechecks. Schedule run directly instead of
restarting the effect through a tick; do not disable exhaustive-deps.

### P2-10

Entry point: `web/src/components/features/agents/ChatMessages.tsx:506`.

Guard the whole execution footer by onOpenRun, removing blank margins when Side
Chat supplies no execution-navigation action.

### P3-1

Entry point: `web/src/components/features/agents/useAgentJobs.ts:320`.

Gate fetching, streaming and metadata polling with active; default true
preserves other callers. Close hidden side SSE, ignore irrelevant session events
and refetch on return. Document visibility also gates side work.

### P3-2

Entry point: `web/src/components/features/agents/useAgentJobs.ts:543`.

Memoize the display model on stable API/events/patch/thread inputs. Memoize
SideChat and omit its heavy transcript while inactive; draft typing should not
rebuild history.

### P3-3

Entry point: `web/src/components/features/agents/useAgentJobs.ts:532`.

Filter copied jobs and their messages before display adaptation when
hideForkedHistory is requested. Native/server history remains complete. The pane
must not construct every inherited historical turn only to discard it afterward.

### P3-4

Entry point: `web/src/components/features/agents/SideChat.tsx:198`.

Depend on completion and identity changes, using a current-job ref. Draft
keystrokes must not parse or rewrite the read-receipt blob.

### P3-5

Entry point: `web/src/components/features/agents/useSideChatSession.ts:282`.

Do not broadcast global sidebar invalidation for every advance. Creation still
announces the new thread; regular status polling handles normal turn changes.

### P4-1

Entry point: `web/src/components/features/agents/JobDetail.test.tsx:239`.

Add an integration fixture with a signed-in owner, source sandbox/thread/native
eligibility and valid child receipt. Test lazy creation, placement and Escape
isolation; a userless fixture cannot exercise preparation.

### P4-2

Entry point:
`web/src/components/features/agents/useSideChatSession.test.tsx:44`.

Use fixed test identity and reset mocks/storage between tests, with no module
cache. Cover actual stored receipt restoration, concurrent consumers,
deleted/corrupt records, failed/unknown operation retries and legacy migration.

### P4-3

Entry point: `web/src/components/features/agents/SideChat.test.tsx:357`.

Test newer child turns created outside the pane, rejected mismatched identity,
and tools-panel reset on adoption. A committed response must clear the submitted
draft before local recovery.

### P5-1

Entry point: `web/src/components/features/agents/ConversationComposer.tsx:6`.

Share model ordering, payload overrides, textarea submission and
send/spinner/hint components between main and side composers. Keep differing
preparation, draft and stop state local rather than adding a large generic hook.
This is a narrower extraction than the review’s suggested shape.

### P5-2

Entry point: `web/src/components/features/agents/conversationJob.ts:4`.

Use one latest-job helper for anchor selection, restoration/adoption and
AgentsSidebar, with turn_no defaulting to one and consistent tie behavior.

### P5-3

Entry point: `web/src/components/features/agents/conversationJob.ts:10`.

Share active job/conversation rules across preparation, streaming/polling and
trajectory context. Share the lost-context predicate between main/side composers
and map API field names into it during preparation.

### P5-4

Entry point:
`web/src/components/features/agents/conversationTurnNavigation.ts:324`.

Make describeCurrentTurn own fallback responses and use
describeConversationTurns in SideChat. Render promptCard through a shared
user-message renderer with copy action and stable prompt anchor; do not invent
an id=-1 message.

### P5-5

Entry point: `web/src/components/features/agents/sideChatStorage.ts:2`.

Use agents.sideChat.session/draft namespaces with encoded owner/thread segments
and an unambiguous delimiter. Keep operation IDs inside records. Migrate old
sessionStorage locators and API pending IDs without silently re-namespacing
in-flight forks.

### P5-6

Entry point: `web/src/components/features/agents/useSideChatSession.ts:53`.

Return a record or null from childSession and boolean from advance. Clear the
submitted draft after successful POST, before adoption. An invalid receipt says
Message sent / reloading rather than falsely presenting that text as unsent.

### P5-7

Entry point: `web/src/components/features/agents/sideChatStorage.ts:10`.

Clear the current user’s side drafts on logout/auth expiry and notify mounted
panes. Keep other users’ state. Offer Discard draft for archived/lost side
chats. Durable locators contain IDs, not text.

### P5-8

Entry point: `web/src/lib/i18n/dictionaries/en.ts:13`.

Add matching English and Chinese composer, status, recovery, local-error and
accessible-region strings. Keep the product tab name Side Chat and the existing
general transcript conventions.

## Real server contract and tests

The failure chain is the `JOB_SELECT` projection in
`cloud_agent/storage/_sql/jobs.py:65`, conversion in
`cloud_agent/storage/_sql/rows.py:67`, and `_job_response` in
`cloud_agent/api/routes.py:506`. The command result lacks the
retained-membership join. Returning it directly from follow-up POST produced
null sandbox/fork identity and false workspace_shared despite committing a valid
turn. Both new and replayed responses now use the hydrated owner read. This
intentionally changes Python and must enable the Python CI tier; do not preserve
the original web-only classification by weakening client validation.

`tests/integration/storage/test_agent_job_store.py` includes
`test_side_chat_follow_up_http_receipt_matches_retained_owner_read`: real
PostgreSQL command, owner query and FastAPI route, with only authentication,
repository entitlement and external dependencies overridden. It proves the raw
command lacks the join, asserts both HTTP receipts and only one inserted
follow-up, and checks a normalized projection against
`tests/fixtures/side_chat_job_receipt.json`. `SideChat.test.tsx` imports that
same fixture. A focused server route regression in
`tests/servers/test_agent_jobs_api.py` checks both initial and replay paths too.
Web mocks alone do not establish the server contract.

Additional tests cover StrictMode/concurrent consumers, unknown versus failed
operation recovery, archive checks, busy backoff, stored/deleted/corrupt
locators, legacy pending IDs, explicit API idempotency ownership, source
boundaries, independent sends/stops/streaming, hidden subscriptions, draft and
picker retention, external latest-turn adoption, attempt filtering, visible turn
numbering, tools identity, read receipts and user-scoped logout cleanup.

## Checked and cleared

Preserve these exclusions from the review; do not expand the patch to “fix”
them:

1. The initial ChatMessages extraction was verified as verbatim except exports.
   The conditional footer and shared prompt-card renderer are intentional later
   corrections; other tool/message rendering needs no rewrite.
2. `turns.length && condition ? node : null` does not render a falsy zero.
3. `useAgentJob('')` already avoided I/O. The new active option addresses
   hidden, nonempty child IDs.
4. The original manual/side fork scopes had no reachable key collision. New
   operation ownership addresses recovery and naming, not that nonexistent bug.
5. Validated child thread non-null assertions are safe.
6. `sandbox_busy` reaches the client with that exact code.
7. The old request-promise map cleaned up correctly. Web Locks replace it for
   cross-window coordination, not to fix a claimed livelock.
8. The original opening-boundary selection rule is sound and must be preserved.
9. Page-to-JobDetail userId wiring was already correct and typed.
10. The claim against `McpApprovalNotice active={active && running}` was
    refuted. Hidden panes need not poll an invisible badge. A global
    pending-approval inbox is a separate feature outside this PR.
11. No applicable CLAUDE.md adds rules. AGENTS.md and CONTRIBUTING.md govern
    this change; preserve test assertions, omit credentials and target dev.
12. Unassigned liveNote behavior and permitted long className strings are
    pre-existing concerns, not defects introduced by this feature.

## Verification commands

From the repository root:

```sh
env -u CODEX_VERSION make check
# Use an isolated UTF-8 PostgreSQL database with a Unicode-aware locale.
env -u CODEX_VERSION TEST_DB_PORT=55439 TEST_DB_NAME=side_chat_test_review uv run pytest -q -m dbtest
cd web
npm test
npm run type-check
npm run lint
npm run format:check
npm run build
```

The CODEX_VERSION unset removes only the desktop-injected CLI version override
that deployment tests intentionally reject. It does not change runtime pins. The
database name/port above identify the isolated review cluster, not a required
service configuration. After merging dev at `e916c01`, the checks returned 4,135
standard Python tests, 466 PostgreSQL tests, and 1,408 web tests passing.

Browser acceptance: open the pane in two tabs/windows of one origin/profile and
verify one fork, blank initial body, draft retention across workspace tabs,
side-only send/stream, refresh restoration and unchanged main URL. Stop targets
only the child, and returning from a hidden pane reads authoritative state. The
browser smoke test passed in two tabs with exactly one fork and one side
follow-up, including streamed reply, draft retention, and refresh recovery. A
fixture API is sufficient for this interaction smoke test; use the real
PostgreSQL HTTP regression above for the response contract.

## Boundaries and deliberate choices

Web Locks and localStorage are required for coordinated creation; unavailable
storage/coordination fails closed with a visible retryable error. The
association is browser-profile-local, not shared across devices. Clearing site
data removes locators without deleting durable children. Unsent drafts use
per-tab sessionStorage. The child shares writable files, so sibling edits can
conflict; this patch adds no cross-thread file serialization.

The creation toast plus existing sidebar fork marker implements the review’s
visibility alternative while preserving the approved minimal interface. Shared
composer primitives cover behavior that must agree; pane-specific lifecycle and
draft state remain local. These are explicit design choices rather than
omissions.

No runtime adapter, provider-specific fork implementation, sandbox security
boundary, or database schema changes. Live provider/container end-to-end
execution is separate from and was not performed by the tests above.
