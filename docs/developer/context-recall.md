# Native conversation recall

Cloud Agent keeps each chat's native CLI session in its retained sandbox. The
CLI owns the conversation history, and Agent passes each user message through
unchanged.

Two questions need separate answers: did the same native session survive, and
did the model answer correctly from its history? The September investigation
found failures of both kinds. It reproduced native persistence bugs, but it also
found models claiming that history was missing even when the request was
complete.

For session architecture, see the
[native Fork/shared-workspace design](../agents/specs/2026-08-31-native-session-fork-shared-workspace-design.md).
The dated findings below are local acceptance evidence. They do not describe
the current state of a remote deployment.

## Context status

Each turn reports whether the chat's native context survived:

| Context status | Meaning |
|---|---|
| `NEW` | No native turn has previously settled for this member |
| `LIVE` | The turn uses the compatible resident CLI process |
| `RESTORED` | A replacement process resumes the retained native session |
| `LOST` | Required native state or container integrity cannot be established; execution is refused |

Stopping a container preserves its writable layer. Losing the container or the
Docker data root also loses the required native state. Transcript replay and
cross-host restoration are not recovery paths.

## Native session identity at the shared boundary

After each turn settles, the runner verifies the native session ID before saving
it. This check also covers API failures that settled normally. If a continuation
reports a different ID, the turn fails before it can replace the durable
locator.

If launch omits the locator, the sandbox manager uses the retained member's saved value.
It rejects a conflict between the requested and observed locators, then checks
the identity again when the turn settles. The runner also rejects any retained
chat that has no resume locator.

Claude root stream events are checked before normalization, so child-agent
events with `parent_tool_use_id` cannot replace the root session ID. The other
bundled adapters validate their own native resume handshakes.

History comes directly from the native session. Cloud Agent does not replay
transcripts, rewrite JSONL, or add a continuation reminder. An earlier
continuation-prompt experiment was withdrawn, so its successful samples do not
validate the current unchanged-input implementation. Fault-injection tests
found a validation gap in the older shared runner; they did not show that a
staging request had actually supplied the wrong locator.

## Other native CLIs

All five bundled CLIs can keep a failed query for a follow-up, but only when the
native turn verifiably ended, activation cleanup completed, and both the
transport and native locator remain usable.

| CLI | Native settlement evidence |
| --- | --- |
| Claude Code | `result`, including its `is_error` value |
| Codex | `turn/completed` for the active native turn ID |
| Pi | `agent_end`; the last assistant's `stopReason` determines failure |
| OpenCode / Kilo | Bridge `settled` for the active turn at native idle |

A command rejection, dead transport, timeout, or cancellation does not count as
native settlement. Pi reports terminal API errors as failures, not successful
empty turns. OpenCode/Kilo use message-role metadata to collect only
assistant-owned text. They resolve late metadata at settlement and exclude
events from earlier bridge turns. This keeps user text out of assistant output
without changing the native model history.

The pinned Claude 2.1.220 CLI has a reproduced retry-parent-chain defect: a
delayed `system/api_error` can bypass a successfully retried assistant reply on
a later resume. When the driver sees `system/api_retry`, it settles the current
turn, saves the native locator, and gracefully retires the process. The next
turn resumes that same session in the same container. A successful turn without
a retry keeps the live process. This handling does not repair transcripts that
the older behavior already damaged.

The sandbox's `http-agent-live-bridge=4` helper pin is required by host
attestation. Version 4 gates every turn on the current live SSE subscription,
and aborts an active native turn if that non-replayable stream is lost. It also
extends autonomous permission replies for both OpenCode and Kilo to the Task
child sessions the root turn explicitly reports, retries a failed one-shot
permission reply, and aborts instead of leaving that request pending. Version 3
added correlated native question replies. Deploy the sandbox_manager/runner and sandbox
image together;
an older helper image must not advertise readiness for the new host.

## Release scope and remaining findings

The implementation addresses the reproduced retry/flush history loss and keeps
verifiably settled failures across all five CLIs. It also covers Pi error
reporting, OpenCode/Kilo assistant-message ownership, and runner/sandbox_manager session
identity. The same work corrected the local Responses fixtures and smoke checks.

