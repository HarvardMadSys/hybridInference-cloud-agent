// The conversation's turn directory.
//
// One entry per turn the transcript actually renders, in the order it renders
// them. The Chat and its navigation rail both read this derivation, so a turn
// cannot appear in the ladder without a body to scroll to, and the ladder
// cannot disagree with the body about which messages belong to which turn.
//
// Pure on purpose: no DOM, no React, no network. Positioning and reading
// state live in `useConversationScroll`.

import type {
  AgentEvent,
  AgentHistoryTurn,
  AgentJob,
  AgentJobState,
  AgentThreadMessage,
} from './types';

/**
 * Preview budgets, sized to the rail card's clamps — one prompt line and up to
 * three response lines. Anything past a budget is invisible, and copying whole
 * transcripts into navigation state would grow with the conversation on every
 * structural update.
 */
export const PROMPT_PREVIEW_LIMIT = 50;
export const RESPONSE_PREVIEW_LIMIT = 120;

/** One turn as the transcript renders it. */
export interface ConversationTurnDescription {
  /** Stable identity inside this conversation: the job that produced the turn. */
  key: string;
  jobId: string;
  /** Number the transcript shows beside this turn. */
  displayNo: number;
  /** Server turn number, absent on rows that predate it. */
  turnNo?: number;
  /** Durable messages the transcript renders for this turn, in order. */
  messages: AgentThreadMessage[];
  /** Activity rows the transcript renders for this turn, in order. */
  events: AgentEvent[];
  /**
   * Response rows rendered from the durable copy because the stored event log
   * carries no message for this turn — how a forked turn still shows what it
   * said.
   */
  fallbackResponses: AgentThreadMessage[];
  /**
   * The turn's prompt when the transcript renders it as its own card rather
   * than as one of `messages`; null when the prompt is not drawn separately.
   */
  promptCard: string | null;
  /**
   * Whether this turn's own rows are a valid starting point for a fork. Only a
   * stored turn can carry it; the newest turn answers the question elsewhere.
   */
  forkable: boolean;
  /** Tool calls the transcript renders for this turn, for its closing ruler. */
  toolCalls: number;
  /** The prompt text a preview should show, whatever owns its rendering. */
  prompt: string;
  /**
   * The last reply the transcript renders for this turn, or empty while there
   * is no reply to show yet. Raw text: the preview budgets it.
   */
  lastResponse: string;
  state?: AgentJobState;
  isCurrent: boolean;
}

/** One mark on the navigation rail. */
export interface ConversationTurnNavigationEntry {
  key: string;
  jobId: string;
  displayNo: number;
  turnNo?: number;
  promptPreview: string;
  /** Empty while the turn has no terminal reply to show. */
  responsePreview: string;
  state?: AgentJobState;
  /**
   * Attempt the visible reply belongs to, set only when the transcript is
   * showing an older attempt than the newest one.
   */
  previewAttemptNo?: number;
}

/** Which conversation, and how much of it, the page is currently showing. */
export interface ConversationNavigationScope {
  /** Identity that outlives the individual turn: a follow-up keeps it. */
  conversationKey: string;
  /** Newest turn the page renders; older links stop the ladder here. */
  throughJobId: string;
}

export interface ConversationTurnOptions {
  /**
   * Display attempt the transcript renders, or 0 for "every attempt". The
   * preview follows the same filter, so the card cannot describe a run the
   * reader is not looking at.
   */
  attemptNo?: number;
  /**
   * Display number of the newest attempt, when the server reported one. A
   * preview is only labelled with an attempt when this proves the shown one is
   * not it; an unknown current attempt is not evidence of an old one.
   */
  currentAttemptNo?: number;
}

/**
 * Turns whose reply is already settled.
 *
 * `forkable` is deliberately not consulted: it also requires a settled native
 * ordinal and a compatible host, and a turn a reader can read is finished
 * whether or not it can be forked.
 */
function isTerminal(state: AgentJobState): boolean {
  return state !== 'queued' && state !== 'running';
}

const GRAPHEME_SEGMENTER =
  typeof Intl === 'undefined' || typeof Intl.Segmenter !== 'function'
    ? null
    : new Intl.Segmenter(undefined, { granularity: 'grapheme' });

