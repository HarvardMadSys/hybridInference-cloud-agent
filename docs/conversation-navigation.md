# Conversation navigation

Use the turn rail beside **Chat** to find earlier questions and replies without
leaving the conversation. Each mark represents one turn in transcript order.
The newest mark takes you to that turn's beginning; **Back to bottom** takes you
to the end and resumes following new output.

This guide describes the current implementation. For design decisions and sizing
measurements, see the [turn navigation design](specs/2026-09-11-conversation-turn-navigation-design.zh.md).

## The rail

The rail sits in the Chat column's right gutter. It appears when the conversation
has at least two visible turns, the column is at least 720px wide, the reading
area is at least 200px high, and the device reports a fine pointer. Opening the
workspace can make the column too narrow to show it. On smaller or touch-only
layouts, use normal transcript scrolling. Trajectory has no rail.

The current turn has the longest, darkest mark. Hovered or focused marks widen
slightly. These lengths indicate interaction state; they do not encode message
length, duration, tokens or progress.

Marks retain their spacing in long conversations. Scroll within the rail to
reach more turns; faded ends indicate additional marks. Scrolling the rail does
not scroll the transcript. There is no separate turn menu or list panel.

## Previews

Hover a mark or focus it with the keyboard to preview the question and its last
visible reply. Moving the pointer onto the preview keeps it open. Escape closes
it; moving away closes it after a short delay.

The question uses the message that was sent, rather than the conversation title.
A reply appears only after the turn reaches a terminal state, so a running
answer does not change beneath the pointer. Previews follow the attempt shown
in the transcript and contain only visible question/reply text. They exclude
reasoning, tool arguments, tool output and runtime records. Question text is
capped at 50 characters and reply text at 120.

Hovering changes neither the reading position nor the current turn and sends no
request. Copied Fork history uses durable messages for both transcript and
preview even when those turns have no stored event log.

## Jumping

Click a mark, or press Enter or Space on a focused mark, to place the turn's
first question about 24px below the top of the reading area. A turn without a
question lands on its first visible content.

Arrow keys move focus between marks; Home and End focus the first and last.
Moving focus does not jump until you activate the mark. A jump leaves the URL,
selected attempt and workspace state unchanged. It does not invoke Fork,
Rewind or Cancel.

A later jump, manual transcript scrolling or leaving the conversation cancels
an unfinished jump. If the target cannot be found, the transcript stays in place
and an accessible message reports that the turn is unavailable.

**Back to bottom** is the circular chevron above the composer, aligned with the
conversation content's right edge. It moves to the end of the transcript and
resumes following output, while the newest rail mark moves to the beginning of
the newest turn.

## Tasks and subagents

The floating **Tasks and subagents** window shows the selected turn and attempt's
latest recorded task list and delegated agents without narrowing the conversation.
It appears only when at least one task or subagent has meaningful recorded data;
an empty Tasks or Subagents section is omitted. Close it to read underneath, and
use the header toggle to reopen it.

Task checklists include recorded status and dependencies. Task creation results
link assigned IDs to their names and descriptions, so later status updates change
the same row. **Status not recorded** means the selected events contain no status
for that task; a successful creation does not mean the task is completed.

Subagent cards show assigned work, agent type, model, expandable prompts, and
the latest reported agent state. Under **Returned to the main agent** they show
what the main agent received back, rendered as Markdown and collapsed when
long:

- Claude Code's Agent or Task, and OpenCode's or Kilo's task, return the
  agent's answer as the call's own result. Claude records the agent's status
  beside it.
- Codex returns a finished agent's answer, or its error, as the message of the
  agent state its `wait` call reports.
- pi has no subagents.

A card says when a reply is still pending, was not recorded, or was stored
only in part. A failed or interrupted launch stays listed with what it
returned. A background launch returns only a launch notice, and its card says
so rather than presenting the notice as the answer; a successful background
launch does not imply that delegated work has finished. Chat keeps compact
tool calls with expandable recorded arguments and output. **View subagent
execution** opens the source call in the trajectory inspector when its stored
record is available, and the call's summary there shows the same reply.

