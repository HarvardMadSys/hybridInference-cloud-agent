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

The sandbox advertises `http-agent-live-bridge=2`, required by host attestation.
Deploy the broker/runner and sandbox image together. An image without this
helper version must not advertise readiness for the new host. This is not an
automatic migration or repair of legacy retained transcripts.

## Verification before accepting a deployment

1. Require the existing CI Gate. Its pinned native CLI regression checks actual
   outbound requests, not just launch flags: user/assistant history must survive
   live follow-ups, immediate close/restore,
   a failed API call followed by a retry, and a successful internal retry on
   either the first or a later turn followed by multiple native restores.
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
   Three repetitions make nine model calls and incur ordinary inference usage.

   A missing key, non-200 response, stream error, truncation, failure to recall
   the greeting/function name, or a recognized missing-history claim produces
   a nonzero exit. Inspect failures; the conservative claim detector can also
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
