# Native conversation recall

A retained native session and a correct answer about its history are separate
checks. PR96's local Flash reproduction kept every user/assistant message in the
request, while the model claimed there had been no previous conversation.
A small synthetic reproduction showed that file-memory instructions could
trigger this confusion. Removing auto memory did not fix the full CLI case.

Claude follow-ups now carry a separate `<cloud-agent-context>` text block that
identifies a native continuation and asks the model to ground recall in the
actual user messages. The user's original text remains its own unchanged block.
Fresh sessions do not receive a continuation marker. Live follow-ups, retries,
and the first turn after native restore or Fork do. This does not replay history,
replace the CLI system prompt, disable auto memory, or recover a lost sandbox.
Compacted or genuinely absent details are not reconstructed by this mechanism.

## Verification before accepting a deployment

1. Require the existing CI Gate. Its pinned native CLI regression checks actual
   outbound requests, not just launch flags: user/assistant history and the
   continuation marker must survive live follow-ups, immediate close/restore,
   and a failed API call followed by a retry. This uses a loopback model fixture
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
