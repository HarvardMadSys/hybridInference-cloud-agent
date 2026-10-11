# Tool approvals

Open **Approvals** in the application header to review pending MCP calls across
your conversations, including conversations hidden by a collapsed task list.
The badge counts loaded pending calls. A trailing **+** means more calls are
available; choose **Load more** to see them.

Each card shows its conversation, connection, capability, arguments, and
deadline. Server descriptions and hints are information supplied by that server.
Choose **Open conversation** for the surrounding task context before deciding.

**Approve** releases that one held request under its current authorization and
attempt fence. **Deny** refuses that request. The inbox does not change a
conversation's permission rules. To manage those rules, use the conversation's
Tools panel; its **Always allow** action retains the existing conversation scope.

The queue refreshes when the owner's change stream reports a relevant change,
with polling as a fallback. **Refresh** reads it again immediately. Expired calls
leave the pending queue even if the worker has not yet recorded expiry. A
decision that loses a race with expiry or another tab cannot revive the call.

The Tools panel also shows request history. Choose **Load older requests** to
continue beyond its first page. Loaded history stays available through refreshes
and displays overdue requests as expired.

## Current Always allow limitation

The Tools panel saves **Always allow** after the approved call advances and no
pending or approved call remains on that connection. This avoids invalidating a
held request by changing its policy while it waits. The deferred save currently
lives in the panel: leaving the conversation before it runs can lose the rule.
Keep the conversation open until the panel confirms that the capability can run
without asking, or save its permission explicitly in Tools afterward. The
one-time approval remains recorded even if the deferred rule is lost.

## API pagination

`GET /v1/agent/mcp/invocations?pending_only=true` reads the authenticated owner's
pending inbox. Add `job_id` or `thread_id` to narrow it. History reads require one
of those scopes; omitting both does not expose account-wide history.

`limit` defaults to 50 and accepts 1 through 100. Responses include
`invocations`, `has_more`, and `next_before_id`. When `has_more` is true, pass
`next_before_id` as `before_id` for the next page. IDs run newest first, and the
cursor is exclusive, so newly inserted calls do not shift older pages.

Invocation reads and decisions are scoped to the authenticated owner and omit
deleted conversations. Internal audit history is retained after deletion.
Reading the queue does not mutate stored invocation state or grant permission.
