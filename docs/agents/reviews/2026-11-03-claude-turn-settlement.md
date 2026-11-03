# Claude resident turn that never settled after its final result

Reviewed against `dev` at `9ea0773`, 2026-11-03. A Claude Code 2.1.300
resident turn wrote a successful final `result`, and the UI showed Agent
Finished, but its job and attempt stayed `running` and the runner kept
renewing the lease. Only the resident Claude process was alive, as expected
between turns. This record covers the root cause, the fix and the tests. No
deployed acceptance run was authorized, so none was done.

## Root cause

`ClaudeCodeLiveTurn._settles` holds a turn's result until Claude has answered
every background task that finished. That way the notification turn Claude
opens after its result belongs to this turn, not to the next one. It added
each `system`/`task_notification` to an unanswered set and cleared the set
only on a `system`/`init`, which opens a notification turn.

Claude answers a notification in one of two ways. If the notification arrives
while the turn is still between tool calls, Claude reads it into the same
turn's next model request, and that turn's result answers it. Otherwise
Claude opens a turn of its own after the result. The first way produces no
`init`. On the host, `npm test` was backgrounded by its tool timeout and
finished while Claude was still working. Claude read its notification in the
same turn and then gave the closing answer, so the set was never cleared.
The driver kept waiting for a turn Claude would never open. The sandbox
manager never wrote its `result` frame, and the runner relayed a turn that
looked as if it were still running.

## What the CLI writes

Read from the Claude Code bundles, not from a live service:

| Version | Notification read inside a turn, with `--replay-user-messages` |
|---|---|
| 2.1.220 (the pin) | A `user` record with `isReplay: true` and `content` set to the queued notification prompt, `<task-notification>` then `<task-id>ID</task-id>`. It has an `origin` only if the queued command set one. |
| 2.1.300 (deployed) and 2.1.317 | The same record, plus `origin: {"kind": "task-notification"}`, which is derived from the command mode |

So the read is visible on the wire. It is also the event that decides
whether a notification turn is still owed.

## Fix

`cloud_agent_host/claude_background.py` now holds the turn's background
bookkeeping, which used to sit inline in `_settles`:

- A replayed notification, recognised by its `origin` or by the tagged
  `<task-id>`, marks its task as read. That task no longer holds the result.
- A read that is echoed before its `task_notification` still counts.
- A notification marked by `origin` that names no task the driver can match
  releases every pending wait. The turn must not wait forever for a turn
  Claude will not open.
- A notification replay is never taken as the turn's prompt, or as an owner's
  mid-turn message. Notifications contain words such as `status` and
  `completed`. A prompt of `status` used to end the next turn with the
  notification's answer and leave the prompt's answer to the turn after it.

These behaviours are unchanged: a background agent that is still running
holds the result, a notification left unread at the result waits for its own
turn, and cancel, retirement and follow-up turns on the same process work as
before. `drivers.py` shrank from 2,891 to 2,882 lines, and its size-budget
entry is lowered to match.

## Tests

`tests/unit/test_claude_turn_settlement.py` drives the real driver over a
resident process whose stdout stays open and silent after its last line. A
turn that waits for something that never comes therefore hangs, as it did on
the host. The tests fail after a bounded wait instead of passing at the end
of a scripted feed. The tests cover:

- a backgrounded shell whose notification is read inside the turn, with and
  without `origin`, and in both orders, followed by a follow-up turn on the
  same process;
- the same for a background agent, and an agent that is still running and
  holds the result;
- one task read and one left unread, plus cancelling while waiting;
- notification text not taken for the prompt or for an owner's message;
- a notification that names no task;
- the whole chain: the real driver, `ResidentTurn`, the sandbox manager's
  `/shared-sandboxes/{id}/turn` endpoint, `SessionSupervisorClient`, the
  runner's `_stream_shared_agent_turn`, and `_run_shared_live_attempt`
  settlement, ending in `finish_attempt("succeeded")`.

On the original driver, the 9 cases the fix targets failed: each hung until
its bounded wait, or ended with the wrong turn's answer or a wrongly taken
message. The 4 cases that check preserved behaviour passed. With the fix, all
19 cases pass. The other 6 of the 19 test the parser in the new module on its
own.

## Limits

Each CLI version's wire shape was read from its released bundle. No real
Claude process was run against a model. Whether the hung attempts on the host
end once this ships needs a deployed check, which was out of scope.
