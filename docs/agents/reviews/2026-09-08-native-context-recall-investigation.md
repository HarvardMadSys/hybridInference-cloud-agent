# Native conversation recall: investigation record (2026-09-07 and 2026-09-08)

Moved on 2026-10-16 from [native conversation recall](../../developer/context-recall.md),
which keeps the current behavior and the acceptance checks. These are the
dated synthetic reproductions and local verification runs behind that work.
They describe the revisions and environments named here, not a current
deployment.

## Investigation evidence (2026-09-07)

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
