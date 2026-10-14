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

## Always allow

The conversation's **Always allow** action records the current approval and its
future permission intent in one server-side transaction. You can navigate away
or refresh after submitting it. The server saves the permission once no calls
on that conversation's connection are waiting for approval or dispatch.

The saved rule covers only the named capability in this conversation. Other
rules and default confirmation behavior remain unchanged. Before saving, the
server checks the owner, connection and policy revisions, and the capability
fingerprint again. Reconnecting, disabling the connection, changing permissions
or changing the capability definition discards a stale intent instead of
restoring a revoked permission. The call history shows whether the save is
pending, applied, or discarded.

## Waiting and expiry

A call requiring confirmation waits for an owner decision for up to ten
minutes. Claude Code's per-server HTTP request timeout and remote idle timeout
are explicitly set to the same 26-minute window as its overall tool timeout.
Raising only the overall timer leaves separate network timers able to abandon
the request while Approvals still shows it as pending.
Clients accepting SSE receive an immediate response and periodic keepalive
comments during the wait, followed by the original call's single JSON-RPC result
or error. Disconnecting cancels the wait and expires its held approval; no
background dispatcher keeps running on behalf of an abandoned request.

An approval expiry means the request was not dispatched to the upstream server.
Review the permission or approve the next request before retrying. Repeated
unapproved retries do not diagnose an unavailable GitHub server. A changed
session authorization also stops the held call before dispatch. Errors after
dispatch can have an unknown outcome; check the external state before retrying
a write such as creating a pull request.

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
