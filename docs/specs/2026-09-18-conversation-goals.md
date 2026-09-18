# Conversation goals

A goal is one completion condition on a conversation. The platform judges it at
the end of every turn and queues the next turn itself while the objective is
unmet, up to `max_rounds`. A goal is set by its owner, on the conversation, and
never by the agent working toward it.

## What a round is

Every automatic round is an ordinary follow-up turn: one `agent_jobs` row, one
sandbox turn, one `agent/<thread-id>` update with its draft PR. It is
cancellable, metered, navigable and published exactly like a message a person
typed — which is the point. The platform does not run a second loop inside a
turn, and the turn view, the usage ledger, and the publication path need to know
nothing about goals.

Two asymmetries carry the whole design:

- **A completion claim finishes nothing.** It buys one *verification* round,
  which re-runs the check the objective names and reports again. Only a
  verification round reporting `complete` completes the goal. The alternative —
  asking a second model whether the first one is done — reads a narrative
  instead of running a test, and needs a credential path of its own.
- **A `blocked` report blocks nothing until it has been reported three
  consecutive turns.** One turn saying "I cannot" is a turn, not a verdict.

## State

The goal is conversation-level and single-valued: "no goal" is
`agent_threads.goal_objective IS NULL`.

| State | Continues itself | Entered by |
| --- | --- | --- |
| `active` | yes | owner set / resume; fork of a paused conversation |
| `paused` | no | owner pause; and the platform, with a reason: a failed or cancelled turn, a fork, a lost context, an archive, or a stall |
| `blocked` | no | the platform: `round-limit`, or `model-reported` after three consecutive turns |
| `complete` | no | a verification round reporting `complete` |

`paused` carries `goal_pause_reason`; `blocked` carries `goal_blocked_code` and
`goal_blocked_message`. Control-plane restarts change no state: continuation is
driven by the `worker_finish` event, not by an in-memory driver.

A person's own queued turn makes the goal yield. Nothing is queued behind a
message they are waiting on; the goal is judged again when that message's turn
ends. A completion claim made in that turn waits for the same moment.

## Reaching the model

The objective travels the existing `task_prompt` channel, exactly as selected
skills do, so all five harnesses receive it and no host protocol, network
destination, credential or shared-workspace file is added.

- Every turn of a conversation with an `active` goal is prefixed with a
  `<conversation_goal>` block naming the objective, the round budget and the
  two-line report convention. The turn that *set* the goal is included: it is
  the first turn that should be working toward it.
- An automatic round's body is generated server-side (`<goal_round>`), and says
  that the current workspace, tool results and files are authoritative. Rounds
  can be hours apart and the container may have restarted between them, so a
  model that trusts its own earlier narration over what it can see now is a
  model that reports success against a tree that no longer exists.

A turn reports in one of two ways, and both land in the same two columns on the
turn's row:

- **The `report_goal` tool**, preferred whenever the harness can reach the
  relay. A tool call is a structured act recorded the moment it happens; it
  cannot be restated mid-answer, and it survives a turn whose prose never
  arrives. `get_goal` reads the objective back.
- **Two lines at the end of the final assistant message** — `GOAL_STATUS:
  continue|complete|blocked` and `GOAL_REASON: <one line>` — parsed in the same
  transaction as the terminal transition. This is the fallback for a turn that
  called nothing, and it is what a harness with no MCP support is told about.

A tool report wins over the parsed text when a turn somehow produced both: the
call is the deliberate one, and it is the one the platform acknowledged at the
time. Whichever arrives, the turn is never terminal without the answer it ended
on.

The blocked response budget is bounded, not a person's: a turn that cannot be
continued is *paused*, never retried. There is no per-task dollar budget; spend
stays subject to the gateway's per-user quota, and `max_rounds` bounds how long
the platform keeps spending it without being asked again.

## Round accounting

A round is charged when it is **claimed**, not when it is queued, and only its
first attempt pays:

- A round that was queued and then cancelled never reaches a claim, so it never
  spends the budget.
- A retry of a round that already started does not spend it twice.
- A round carries the `goal_revision` it was created for, and a claim only
  charges a goal still at that revision — a round left over from a replaced
  objective cannot spend the new one's budget.
- Verification rounds are never charged: they check one completion claim, they
  do not make progress.

The limit is checked *after* the verification branch, so a completion claim made
on the last permitted round is still verified.

## Continuation

`worker_finish` calls the continuation synchronously, after the terminal
transition commits and before the runner is acknowledged. The owner's page
watches the newest turn; if the next round did not exist by the time the finish
returned, the conversation would look finished for a moment and the page would
stop polling — so the round the user is waiting to see would be the one round
nobody fetched.

The decision itself is a pure function over the turn's facts
(`cloud_agent/goals/decision.py`), and every rule about automatic continuation
lives there. The orchestration around it repeats the follow-up route's own
checks — archived conversation, lost sandbox context, repository entitlement —
and *pauses* rather than retrying when one of them fails, so the owner is told
which of them happened instead of watching a goal that silently stopped.

The next round's idempotency key names the parent turn, not a round number. One
finished turn makes exactly one continuation decision, so replaying a finish
finds the round it already made; a round number can repeat after a cancelled
round, and a key built from it would resolve to the cancelled row and silently
queue nothing.

