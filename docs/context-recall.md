# Native conversation recall

A retained native session and a correct answer about its history are separate
checks. PR96's local Flash reproduction kept every user/assistant message in the
request, while the model claimed there had been no previous conversation.
A small synthetic reproduction showed that file-memory instructions could
trigger this confusion. Removing auto memory did not fix the full CLI case.

Native CLIs own the conversation history. Cloud Agent passes each user message
unchanged and resumes the same native session; it does not append a reminder to
persuade the model that history exists. The earlier continuation-prompt experiment
has been withdrawn. Its passing behavior samples are not acceptance evidence for
the unmodified-input implementation.

The persistence fixes remain: allow a settled CLI to flush its native history,
report native API errors as failures, and retain a verifiably settled failed turn
for retry. Those fixes do not establish that the downstream model will correctly
answer a recall question. The unprompted Flash recall failure remains unresolved.

A second, independent bug was reproduced with the pinned Claude 2.1.220 CLI
against a loopback fixture, without any model server. After a retryable API error
and a successful internal retry, a delayed native `system/api_error` record can
become the next user's parent, bypassing the successful assistant reply. Live
requests still contain that reply, but later native resume drops it.

The driver detects the CLI's wire `system/api_retry` event, completes the current
turn, and requests graceful process retirement after broker settlement saves its
native locator. The next turn resumes the same native session and container.
Uninterrupted successful turns continue using the live process. No conversation
replay, JSONL rewriting or continuity prompt is added. Previously damaged parent
chains are not retroactively repaired by this change.

## Native session identity at the shared boundary

The shared runner introduced by PR96 did not call the existing
`_verified_native_session_id` check before saving the broker's settled locator.
It could therefore accept a different session ID as a successful continuation.
That check is now applied to both successful and verifiably settled failed turns.
An unexpected locator fails the attempt before it can replace the durable one.

The broker also uses the retained member's locator when a launch omits it,
rejects a conflicting requested or observed locator, and verifies identity again
at settlement. The normal runner already rejects an absent resume locator for a
retained chat; the broker check protects that boundary independently. These
fault-injection cases prove a validation gap, not that a staging request actually
sent a missing or wrong ID.

Claude root stream events are checked before normalization can overwrite the
saved session ID or expose another session's output. Events with
`parent_tool_use_id` belong to a child agent and cannot replace the root locator.
The other bundled adapters already validate their native resume handshake.
This uses the CLI's own history; it does not reconstruct context from UI text.

## Other native CLIs

The same failure-retention policy now applies to every bundled CLI. Previously
the runner restricted it to Claude Code. A local Codex task remembered its
function name before an injected API failure, but its next follow-up returned
`sandbox_context_lost` despite the native session still existing. Pi also
reported a terminal API error as a successful empty turn.

| CLI | Evidence that the failed query ended normally |
| --- | --- |
| Claude Code | Native `result` boundary, including `is_error` |
| Codex | `turn/completed` for the active native turn ID |
| Pi | `agent_end`; the last assistant's `stopReason` determines failure |
| OpenCode / Kilo | Bridge `settled` for the active turn, emitted at native idle |

A command rejection, dead transport, timeout or cancellation cannot substitute
for this evidence. The broker must also complete activation cleanup and verify
that the resident transport and native locator remain usable before settlement.

OpenCode and Kilo additionally emit text parts for user messages. Their bridge
now forwards `message.updated` metadata whose session ID is nested in `info`.
The driver joins only parts owned by assistant messages, resolves late metadata
at settlement, and excludes events attributed to a previous bridge turn. This
fixes user prompts being copied into the visible assistant reply; native model
history was already separate from that incorrect display text.

The sandbox advertises `http-agent-live-bridge=3`, required by host attestation.
Deploy the broker/runner and sandbox image together. An image without this
helper version must not advertise readiness for the new host. This is not an
automatic migration or repair of legacy retained transcripts.

## Release scope and remaining findings

The Cloud Agent fixes cover the reproduced retry/flush history loss, retention
of verifiably settled failures across all five CLIs, Pi API-error reporting,
OpenCode/Kilo assistant-message ownership, and native session identity at the
shared runner/broker boundary. The local Responses fixture and smoke acceptance
checks are also corrected. No unresolved PR review comment was present when
this update was prepared.

Two investigation findings remain separate from those fixes:

