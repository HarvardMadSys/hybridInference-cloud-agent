# Conversation goals

A goal is one completion condition on a conversation. The platform judges it at
the end of every turn and queues the next turn itself while the objective is
unmet. A goal is set by its owner, on the conversation, and never by the agent
working toward it.

**There is no round limit by default.** The owner is asked one thing — what
"done" means — and the platform then keeps going until the objective is met, the
owner stops it, it needs the owner, or one of the run-time limits that already
governs every turn applies (the account's inference quota, a single execution's
deadline, cancellation, repository access). No default budget, no ceiling, and
no large sentinel integer standing in for "unlimited": the absence of a bound is
the absence of a value, and `rounds_used` is a statistic the owner reads rather
than a stop condition. A bound exists only when an owner states one, it is
honored exactly when they do, and resuming a goal that has spent its own bound
lifts it.

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
| `active` | yes | owner set / resume — and nothing else |
| `paused` | no | owner pause; and the platform, with a reason: a failed or cancelled turn, a fork, a lost context, an archive, a stall, or a bound the owner stated and the goal reached |
| `complete` | no | a round whose readable report says the objective is met |

`paused` carries `goal_pause_reason` (the machine-readable cause) and
`goal_pause_detail` (the sentence behind it, always set while paused). Control-
plane restarts change no state: continuation is driven by the `worker_finish`
event and a durable wakeup row, not by an in-memory driver.

`round_limit` is still in the vocabulary, because rows written when the platform
imposed a default carry it and a bound an owner states can still reach one. It
is not a stop the platform reaches on its own any more.

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

A turn that cannot be continued is *paused*, never retried. There is no
per-task dollar budget, and no round budget either: spend stays subject to the
gateway's per-user quota, and a turn stays subject to the execution deadline and
to cancellation. Those are the limits the platform already had, and a goal
neither weakens them nor adds a second one of its own.

## Round accounting

What a goal has spent is **counted, never stored**: the number of work rounds
carrying the goal's current revision that a model actually ran for. Deriving it
makes every rule below a property of the data rather than a write that has to be
remembered:

- A round queued and then cancelled never started, so it costs nothing.
- A round that started costs once, however many times it is retried.
- The revision names the *objective*. A round carries the revision it was
  created for, so a round left over from a replaced objective cannot be counted
  against the new one — and replacing the objective starts the count over while
  editing only the bound does not.
- Repair rounds count too: they are model rounds, and the count describes spend
  rather than progress.

A stored counter was tried first and is the wrong shape twice over. It has to be
incremented by whoever claims a round, which means the claim path writing the
conversation's row while already holding the round's lock — the reverse of this
repository's thread-before-job order, and a deadlock against pausing or
messaging at the moment a runner takes a round. And it has to be fenced against
the revision it was granted for, where every fence that was slightly wrong
silently gave away a free round.

The count is never a reason to stop on its own. The bound — when, and only
when, an owner stated one — is checked *after* the report, so a completion
reported on the last permitted round still completes.

## Continuation

`worker_finish` calls the continuation synchronously, after the terminal
transition commits and before the runner is acknowledged. The owner's page
watches the newest turn; if the next round did not exist by the time the finish
returned, the conversation would look finished for a moment and the page would
stop polling — so the round the user is waiting to see would be the one round
nobody fetched.

The decision itself is a pure function over the turn's facts
(`apps/backend/cloud_agent/goals/decision.py`), and every rule about automatic continuation
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

**The message box is the whole interaction, and there is no dialog anywhere in
the feature.** Setting a goal means typing `/goal <objective>` and pressing
Enter. Nothing else has to be opened, filled in, confirmed or dismissed, and
there is no second place where the same one thing — an objective — is edited.

