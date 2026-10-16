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
The synthetic reproductions and local verification runs behind this page are
kept as a
[dated investigation record](../agents/reviews/2026-09-08-native-context-recall-investigation.md);
they do not describe the current state of a remote deployment.

## Context status

Each turn reports whether the chat's native context survived:

| Context status | Meaning |
|---|---|
| `NEW` | No native turn has previously settled for this member |
| `LIVE` | The turn uses the compatible resident CLI process |
| `RESTORED` | A replacement process resumes the retained native session |
| `LOST` | Required native state or container integrity cannot be established; execution is refused |

Stopping a container preserves its writable layer. Losing the container or the
Docker data root also loses the required native state, unless the group was
stopped cleanly with a current
[restorable checkpoint](operations.md#restoring-a-removed-hosts-sandboxes),
which another host can restore once this one is removed, terminates or moves
out before its lease ends. Transcript replay is
never a recovery path.

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
added correlated native question replies. Deploy the runner, Sandbox Manager
and sandbox image together;
an older helper image must not advertise readiness for the new host.

## Release scope and remaining findings

The implementation addresses the reproduced retry/flush history loss and keeps
verifiably settled failures across all five CLIs. It also covers Pi error
reporting, OpenCode/Kilo assistant-message ownership, and runner/Sandbox
Manager session identity. The same work corrected the local Responses fixtures and smoke checks.

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