| Finding | Status and ownership |
| --- | --- |
| Flash sometimes gives an incorrect missing-history explanation despite complete outbound native history. | Unresolved response behavior. This also occurred in fresh synthetic API requests without Cloud Agent or a CLI. Explicit reasoning effort and a newer CLI did not reliably eliminate it. The available client evidence does not identify the remote engine/template/model cause. |
| The gateway source snapshot drops Anthropic effort when the model configuration omits its allowed values, and loses historical thinking blocks during translation. | Reproduced locally; separate gateway/configuration patches exist outside this repository. They are not included in this PR and their deployment has not been verified. Fixing these compatibility defects is not proof that all Flash recall responses are fixed. |

The behavior preflight reports recall and observed thinking independently so
neither finding is concealed by a successful HTTP response. Existing damaged
native transcripts and old disposable chats are not migrated by this release.

## Verification before accepting a deployment

1. Require the existing CI Gate. Its pinned native CLI regression checks actual
   outbound requests, not just launch flags: user/assistant history must survive
   live follow-ups, immediate close/restore,
   a failed API call followed by a retry, and a successful internal retry on
   either the first or a later turn followed by multiple native restores.
   Assertions match exact user/assistant text, roles, order and repeated-message
   occurrences. Shared runner and broker tests also reject a changed native ID
   before persistence, including normally settled API failures.
   This uses a loopback model fixture
   without external credentials or inference charges.
2. Keep the deployment's exact host/API SHA, reverse broker probe and fresh
   heartbeat checks. A successful model call is not evidence that the intended
   host revision is serving jobs.
3. Run the opt-in behavior preflight from the target source checkout against
   the inference gateway used by the deployment:

   ```bash
   CLOUD_AGENT_TEST_API_KEY_FILE=/path/to/test-key \
     uv run python -m deploy.preflight.check_model_context \
     https://gateway.example.com --repetitions 3
   ```

   `CLOUD_AGENT_TEST_PROXY` can point to a local HTTP proxy when needed. The key
   stays in the trusted test process, never in an agent sandbox or process
   arguments. The script sends only new synthetic messages, uses
   `deepseek-v4-flash`, and prints replies and request IDs without auth headers.
   It checks the greeting, repeated questions, a task, randomly generated user
   and assistant facts with a requirement update, and a no-history negative
   control. Three repetitions make fifteen model calls and incur ordinary
   inference usage.

   A missing key, non-200 response, stream error, truncation, failure to recall
   the required facts, or a recognized missing-history claim produces a nonzero
   exit. The script requests the observed native CLI `adaptive/high` settings
   and also requires a thinking output. Each completed response independently
   reports `recall_passed` and `thinking_observed`, keeping the answer even if
   thinking is absent. Neither missing thinking output nor observed thinking
   proves anything about history loss or recall accuracy; inspect both checks.
   Thinking output also does not prove the exact effort reached the engine.
   Inspect failures; the conservative claim detector can also
   reject an answer that quotes a previous bad claim to correct it. HTTP 200
   alone is not a pass.
4. Check a new Cloud Agent chat and its follow-up/idle restore through the
   deployed app before calling rollout verification complete. Old disposable
   chats without native shared-live membership still need a new chat; this
   change does not migrate their history.

The behavior preflight is deliberately opt-in: CI pull requests never receive
an inference key, and no deployment credential is provisioned by this change.
It is not automatically run by the deploy workflows. Run it again after changes
to the gateway's model routes, model deployment, or CLI pin. Finite samples do
not guarantee that a model can never make another incorrect claim; they test
this reported recall failure separately from native history persistence.

## Current investigation evidence (2026-09-07)

A fresh synthetic reproduction bypassed Cloud Agent and the native CLI entirely:

- System: `You are a coding assistant. You can use persistent files for memories between conversations.`
- User: `hi`
- Assistant: `Hello! How can I help you today?`
- User: `我刚才让你做什么了`

Using only `deepseek-v4-flash`, twelve requests compared Anthropic default,
adaptive and enabled thinking with OpenAI default, enabled and disabled thinking
(two samples per configuration). Eleven replies claimed missing history; only
one OpenAI enabled sample recalled `hi`. All twelve responses were HTTP 200 and
completed normally. This shows that bypassing Anthropic translation or explicitly
requesting thinking is not a reliable fix. It does not identify the selected
upstream provider or prove what its model server received.

API request IDs retained for a future authorized investigation (no gateway logs
or configuration were accessed for these tests):

- Anthropic adaptive, failed recall: `d419d6b244519572c8481520`.
- OpenAI enabled, successful recall: `3ddbd2035605b4fb3d3a3805`.
- OpenAI enabled, failed recall: `56d2d878c25d78bdbaf52ffb`.