The box offers the command as soon as a slash is typed, filters it as the name
is typed, and takes Enter, Escape and the arrow keys; an IME's Enter commits
its candidate and is never a submit. Only a leading `/goal` is a command —
`/goals`, a mention inside a sentence, and a line inside a code block are all
ordinary text. **Choosing the Goal row writes `/goal ` into the box and puts the
caret after the space**: the objective is then typed where it would have been
typed anyway. A bare `/goal` with nothing after it is not an objective — it
creates no goal and is never sent to the model as prose; the composer says what
is missing and leaves the token where the owner can finish the sentence.

`/goal <objective>` acts on submit instead of sending the command to the model.
On a conversation that exists, that is the thread's own `PUT`, and the
platform's next round is what starts the work — nothing has to be sent saying
"begin". On a task that does not exist yet, the objective is the first turn's
content *and* the goal, in the one create request, so a command can never queue
two tasks and a failed command can never leave one behind.

The composer's plus menu keeps a Goal row, and it is the same interaction: it
writes the command into the box and hands the caret back. It joins that menu as
its own group, after the uploads, Skills and MCP entries, and it has no
chevron, because it opens nothing.

A conversation that already has a goal gets a compact header sharing its
message box's outer surface. The objective is the primary text; a short status
label and the rounds run sit beneath it. Paused goals retain their reason and
detail, and completed goals name the completion basis. The header is absent,
not empty, when there is no goal. Archived conversations keep a standalone
goal band above their restore action.

Pause or Resume stays visible. The **Goal options** menu holds Edit and Clear
(only Clear after completion), with keyboard navigation, outside-click
dismissal and Escape returning focus to its trigger without stopping the run.
Every control has an accessible name and a tooltip carrying the operation's
own semantics: Pause stops what comes next and leaves the round
already running alone (interrupting that round is the existing Stop button),
Edit changes the objective without interrupting the round in flight, Clear
removes the goal and its queued rounds and never the chat, and Resume is the
owner starting it again. **Edit opens in place**, on the row the objective is
already on, with Save and Cancel beside the words: a refused write leaves the
draft exactly where the retry can reach it, and the re-read a compare-and-set
refusal forces never types the other tab's goal over it. The objective
ellipsises to keep the band compact and opens, whole, on request.

The band reports rounds as a count of what actually ran — "3 rounds run" — and
names a total only when the owner stated one ("3 of at most 10 rounds"). The
`3/10` it used to show reported a limit nobody had set and made the absence of
a limit impossible to say.

A paused goal whose own stated bound has been spent offers Resume, which lifts
that bound: it is the only way out, because the round bound has no configuration
surface, and a control that can only fail is not a control. A bound that has
*not* been reached is left exactly as it was.

The band reads the goal from the thread on every reload, and every write
re-reads it afterwards. A write carries the revision the client read, so a
second tab's edit is refused with a 409 rather than silently overwritten, and
the refusal is what triggers the re-read. A goal that is still `active` keeps
the page observing after the turn on screen has settled: the next round is
usually queued a moment later, and the owner is watching for exactly that.

An automatic round is not drawn as a user bubble. Nobody wrote it, so it
renders as a system marker — `Round 3 · automatic` or `Verification round` —
with the round's generated body collapsed behind it and the status that turn
reported.

## Owner API

| Route | Action |
| --- | --- |
| `GET /v1/agent/threads/{thread_id}/goal` | The goal, and the control revision even when there is none |
| `PUT /v1/agent/threads/{thread_id}/goal` | Set, replace or edit; optional `revision` for compare-and-set (409 on mismatch) |
| `POST /v1/agent/threads/{thread_id}/goal/pause` | `active → paused(user)`, cancelling the queued automatic rounds |
| `POST /v1/agent/threads/{thread_id}/goal/resume` | `paused → active`, recording the obligation to schedule |
| `DELETE /v1/agent/threads/{thread_id}/goal` | Remove the goal and its queued rounds |

`POST /v1/agent/jobs` accepts a `goal` alongside the first message; a later
message (`POST /v1/agent/threads/{thread_id}/messages`, which replaced the
per-job follow-up route) carries none, and a goal set mid-conversation goes
through `PUT …/goal` above. `/config` advertises `goal_limits` — which
names the objective's bound and nothing else, because a deployment that
advertised a round ceiling was telling every client that "unlimited" meant
"fifty". `goal.max_rounds` is optional and absent in every request this UI
sends; when a caller states one, the server honors it exactly.