The documented investigation also left two separate findings:

| Finding | Evidence and limit |
| --- | --- |
| Flash can claim missing history with complete outbound messages | Reproduced in native and direct synthetic API calls. Reasoning effort and newer CLIs did not reliably fix it; client evidence does not identify the upstream engine/template/model cause. |
| Gateway snapshot loses effort configuration and historical thinking blocks in translation | Reproduced locally. Separate patches outside this repository were not deployment-verified by this investigation; they do not establish that Flash recall is fixed. |

A successful HTTP response does not prove factual recall, and neither does a
persisted native session. These fixes do not migrate already damaged transcripts
or old disposable chats. Start a new chat when legacy history has no native
shared-live membership.

## Verification before accepting a deployment

1. Require **CI Gate**, including the pinned native CLI request-history
   regressions. They compare exact user/assistant text, roles, order and repeated
   occurrences through live follow-ups, immediate close/restore, failed-call
   retries, and successful internal retries followed by native restores. Sandbox Manager
   and runner checks must also reject changed locators for successful and settled
   failed turns. The loopback fixture needs no inference credentials or charges.
2. Verify the exact host/API SHA, reverse sandbox manager probe and fresh heartbeat using
   [deployment reliability](deployment-reliability.md). A working model call does
   not prove the intended host revision is serving jobs.
3. Run the opt-in behavior preflight from the target checkout against the target
   inference Gateway:

   ```bash
   CLOUD_AGENT_TEST_API_KEY_FILE=/path/to/test-key \
     uv run python -m deploy.preflight.check_model_context \
     https://gateway.example.com --repetitions 3
   ```

   `CLOUD_AGENT_TEST_PROXY` optionally names an HTTP proxy. The key remains in
   the trusted test process, outside sandbox credentials and command arguments.
   The script sends synthetic messages to `deepseek-v4-flash` and prints answers
   and request IDs without authentication headers. Greeting, repeated-question,
   task, fact/update and no-history cases produce five calls per repetition;
   the default three repetitions make 15 calls with ordinary inference usage.

   A non-200 response, stream error, truncation, missing required facts or a
   recognized missing-history claim fails the check. The script requests native
   `adaptive/high` settings and also requires observed thinking. Results report
   `recall_passed` and `thinking_observed` separately, retaining the answer even
   when thinking is absent. Observed thinking does not prove the engine received
   the exact effort, and missing thinking does not prove history loss. Inspect
   conservative detector failures: it may reject a reply quoting a bad claim in
   order to correct it. HTTP 200 alone is insufficient.
4. Use the deployed app to check a new chat, live follow-up and idle restore.
   Confirm the same retained sandbox and native locator. If an answer reports
   lost context, compare the outbound request history and settlement evidence
   before attributing it to persistence.

The preflight is opt-in and is not automatically run by deployment workflows.
CI receives no inference key. Repeat it when Gateway model routes, model
deployment or CLI pins change. Finite samples test the reported failure without
guaranteeing that a model never makes another incorrect claim.

Implementation and checks:
[`runner.py`](../../apps/backend/cloud_agent_host/runner.py),
[`drivers.py`](../../apps/backend/cloud_agent_host/drivers.py),
[`shared_session_supervisor.py`](../../apps/backend/cloud_agent_host/shared_session_supervisor.py),
[`shared_protocol.py`](../../apps/backend/cloud_agent_shared/shared_protocol.py),
[`check_model_context.py`](../../deploy/preflight/check_model_context.py) and
[`test_claude_driver_context.py`](../../tests/external/test_claude_driver_context.py).

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
- New sandbox_manager/driver identity tests failed before the changes (10 failures), as
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
  sandbox manager and both runners matched all four changed production source hashes.
  Temporary gateway overlays were removed after testing. No remote deployment,
  configuration, logs or web UI were accessed in this verification.

These changes repair reproduced local persistence and identity defects. They
do not establish that every missing-history statement has the same cause or
that changing reasoning effort can eliminate it. The model-behavior preflight
remains a separate acceptance check; production prompts and CLI pins are
unchanged by these identity fixes.