A separate local check exercised the gateway source snapshot's normalizer,
translator and adapter through an actual loopback HTTP receiver. All twelve
combinations of DeepSeek/SGLang/Ollama profile, streaming mode and inline-system
shape preserved the synthetic conversation. It did not exercise deployed dynamic
routing, the full router or the upstream model server's chat template.

The gateway snapshot also strips Anthropic `thinking`, and drops effort values
when the model config does not declare their domain. These are compatibility
leads, not established causes of the recall failure. No gateway workaround or
model-route change has been shipped as part of this Cloud Agent branch.

Further synthetic tests used OpenAI chat completions with temperature zero and
unchanged messages. A neutral coding-assistant system description recalled `hi`
in 2/2 samples; adding a file-memory sentence produced false missing-history
claims in 2/2. Explicit high effort produced reasoning output, but only 1/2
replies recalled `hi`. A further three samples with maximum effort recalled
`hi` in only 1/3 replies; all three completed normally with reasoning output.
Thus enabling reasoning is not an established fix.
OpenCode also produced a false missing-history answer with complete native
history, so this behavior cannot be attributed solely to Claude's persistence
or memory feature. These model-behavior failures remain distinct from the
reproduced native failure/LOST and message-ownership bugs fixed above.

The local Docker stack also passed a real-Flash task-history sequence for all
five bundled CLIs: initial function-name agreement, two live follow-ups, idle
stop and native restore, a terminal API rejection, a successful follow-up and
a native fork. The first query also received a locally injected transient API
error. Assertions checked every prior successful user/assistant message in the
outbound model request, the agreed function name in the visible reply, and the
same retained sandbox. The terminal rejection was marked failed for every CLI;
the following query and fork retained history. This validates the repaired
session lifecycle, but does not override the separate failing greeting-recall
samples above or count as verification of a remote deployment.

Fresh greeting checks on the final sandbox image again reproduced false
missing-history claims in OpenCode and Kilo while their outbound requests had
the complete user/assistant history in the correct roles. Their original user
text was no longer copied into the displayed assistant reply. Claude recalled
`hi` in both live follow-ups and the idle-restore query in this sample; earlier
Codex and Pi greeting checks also recalled it. These finite successful samples
do not eliminate the failing cases or establish an upstream root cause.

## Local verification update (2026-09-08)

- The original retry behavior was restored only in a test plugin by disabling
  process retirement. Pinned Claude 2.1.220 then failed the request-history
  assertion after native restore: the initial `hi` was absent. With the driver
  fix enabled, all four real native CLI regressions passed (367.95 seconds).
  The protocol fixture made no remote inference requests.
- New broker/driver identity tests failed before the changes (10 failures), as
  did all ten shared runner cases for five runtime IDs and two settled outcomes.
  The combined focused suites now pass 162 tests.
- Local Docker smoke and an independent request audit verified all five CLIs
  across live continuation, idle stop/restore, historical native fork and child
  stop/restore. The audit compared exact conversational text, roles, order and
  occurrence counts in 30 requests, including the correct shorter fork prefix.
  CLI-generated environment annotations were excluded from user text, and two
  OpenCode/Kilo title requests were identified separately.
- That audit first caught a defect in the local Responses mock: it omitted
  `response.output_item.added/done`, so Codex completed jobs without retaining
  assistant messages. The mock now emits the item/content-part lifecycle, and
  the smoke requires a nonempty assistant reply belonging to each current job.
  Codex was rerun after the correction; all six requests had the expected
  history. The earlier Codex lifecycle pass is not history evidence. This is a
  local test-fixture fix, not a claim about the deployed inference API.
- A fresh local Claude chat used the real `deepseek-v4-flash` API for four turns,
  with one locally injected retryable failure and an idle restore. Every request
  retained all earlier user/assistant text in order, including both occurrences
  of a repeated question. The same container and native session were retained.
  Flash recognized the initial greeting but also speculated about lost session
  state; the last answer accurately quoted `hi`. Since `hi` assigns no task,
  saying that no task was assigned is not itself evidence of memory loss. The
  unsupported session diagnosis remains a response-quality issue and cannot be
  treated as proof that Cloud Agent discarded context.
- `make check` passed formatting, lint and 1,833 tests (194 external/database
  tests deselected). The four native regressions above ran separately. Local
  broker and both runners matched all four changed production source hashes.
  Temporary gateway overlays were removed after testing. No remote deployment,
  configuration, logs or web UI were accessed in this verification.

These changes repair reproduced local persistence and identity defects. They
do not establish that every missing-history statement has the same cause or
that changing reasoning effort can eliminate it. The model-behavior preflight
remains a separate acceptance check; production prompts and CLI pins are
unchanged by these identity fixes.