**Absence is not `null`.** The field is tri-state, and the three states are three
different requests: omitted leaves whatever bound the goal carries exactly as it
is, a number sets one, and an explicit `null` lifts one. Anything else makes the
ordinary edit destructive — the band's editor edits only the objective, so a
request that does not mention a bound would otherwise delete a limit its owner
stated, silently, with nothing to undo it. The automatic
continuation path never accepts a goal at all: an agent cannot edit the goal it
is running for. Clearing a goal keeps `goal_revision` counting, so a round
created against the removed goal can never be confused with one created against
whatever comes next. Every turn keeps its own snapshot of the objective, the
round it was and what it reported; editing the goal never rewrites history.

Editing an objective restarts the round count; changing only the bound does not.
A paused round the owner resumes continues from the last turn that actually ran,
not from the cancelled round that was waiting. Resuming a goal whose own stated
bound has been reached clears that bound and leaves the count alone: the count
is history, and the bound was the owner's to lift.

## Rollout

1. Apply the additive Alembic revisions `0019_conversation_goal`,
   `0020_goal_controller`, `0021_continuous_measurement`, and
   `0022_goal_open_ended` before starting the new control plane. Nothing is backfilled at any step.
2. Upgrade the control plane. No host rebuild is needed: the goal reaches the
   agent through the existing prompt channel.
3. No new secret, environment variable, or egress destination.

`0022` is the revision that makes a bound optional. It changes no row: a goal
that carries `goal_max_rounds = 10` keeps it, because the old client's default
was ten but ten is also an ordinary number for a caller to have asked for, and a
migration that guessed would be deleting a constraint its owner stated. Nothing
is resumed either — a goal that was paused stays paused, whatever paused it. The
way out of a spent bound is the owner's own Resume, which lifts it and returns
the new state so the change is visible rather than implied. Where the previous
shape cannot be represented at all — a goal with no bound, during a downgrade —
the row is given the old default rather than dropped: losing an objective is far
worse than bounding it.

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
archives, idempotent round creation), the route loop end to end, and the browser
pieces (the band's shapes and controls, the command's recognition rule and its
keyboard behaviour, the two composers' submit paths, the goal mapping,
dictionary alignment).

The continuation rules are pinned at three levels, and the round bound at all
three: the pure decision (`rounds_used` of 9, 10, 11, 49, 50, 51 and 5,000 with
no bound all queue another round; a stated bound still pauses), the wakeup
consumer over a fake store, and the real Postgres path — fifty charged rounds on
real rows, a real wakeup, one `consume_wakeup` call, and exactly one round
created. That last one is bounded on purpose: it exercises the counting SQL and
the decision without ever starting an agent, so no test depends on a goal that
runs for ever. Migration coverage builds the `0020` shape, upgrades over it, and
asserts that a stated bound survives, that a paused goal is not resumed, that an
objective with no bound is accepted, that `goal_max_rounds = 0` is not a way to
spell "unlimited", and that the lossy downgrade bounds rather than discards.

The browser pieces were also looked at in a real Chrome against the real
Next.js app, with a stub control plane standing in for the one this environment
cannot run. That distinction matters and is stated wherever those runs are
cited: a stub control plane proves the *client* — what it renders, what it
sends, what it does with a refusal — and proves nothing about scheduling,
claims or publication. It is not an end-to-end run.

What was looked at: the command menu in both composers and the `/goal ` it
writes into the box with the caret kept there, a bare `/goal` asking for the
objective instead of creating anything, a new task created once from
`/goal <objective>`, an existing conversation's goal set through the thread's
own route, the band's inline editor in place with no modal over the chat, and
the band in light, dark and a phone width with a long, a paused, a bounded and
a completed objective. The end-to-end run below is the part that still needs a
real deployment and a real runner.

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