const WHITESPACE = /\s/;

/**
 * The text's grapheme clusters, lazily.
 *
 * Lazily is the point: a caller that stops reading after a budget must not pay
 * for the clusters behind it. Falling back to code points still avoids
 * splitting a surrogate pair.
 */
function* textCharacters(text: string): Generator<string> {
  if (GRAPHEME_SEGMENTER === null) {
    yield* text;
    return;
  }
  for (const { segment } of GRAPHEME_SEGMENTER.segment(text)) yield segment;
}

/** One space per run of whitespace, and none at either end. */
function collapseWhitespace(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

/**
 * Collapse whitespace and cap at `limit` characters with a trailing ellipsis.
 *
 * Rendered text, not Markdown: the card shows the words the reader already
 * sees, and a stray `**` or backtick costs two of fifty characters.
 *
 * Reads only as far as the budget can reach. A stored reply is routinely tens
 * of thousands of characters — a Chinese one is a cluster per character — and
 * clustering all of them to show the first hundred and twenty cost tens of
 * milliseconds per derivation, on every streamed token and every keystroke in
 * the composer.
 */
export function previewText(text: string, limit: number): string {
  const budget = Math.max(1, limit);
  // Already within budget, which is the common case for a prompt: one pass,
  // no clustering at all.
  if (text.length <= budget) return collapseWhitespace(text);

  let preview = '';
  /** Where the budget's last character ended, for the clipped case. */
  let cut = 0;
  let counted = 0;
  let space = false;
  let clipped = false;

  for (const character of textCharacters(text)) {
    if (WHITESPACE.test(character)) {
      // Leading whitespace is dropped; a run inside becomes one space.
      if (counted > 0) space = true;
      continue;
    }
    if (space) {
      if (counted >= budget) {
        clipped = true;
        break;
      }
      preview += ' ';
      counted += 1;
      space = false;
      if (counted === budget - 1) cut = preview.length;
    }
    if (counted >= budget) {
      // One character past what the card can show: enough to know the ellipsis
      // is honest, and no reason to read the rest of the reply.
      clipped = true;
      break;
    }
    preview += character;
    counted += 1;
    if (counted === budget - 1) cut = preview.length;
  }

  // The ellipsis is part of the budget, so a clipped preview can never be
  // longer than the whole one it replaces.
  return clipped ? `${preview.slice(0, cut).trimEnd()}…` : preview;
}

/** The last non-empty message row among `events`, or empty. */
function lastEventMessage(events: readonly AgentEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind === 'message' && event.text.trim() !== '') return event.text;
  }
  return '';
}

/** The last non-empty assistant row among `messages`, or empty. */
function lastAssistantMessage(messages: readonly AgentThreadMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === 'assistant' && message.content.trim() !== '') return message.content;
  }
  return '';
}

/** Whether an event row survives the selected attempt's filter. */
function visibleForAttempt(event: AgentEvent, attemptNo: number): boolean {
  return event.attemptNo === undefined || attemptNo === 0 || event.attemptNo === attemptNo;
}

// Retained-session bookkeeping is useful in Trajectory, but a follow-up in
// Chat should read as a continuation. Keep the original job events intact:
// workspace controls still need their readiness and finalization phases.
const FOLLOW_UP_INTERNAL_PHASES = new Set([
  'started',
  'init',
  'system',
  'checked_out',
  'workspace_reused',
  'context_restored',
  'setup',
  'workspace_preparing',
  'workspace_ready',
  'workspace_finalizing',
]);

function chatEvents(events: AgentEvent[], turnNo: number): AgentEvent[] {
  if (turnNo <= 1) return events;
  return events.filter(
    (event) => event.kind !== 'lifecycle' || !FOLLOW_UP_INTERNAL_PHASES.has(event.text),
  );
}

/** Tool rows a rendered event list contains. */
function toolCallsIn(events: readonly AgentEvent[]): number {
  return events.filter((event) => event.kind === 'tool_use').length;
}

/** The first user row's text, or empty. */
function firstUserMessage(messages: readonly AgentThreadMessage[]): string {
  return messages.find((message) => message.role === 'user')?.content ?? '';
}