## The platform's own MCP server

`get_goal` and `report_goal` are not a connection. They have no URL, no
credential, no upstream session and nothing to authorize — they read and write
this service's own row, under the attempt fence the caller's token already
carries. They are merged into the same catalogue the per-user connections are
merged into (`cloud_agent/mcp/platform.py`), under bare names that no
`<alias>__<tool>` can collide with, and they are advertised whether or not the
owner connected anything at all.

They are dispatched *before* the catalogue's resolution step, because there is
nothing to resolve: no policy to consult, no approval to wait for. A report is
fenced to the running attempt, so a superseded turn cannot relabel what its
successor is doing. Nothing about the decision moves — the tool writes the same
columns the parser writes, and `worker_finish` still decides.

`report_goal` is written to the *current attempt's* job row, which means a
`blocked` claim can be made three times inside one turn. What the platform
counts is three consecutive **turns**, as the state table says; the tool's
result says so explicitly, and the decision reads the row once at finish.

## The owner's view

The goal is set from the composer's attach menu — the plus button that already
holds "what this message carries", as opposed to the control row's "how it
runs". It joins that menu as its own group, after the upload, Skills and MCP
entries, and shows the objective it would set (or "Keep going until…" when it
would set none). Its dialog takes an objective, a maximum rounds and a clear.
It only changes local state and rides the message the user is about to send,
because a goal with no message attached has nothing to work on.

The menu's groups are optional props: the job view's follow-up composer offers
no uploads and no MCP submenu, and it shows the groups it has rather than
growing a second, differently-shaped menu beside the one the product already
has.

A conversation that already has one gets a status band above its message box,
in one of five shapes — in progress (including "verifying"), waiting between
rounds, paused with the reason it stopped, blocked with which kind of block, or
complete. Each offers exactly the actions that state admits. A paused goal
left behind by a fork says so in those words: a fork never continues itself.

An automatic round is not drawn as a user bubble. Nobody wrote it, so it
renders as a system marker — `Round 3 · automatic` or `Verification round` —
with the round's generated body collapsed behind it and the status that turn
reported.

## Owner API

| Route | Action |
| --- | --- |
| `PUT /v1/agent/jobs/{job_id}/goal` | Set, replace or edit; optional `revision` for compare-and-set (409 on mismatch) |
| `POST /v1/agent/jobs/{job_id}/goal/pause` | `active → paused(user)`, cancelling the queued automatic round |
| `POST /v1/agent/jobs/{job_id}/goal/resume` | `paused → active`, and queue a work round when nothing is pending |
| `DELETE /v1/agent/jobs/{job_id}/goal` | Remove the goal and its queued round |

`POST /v1/agent/jobs` and `POST /v1/agent/jobs/{job_id}/follow-ups` accept a
`goal` alongside the message, and `/config` advertises `goal_limits`. The
automatic continuation path never accepts one: an agent cannot edit the goal it
is running for. Clearing a goal keeps `goal_revision` counting, so a round
created against the removed goal can never be confused with one created against
whatever comes next. Every turn keeps its own snapshot of the objective, the
round it was and what it reported; editing the goal never rewrites history.

Editing an objective restarts the round budget; changing only the round cap does
not. A paused round the owner resumes continues from the last turn that actually
ran, not from the cancelled round that was waiting.

## Rollout

1. Apply additive Alembic revision `0016_conversation_goal` before starting the
   new control plane. Every existing row has no goal, which is what the shape
   constraint asserts; nothing is backfilled.
2. Upgrade the control plane. No host rebuild is needed: the goal reaches the
   agent through the existing prompt channel.
3. No new secret, environment variable, or egress destination.

An application rollback may leave the additive schema in place. Drain active
goals — pause or clear them — before rolling back to a control plane that cannot
continue them, because a round queued by that control plane simply runs as an
ordinary turn. Database downgrade removes every goal and every turn's snapshot
and must run with the API stopped.

## Verification

Coverage includes the decision rules one at a time, the report parser's
tolerance and its limits, prompt snapshots for both reporting routes, the
platform tools on the merged MCP surface (always advertised, rejected statuses,
the attempt fence), goal storage under real Postgres (revisions,
compare-and-set, claim-time accounting, tool-versus-text precedence, forks,
archives, idempotent round creation), the route loop end to end (continue,
verify, complete, blocked, round limit, pause/resume/clear, replay), and the
browser pieces (the band's five shapes, the menu's keyboard behaviour, the goal
mapping, dictionary alignment).

The one acceptance check that needs a real deployment is the end-to-end run:
a goal whose objective is "until `pytest` is green" reaching green on a
repository whose first two turns fail, with each round visible and cancellable
in the conversation and the draft PR updated after each.

## Not in this version

No idle check-in or background wait. No goal shared across conversations
(skills' territory). No scheduled trigger. No goal on a SideChat — the plus menu
is built to be reused there, and that is a separate change. No agent-side goal
editing: the tool surface is exactly `get_goal` and `report_goal`, and setting,
editing, pausing, resuming or clearing a goal is an owner route that no
automatic round calls. No per-task budget field.

Nothing about uploads or MCP connections changes here. The attach menu already
had those groups, and a goal is a third one beside them; a message can carry all
three at once, and each is written by its own column.
