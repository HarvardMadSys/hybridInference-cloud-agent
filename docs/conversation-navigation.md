# Conversation navigation

A long conversation is a stack of turns — what you asked, what the agent did
about it, what it answered. The rail beside the transcript is the index into
that stack: one mark per turn, in the order the turns appear.

This document describes the behaviour as shipped. The design, its alternatives
and the measurements behind the sizes are in
[the turn navigation design](specs/2026-09-11-conversation-turn-navigation-design.zh.md).

## The rail

Marks appear in the right-hand gutter of the Chat column, not at the edge of the
browser window, so opening the workspace does not move them away from the
conversation. The rail is hidden while a conversation has fewer than two turns:
a ladder with one rung is not navigation.

A mark is a quiet hairline until it matters. The turn you are reading is the
longest and darkest mark; the mark under the pointer or keyboard focus widens a
little; everything else stays short. Mark length carries interaction state only
— never message length, duration, token spend or progress.

Long conversations keep a fixed gap between marks and scroll inside the rail
instead of compressing. The faded ends of the rail say which way there is more.
Scrolling the rail never scrolls the transcript.

The header's **Turns** button opens the same list as rows, one line per turn,
tall enough to hit on a phone. It is the primary affordance on narrow columns
and on touch screens, and it stays available on wide ones.

## Previews

Pointing at a mark, or moving keyboard focus to it, shows that turn: the
question, the last reply the transcript shows for it, and its state when a state
is known. Nothing moves while the preview is open — hovering a mark does not
scroll the transcript, does not request anything, and does not change which turn
is current. Moving the pointer onto the preview keeps it open; pressing Escape
or leaving the rail closes it.

The question is the words you actually sent, not the conversation's title. The
reply is the last thing that turn said, and only once the turn has reached a
terminal state: what a running turn has produced so far is not what it will say,
and a card that rewrites itself under the pointer is worse than one that waits.

Nothing hidden is ever previewed. Chain-of-thought, tool arguments, tool output
and run logs stay out; the preview repeats text you could already read in the
transcript, capped at fifty characters for the question and a hundred and twenty
for the reply.

A turn copied by Fork has no stored event log. Its preview and its transcript
come from the durable messages the fork copied, so it reads the way it did in
the conversation it came from.

## Jumping

Clicking a mark — or choosing a row in the directory, or pressing Enter on a
focused mark — moves the transcript so that turn's first question sits just
below the top of the reading area. A turn with no question yet lands on whatever
it does show.

Jumping is navigation inside the page. It does not change the URL, does not
switch the selected run attempt, does not open the workspace, and never calls
Fork, Rewind or Cancel. Arrow keys walk between marks and Home/End jump to the
ends of the rail without scrolling the transcript; only Enter, Space or a click
commits.

A jump in flight is cancelled by the next one, by your own scrolling, and by
leaving the conversation. If a turn cannot be located at all, the reading
position is left where it was and a short message says so, rather than the
transcript silently landing somewhere you did not ask for.

**Back to bottom** is a different control from the newest mark. The newest mark
goes to the beginning of the newest turn; Back to bottom goes to the end of the
transcript and resumes following it.

## Reading position

The transcript grows while you read it, and where it grows decides whether it
moves you.

- **Following** — the transcript stays at the end as output arrives. This is how
  the conversation opens, and how it behaves after Back to bottom, after you
  scroll down to the live end yourself, and after a follow-up you sent is
  accepted.
- **Reading** — the transcript holds its place. Scrolling up, focusing a turn
  through the rail, or switching to an older run attempt puts it here, and new
  output neither pulls you to the bottom nor shifts the text under your eyes.
- While you are away from the end, a **Back to bottom** control appears; when
  something arrived while you were reading, it says **New content** instead. It
  counts nothing: the number would be stale before it was read.

Whether you are at the end is a measurement, not a settings toggle. Returning to
the end is always something you do.

## Accessibility

The rail is a named navigation region of real buttons. Each mark is labelled
with the turn it addresses, the current one is marked `aria-current`, and the
preview is associated with its mark as a description. Arrow keys move between
marks, Home and End go to the first and last, and Enter or Space jumps. Only one
mark is in the tab order at a time, so a two-hundred-turn conversation does not
take two hundred Tab presses to pass. Escape closes a preview or the directory
and returns focus to the control that opened it.

A jump is announced once, politely, when you ask for it. Nothing is announced
while output streams.

Reduced-motion preferences are honoured, and both shipped languages have full
strings for the rail, the directory, the previews and the states.

## What the rail does not do

- It does not change the conversation's data. There is no new endpoint, no new
  subscription, and hovering or jumping issues no request.
- It does not list turns the page is not showing. Opening an older turn's link
  lists the turns that are on screen and stops there, rather than offering a
  jump to a turn with no body to land on.
- It does not persist anything. The reading position lives in the page, and
  reloading starts the conversation at its end, like a fresh open.

## Verifying it in a browser

Real layout is the part jsdom cannot supply: the unit tests pin the arithmetic
(`useConversationScroll.test.ts`), the controller's decisions against an injected
box model (`useConversationScroll.dom.test.tsx`), the pure data rules
(`conversationTurnNavigation.test.ts`) and rendering, focus and the directory
(`TurnNavigator.test.tsx`, `JobDetail.test.tsx`). What none of them can prove is
what a browser actually lays out — Markdown reflow, the sticky composer's real
height, ResizeObserver timing, pointer and touch input, and the landing distance
in pixels. Check the following in a real browser, against a conversation with at
least a few turns:

1. **Landing.** Click the middle mark: the turn's question sits about 24px below
   the top of the reading area, and the mark you clicked is the current one.
2. **Long turn.** Scroll deep into a long turn so its own question is off screen:
   its mark stays current.
3. **Streaming.** With a run producing output, scroll up. Existing text keeps its
   screen position (within a few pixels) while new output arrives, and the
   Back to bottom control appears.
4. **Resuming.** Scroll back down to the end, or press Back to bottom, and confirm
   following resumes — then scroll up again and confirm it does not resume on its
   own.
5. **Both widths.** At a Chat column wider than 720px the rail is visible and the
   text clears it; narrower, or with the workspace open on a small screen, the
   rail gives way to the Turns button and the panel remains usable.
6. **Rearrangement.** Expand and collapse a tool row, change the composer's
   height, and open or close the workspace while reading history: the text you
   were reading stays put.
7. **A hundred turns.** With a long conversation, marks keep their spacing, the
   rail scrolls inside itself, the keyboard reaches every mark, and the directory
   opens at the current turn.
8. **Forked history.** In a conversation copied by Fork, the copied turns have
   marks and previews even though they have no stored events.
9. **Views.** Trajectory shows no rail; returning to Chat restores the reading
   position.
10. **Keyboard and languages.** Complete the flow with the keyboard only, in both
    light and dark themes, in English and Simplified Chinese, with reduced motion
    enabled.