function historyTurnDescription(
  turn: AgentHistoryTurn,
  displayNo: number,
): ConversationTurnDescription {
  // Exactly what the transcript does: a turn with stored message rows renders
  // those, and only a turn without them falls back to its durable copy.
  const hasEventMessages = turn.events.some((event) => event.kind === 'message');
  const fallbackResponses = hasEventMessages
    ? []
    : turn.messages.filter((message) => message.role === 'assistant');
  // A stored turn is usually finished, and its reply is what it said. The one
  // that is not — the previous turn can still be running behind a queued one —
  // gets the same treatment as the newest turn: what it has produced so far is
  // not what it will say.
  const unfinished = turn.state !== undefined && !isTerminal(turn.state);
  return {
    key: turn.jobId,
    jobId: turn.jobId,
    displayNo,
    turnNo: turn.turnNo,
    ...(turn.state === undefined ? {} : { state: turn.state }),
    messages: turn.messages,
    events: chatEvents(turn.events, displayNo),
    fallbackResponses,
    promptCard: null,
    forkable: turn.forkable,
    toolCalls: toolCallsIn(turn.events),
    prompt: firstUserMessage(turn.messages),
    lastResponse: unfinished
      ? ''
      : hasEventMessages
        ? lastEventMessage(turn.events)
        : lastAssistantMessage(fallbackResponses),
    isCurrent: false,
  };
}

/**
 * The conversation's stored turns, oldest first.
 *
 * These are immutable once written, which is what lets a page memoize them: a
 * run appending events to its newest turn does not rescan the replies behind
 * it. The page shows the conversation's prefix through the selected job, so a
 * link into turn four does not list turn eight — nothing would be there to
 * scroll to.
 */
export function describeHistoryTurns(job: AgentJob): ConversationTurnDescription[] {
  const historyTurns = job.historyTurns;
  if (historyTurns === undefined) return [];
  return historyTurns.map((turn, index) =>
    // A stored turn number leads; the position is the honest fallback for rows
    // old enough to predate it.
    historyTurnDescription(turn, turn.turnNo > 0 ? turn.turnNo : index + 1),
  );
}

/**
 * The newest turn the page renders.
 *
 * Rebuilt whenever it is asked for, because this is the turn a run is writing:
 * its rows are what the transcript draws right now. Only its preview is held
 * back until the turn settles.
 */
export function describeCurrentTurn(
  job: AgentJob,
  options: ConversationTurnOptions = {},
): ConversationTurnDescription {
  const attemptNo = options.attemptNo ?? 0;
  const events = chatEvents(
    job.events.filter((event) => visibleForAttempt(event, attemptNo)),
    job.turnNo ?? (job.historyTurns?.length ?? 0) + 1,
  );

  if (job.historyTurns === undefined) {
    // Fixture, list and standalone shape: the job carries one flat message list
    // and no turn split, so the transcript draws all of it as a single turn.
    const messages = job.threadMessages ?? [];
    return {
      key: job.id,
      jobId: job.id,
      displayNo: job.turnNo ?? 1,
      ...(job.turnNo === undefined ? {} : { turnNo: job.turnNo }),
      messages,
      events,
      fallbackResponses: [],
      promptCard: job.prompt || job.title,
      forkable: false,
      toolCalls: toolCallsIn(events),
      prompt: job.prompt || job.title,
      lastResponse: isTerminal(job.state)
        ? lastEventMessage(events) || lastAssistantMessage(messages)
        : '',
      state: job.state,
      isCurrent: true,
    };
  }

  // A forked turn has no event log, so its durable copy is the transcript and
  // its prompt is already among those rows; the separate card stands down.
  const promptFromHistory = job.historyIncludesPrompt === true;
  const messages = (job.threadMessages ?? []).filter((message) => message.jobId === job.id);
  const promptCard = promptFromHistory ? null : job.prompt || job.title;
  // A turn still running has no reply to preview: what has been streamed so far
  // is not what the turn will say, and the card would rewrite itself under the
  // reader's pointer on every delta.
  const lastResponse = promptFromHistory
    ? lastAssistantMessage(messages) || lastEventMessage(events)
    : isTerminal(job.state)
      ? lastEventMessage(events)
      : '';

  return {
    key: job.id,
    jobId: job.id,
    displayNo: job.turnNo ?? (job.historyTurns?.length ?? 0) + 1,
    ...(job.turnNo === undefined ? {} : { turnNo: job.turnNo }),
    messages,
    events,
    fallbackResponses: [],
    promptCard,
    forkable: false,
    toolCalls: toolCallsIn(events),
    prompt: promptCard ?? firstUserMessage(messages),
    lastResponse,
    state: job.state,
    isCurrent: true,
  };
}