Trajectory marks explicitly recorded child activity with a **Subagent** label.
Select a delegation step to browse its child steps in the inspector, or use
**Delegated by** on a child step to return to its parent. These links require
runtime ownership records; older sessions without them keep their flat step list.
Claude Code records the delegating call; Codex records the subagent's own
thread, which belongs to the spawn call that started it. A subagent's messages,
reasoning and tool calls never read as the conversation's own.

## Reading position

The transcript has two reading behaviors:

- **Following:** new output keeps the transcript at its end. A conversation opens
  this way. Following resumes after Back to bottom, scrolling to the live end,
  or acceptance of a follow-up you sent.
- **Reading:** the transcript holds the text in place as output arrives. Scrolling
  up, jumping through the rail, or selecting an older run attempt enters this
  state.

Back to bottom appears while you are away from the end. New content adds a dot
and updates the button's tooltip and accessible name. It does not automatically
pull you back to the live end. Switching from Chat to Trajectory and back keeps
the page's reading position; reloading opens at the end again.

## Workspace navigation

Open the workspace from the header or with Ctrl/Command+J. On smaller screens,
**Back to conversation** returns to Chat and preserves the selected workspace
view. The task title and controls adapt to the space left by the sidebar.

Workspace, Git, file-list, and terminal tabs use Left/Right arrows, Home, and End
to select a view while keeping keyboard focus in the tab strip. Narrow Git panes
use a changed-file selector so the diff keeps the available width. The diff and
file preview are keyboard-scrollable regions.

Opening a file in a narrow pane focuses its preview. **Back** or Escape returns
to the file list and focuses the file you opened. Changed files show their parent
paths to distinguish matching names in different directories. Terminal splits
follow the pane's width and scroll vertically when the screen is too short to
show both shells.

## Accessibility

The rail is a named navigation region with real buttons. Each mark names its
turn, the current mark has `aria-current`, and the preview is associated with
its mark as a description. One mark is in the tab order at a time. Arrow keys,
Home and End reach the others without requiring a Tab press for every turn.
Enter or Space jumps; Escape closes the preview.

A requested jump is announced once to screen readers. Streaming output does
not repeatedly announce navigation state. Turn anchors remain named, without
visible numbered dividers. English and Simplified Chinese strings, light/dark
themes and reduced-motion preferences are supported.

## What the rail does not do

The rail navigates only turns already rendered by the page. Opening a historical
turn link limits the rail to that view; it does not offer a jump to later turns
whose bodies are absent. Hovering and jumping add no endpoint, subscription or
network request. Navigation does not persist a reading position across reloads
or alter conversation data.

## Verifying it in a browser

Use a real browser and a conversation with several turns. Unit and DOM tests
cover navigation data, focus and an injected box model; browser checks are still
needed for Markdown reflow, the sticky composer, resize timing and actual pixel
positions.

1. Click a middle mark. Its question should land about 24px below the reading
   area's top, and the mark should become current.
2. Scroll well into a long turn until its question is off-screen. That turn's
   mark should remain current.
3. While output streams, scroll up. Existing text should retain its screen
   position within a few pixels, and Back to bottom should appear.
4. Use Back to bottom or scroll to the end. Following should resume. Scroll up
   again and confirm following stays paused.
5. Test columns on both sides of 720px, reading areas on both sides of 200px,
   and touch input. Hidden-rail layouts must still scroll normally.
6. Expand a tool row, resize the composer, and open/close the workspace while
   reading history. The text under the reader should stay in place.
7. With roughly 100 turns, check fixed mark spacing, rail scrolling and keyboard
   access to every mark.
8. Fork a conversation and verify copied turns have marks and previews.
9. Switch to Trajectory and back. Verify the rail is absent in Trajectory and
   Chat retains the reading position.
10. Repeat keyboard navigation in both languages, both themes and reduced motion.

Implementation and tests live in
[`JobDetail.tsx`](../apps/frontend/src/components/features/agents/JobDetail.tsx),
[`TurnNavigator.tsx`](../apps/frontend/src/components/features/agents/TurnNavigator.tsx),
[`conversationTurnNavigation.ts`](../apps/frontend/src/components/features/agents/conversationTurnNavigation.ts)
and [`useConversationScroll.ts`](../apps/frontend/src/components/features/agents/useConversationScroll.ts).
