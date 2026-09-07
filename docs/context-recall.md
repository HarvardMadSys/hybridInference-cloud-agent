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

Examples to correlate in the staging gateway logs:

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