/** Every turn the transcript renders, oldest first. */
export function describeConversationTurns(
  job: AgentJob,
  options: ConversationTurnOptions = {},
): ConversationTurnDescription[] {
  return [...describeHistoryTurns(job), describeCurrentTurn(job, options)];
}

/** Project a described turn onto the rail. */
export function toNavigationEntry(
  turn: ConversationTurnDescription,
  options: ConversationTurnOptions = {},
): ConversationTurnNavigationEntry {
  const attemptNo = options.attemptNo ?? 0;
  const showingOldAttempt =
    turn.isCurrent &&
    attemptNo > 0 &&
    options.currentAttemptNo !== undefined &&
    attemptNo !== options.currentAttemptNo;
  return {
    key: turn.key,
    jobId: turn.jobId,
    displayNo: turn.displayNo,
    ...(turn.turnNo === undefined ? {} : { turnNo: turn.turnNo }),
    promptPreview: previewText(turn.prompt, PROMPT_PREVIEW_LIMIT),
    responsePreview: previewText(turn.lastResponse, RESPONSE_PREVIEW_LIMIT),
    ...(turn.state === undefined ? {} : { state: turn.state }),
    ...(showingOldAttempt ? { previewAttemptNo: attemptNo } : {}),
  };
}

/** Build the rail's entries for the turns a page currently renders. */
export function buildConversationTurnNavigation(
  job: AgentJob,
  options: ConversationTurnOptions = {},
): ConversationTurnNavigationEntry[] {
  return describeConversationTurns(job, options).map((turn) => toNavigationEntry(turn, options));
}

/**
 * Whether two entry lists would render the same marks.
 *
 * A page memoizes the rail on this. It re-renders and re-derives on every
 * streamed event, while the rail draws two host elements per turn, so a new
 * array identity would rebuild hundreds of marks for a delta that only ever
 * reaches the newest turn's body. Comparing the marks that were just derived is
 * cheaper than rendering them, and — unlike a digest — it cannot serve a stale
 * mark: there is no key that has to predict every field a mark depends on.
 */
export function sameNavigationEntries(
  left: readonly ConversationTurnNavigationEntry[],
  right: readonly ConversationTurnNavigationEntry[],
): boolean {
  if (left === right) return true;
  if (left.length !== right.length) return false;
  for (let index = 0; index < left.length; index += 1) {
    const one = left[index];
    const other = right[index];
    if (
      one.key !== other.key ||
      one.displayNo !== other.displayNo ||
      one.turnNo !== other.turnNo ||
      one.promptPreview !== other.promptPreview ||
      one.responsePreview !== other.responsePreview ||
      one.state !== other.state ||
      one.previewAttemptNo !== other.previewAttemptNo
    ) {
      return false;
    }
  }
  return true;
}

/** The identity a scroll position belongs to. */
export function conversationNavigationScope(job: AgentJob): ConversationNavigationScope {
  return {
    // A follow-up keeps the thread id, and with it every other turn's DOM; the
    // reading position belongs to the conversation, not to the newest job.
    conversationKey: job.threadId ?? job.id,
    throughJobId: job.id,
  };
}

/** DOM identity for one durable message row. */
export function messageBlockKey(jobId: string, messageId: number): string {
  return `${jobId}:m${messageId}`;
}

/**
 * DOM identity for one activity row.
 *
 * The stored event id when the row has one — folding a tool result into its
 * call keeps that identity — and the row's position only for rows that never
 * had one.
 */
export function eventBlockKey(jobId: string, event: AgentEvent, index: number): string {
  return `${jobId}:e${event.sourceEventId ?? `i${index}`}`;
}
