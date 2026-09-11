// Tests for the conversation's turn directory.
//
// What matters here is that the ladder and the transcript cannot disagree: one
// mark per rendered turn, in order, previewing the words that turn actually
// shows — and never a mark for a turn the page does not draw.

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  conversationNavigationScope,
  describeConversationTurns,
  describeCurrentTurn,
  describeHistoryTurns,
  eventBlockKey,
  messageBlockKey,
  previewText,
  PROMPT_PREVIEW_LIMIT,
  sameNavigationEntries,
  toNavigationEntry,
} from './conversationTurnNavigation';
import type { AgentEvent, AgentHistoryTurn, AgentJob, AgentThreadMessage } from './types';

function makeJob(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'Current task',
    prompt: 'Current task',
    state: 'running',
    repo: 'owner/repository',
    baseSha: 'abcdef1',
    branch: 'agent/ajob_1',
    runtime: 'claude-code',
    model: 'qwen-test',
    spentUsd: 0,
    timeoutLabel: '',
    networkSetup: '',
    networkAgent: '',
    sandbox: '',
    attempts: [{ no: 1, status: 'live' }],
    events: [],
    diffFiles: [],
    gates: [],
    usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: null },
    egressDenials: 0,
    ...overrides,
  };
}

function message(
  id: number,
  role: 'user' | 'assistant',
  content: string,
  jobId: string,
): AgentThreadMessage {
  return { id, role, content, jobId, createdAt: null };
}

function historyTurn(
  jobId: string,
  turnNo: number,
  messages: AgentThreadMessage[],
  events: AgentEvent[],
  forkable = false,
  state?: AgentHistoryTurn['state'],
): AgentHistoryTurn {
  return { jobId, turnNo, ...(state === undefined ? {} : { state }), forkable, messages, events };
}

describe('previewText', () => {
  it('collapses whitespace and trims', () => {
    expect(previewText('  Fix the\n\n  timeout   regression  ', PROMPT_PREVIEW_LIMIT)).toBe(
      'Fix the timeout regression',
    );
  });

  it('keeps a string that already fits', () => {
    expect(previewText('Two rounds', PROMPT_PREVIEW_LIMIT)).toBe('Two rounds');
  });

  it('counts the ellipsis inside the budget', () => {
    const clipped = previewText('a'.repeat(120), PROMPT_PREVIEW_LIMIT);
    expect(clipped).toBe(`${'a'.repeat(PROMPT_PREVIEW_LIMIT - 1)}…`);
    expect([...clipped]).toHaveLength(PROMPT_PREVIEW_LIMIT);
  });

  it('never cuts a grapheme cluster in half', () => {
    // A ZWJ family is one character to a reader and four code points to a
    // string; slicing by index would leave half of a person on the card.
    const family = '\u{1F468}\u200D\u{1F469}\u200D\u{1F467}\u200D\u{1F466}';
    expect(previewText(family.repeat(60), PROMPT_PREVIEW_LIMIT)).toBe(
      `${family.repeat(PROMPT_PREVIEW_LIMIT - 1)}…`,
    );
    // A combining acute accent is part of the character it sits on.
    expect(previewText('e\u0301'.repeat(60), 10)).toBe(`${'e\u0301'.repeat(9)}…`);
  });

  it('keeps a long non-ASCII reply that fits, and clips the one that does not', () => {
    // The budget boundary on the path that has to cluster: exactly at the
    // limit is not clipped, one character past it is.
    const fifty = '中'.repeat(PROMPT_PREVIEW_LIMIT);
    expect(previewText(fifty, PROMPT_PREVIEW_LIMIT)).toBe(fifty);
    expect(previewText(`${fifty}中`, PROMPT_PREVIEW_LIMIT)).toBe(
      `${'中'.repeat(PROMPT_PREVIEW_LIMIT - 1)}…`,
    );
  });

  it('collapses whitespace in a long reply without spending the whole budget on it', () => {
    const reply = `${'中'.repeat(20)}${' '.repeat(500)}${'文'.repeat(20)}`;
    expect(previewText(reply, PROMPT_PREVIEW_LIMIT)).toBe(`${'中'.repeat(20)} ${'文'.repeat(20)}`);
  });

  it('returns nothing for whitespace', () => {
    expect(previewText('   \n  ', PROMPT_PREVIEW_LIMIT)).toBe('');
  });
});

describe('describeConversationTurns', () => {
  const earlier = historyTurn(
    'ajob_0',
    2,
    [message(1, 'user', 'Earlier question', 'ajob_0')],
    [
      { kind: 'message', text: 'Earlier answer', attemptNo: 1, sourceEventId: 4 },
      { kind: 'tool_use', tool: 'Bash', detail: 'pytest', attemptNo: 1, sourceEventId: 5 },
    ],
  );

  it('lists stored turns first and the current turn last', () => {
    const job = makeJob({ historyTurns: [earlier], turnNo: 3 });
    const turns = describeConversationTurns(job);
    expect(turns.map((turn) => turn.key)).toEqual(['ajob_0', 'ajob_1']);
    expect(turns.map((turn) => turn.isCurrent)).toEqual([false, true]);
    expect(turns.map((turn) => turn.displayNo)).toEqual([2, 3]);
  });

  it('renders one turn per job, however many attempts it ran', () => {
    const job = makeJob({
      historyTurns: [earlier],
      attempts: [
        { no: 1, status: 'superseded' },
        { no: 2, status: 'live' },
      ],
      currentAttemptNo: 2,
      events: [
        { kind: 'message', text: 'first try', attemptNo: 1, sourceEventId: 9 },
        { kind: 'message', text: 'second try', attemptNo: 2, sourceEventId: 10 },
      ],
    });
    const entries = describeConversationTurns(job).map((turn) =>
      toNavigationEntry(turn, { attemptNo: 2, currentAttemptNo: 2 }),
    );
    expect(entries).toHaveLength(2);
    expect(entries[1].responsePreview).toBe('');
  });

  it('separates the stored turns from the one being written', () => {
    // The split is what lets a page hold onto the stored replies while a run
    // streams into the newest turn.
    const job = makeJob({ historyTurns: [earlier], turnNo: 3 });
    expect(describeHistoryTurns(job).map((turn) => turn.key)).toEqual(['ajob_0']);
    expect(describeCurrentTurn(job).key).toBe('ajob_1');
    expect(describeHistoryTurns(makeJob())).toEqual([]);
    expect(describeConversationTurns(job).map((turn) => turn.key)).toEqual(['ajob_0', 'ajob_1']);
  });

  it('filters a flat job by the selected attempt too', () => {
    // No turn split does not mean no attempts: the transcript still shows one.
    const job = makeJob({
      state: 'failed',
      events: [
        { kind: 'message', text: 'first try', attemptNo: 1, sourceEventId: 9 },
        { kind: 'message', text: 'second try', attemptNo: 2, sourceEventId: 10 },
      ],
    });
    expect(
      describeCurrentTurn(job, { attemptNo: 1 }).events.map((row) =>
        row.kind === 'message' ? row.text : row.kind,
      ),
    ).toEqual(['first try']);
    expect(describeCurrentTurn(job, { attemptNo: 1 }).lastResponse).toBe('first try');
  });

  it('renders a durable reply even when it carries no prompt of its own', () => {
    // The prompt flag decides whether the separate prompt card stands down; it
    // must not decide whether the turn's own rows are drawn at all.
    const job = makeJob({
      historyTurns: [earlier],
      historyIncludesPrompt: false,
      turnNo: 3,
      prompt: 'Copied prompt',
      threadMessages: [message(9, 'assistant', 'Copied answer', 'ajob_1')],
    });
    const current = describeCurrentTurn(job);
    expect(current.events).toEqual([]);
    expect(current.messages.map((row) => row.content)).toEqual(['Copied answer']);
    expect(current.promptCard).toBe('Copied prompt');
    expect(current.lastResponse).toBe('');
  });

  it('reports a stored turn outcome the thread actually recorded', () => {
    const failed = historyTurn(
      'ajob_0',
      1,
      [message(1, 'user', 'Earlier question', 'ajob_0')],
      [],
      false,
      'failed',
    );
    const entries = describeConversationTurns(makeJob({ historyTurns: [failed] })).map((turn) =>
      toNavigationEntry(turn),
    );
    expect(entries[0].state).toBe('failed');
  });

  it('withholds the reply of a stored turn that is still running', () => {
    // The previous turn can still be running behind a queued one; its mark
    // must not promise an answer it has not finished writing.
    const live = historyTurn(
      'ajob_0',
      1,
      [message(1, 'user', 'Earlier question', 'ajob_0')],
      [{ kind: 'message', text: 'still typing', attemptNo: 1, sourceEventId: 8 }],
      false,
      'running',
    );
    const entries = describeConversationTurns(makeJob({ historyTurns: [live] })).map((turn) =>
      toNavigationEntry(turn),
    );
    expect(entries[0].state).toBe('running');
    expect(entries[0].responsePreview).toBe('');
  });

  it('says nothing about a stored turn whose outcome was never reported', () => {
    const entries = describeConversationTurns(makeJob({ historyTurns: [earlier] })).map((turn) =>
      toNavigationEntry(turn),
    );
    expect(entries[0].state).toBeUndefined();
  });

  it('keeps a forked turn in the durable copy the transcript renders', () => {
    // A fork copies messages without an event log, so the prompt lives in the
    // message list and the separate prompt card stands down.
    const job = makeJob({
      historyTurns: [earlier],
      historyIncludesPrompt: true,
      turnNo: 3,
      prompt: 'Copied prompt',
      threadMessages: [
        message(7, 'user', 'Copied prompt', 'ajob_1'),
        message(8, 'assistant', 'Copied answer', 'ajob_1'),
      ],
    });
    const turns = describeConversationTurns(job);
    const current = turns[1];
    expect(current.promptCard).toBeNull();
    expect(current.prompt).toBe('Copied prompt');
    expect(current.lastResponse).toBe('Copied answer');
    expect(current.events).toEqual([]);
  });

  it('falls back to durable assistant messages when a stored turn has no event messages', () => {
    const copied = historyTurn(
      'ajob_0',
      1,
      [
        message(1, 'user', 'Old question', 'ajob_0'),
        message(2, 'assistant', 'Old answer', 'ajob_0'),
      ],
      [{ kind: 'lifecycle', text: 'started', attemptNo: 1, sourceEventId: 3 }],
    );
    const turn = describeConversationTurns(makeJob({ historyTurns: [copied] }))[0];
    expect(turn.fallbackResponses.map((row) => row.id)).toEqual([2]);
    expect(turn.lastResponse).toBe('Old answer');
  });

  it('prefers the stored reply over the durable copy when both exist', () => {
    const turn = describeConversationTurns(makeJob({ historyTurns: [earlier] }))[0];
    expect(turn.lastResponse).toBe('Earlier answer');
    expect(turn.fallbackResponses).toEqual([]);
  });

  it('withholds a reply while the turn is still running', () => {
    const job = makeJob({
      state: 'running',
      events: [{ kind: 'message', text: 'still typing', attemptNo: 1, sourceEventId: 2 }],
    });
    expect(describeConversationTurns(job)[0].lastResponse).toBe('');
  });

  it('keeps what a failed run produced, and says it failed', () => {
    const job = makeJob({
      state: 'failed',
      events: [{ kind: 'message', text: 'partial answer', attemptNo: 1, sourceEventId: 2 }],
    });
    const entries = describeConversationTurns(job).map((turn) => toNavigationEntry(turn));
    expect(entries[0].responsePreview).toBe('partial answer');
    expect(entries[0].state).toBe('failed');
  });

  it('filters the current preview by the attempt the transcript shows', () => {
    const job = makeJob({
      historyTurns: [earlier],
      state: 'failed',
      currentAttemptNo: 2,
      events: [
        { kind: 'message', text: 'first try', attemptNo: 1, sourceEventId: 9 },
        { kind: 'message', text: 'second try', attemptNo: 2, sourceEventId: 10 },
      ],
    });
    const entries = describeConversationTurns(job, { attemptNo: 1, currentAttemptNo: 2 }).map(
      (turn) => toNavigationEntry(turn, { attemptNo: 1, currentAttemptNo: 2 }),
    );
    expect(entries[1].responsePreview).toBe('first try');
    expect(entries[1].previewAttemptNo).toBe(1);
  });

  it('does not claim an attempt is old without a current attempt to compare', () => {
    const job = makeJob({
      state: 'failed',
      events: [{ kind: 'message', text: 'only try', attemptNo: 1, sourceEventId: 9 }],
    });
    const entries = describeConversationTurns(job, { attemptNo: 1 }).map((turn) =>
      toNavigationEntry(turn, { attemptNo: 1 }),
    );
    expect(entries[0].previewAttemptNo).toBeUndefined();
  });

  it('draws one flat turn for a job with no reconstructed history', () => {
    const job = makeJob({
      threadMessages: [message(1, 'user', 'Only question', 'ajob_1')],
      state: 'needs_review',
      events: [{ kind: 'message', text: 'Only answer', attemptNo: 1, sourceEventId: 2 }],
    });
    const turns = describeConversationTurns(job);
    expect(turns).toHaveLength(1);
    expect(turns[0].promptCard).toBe('Current task');
    expect(turns[0].lastResponse).toBe('Only answer');
  });

  it('keeps a mark for a turn that has nothing to show yet', () => {
    const job = makeJob({ historyTurns: [earlier], state: 'queued', events: [] });
    const entries = describeConversationTurns(job).map((turn) => toNavigationEntry(turn));
    expect(entries).toHaveLength(2);
    expect(entries[1]).toMatchObject({ promptPreview: 'Current task', state: 'queued' });
    expect(entries[1].responsePreview).toBe('');
  });

  it('numbers a turn that never got a server turn number by its position', () => {
    const job = makeJob({ historyTurns: [earlier] });
    const entries = describeConversationTurns(job).map((turn) => toNavigationEntry(turn));
    expect(entries[1].turnNo).toBeUndefined();
    expect(entries[1].displayNo).toBe(2);
  });
});

describe('DOM identity', () => {
  it('names rows by their stored ids, not their position', () => {
    expect(messageBlockKey('ajob_1', 12)).toBe('ajob_1:m12');
    const withId: AgentEvent = { kind: 'message', text: 'a', sourceEventId: 42 };
    const withoutId: AgentEvent = { kind: 'message', text: 'a' };
    expect(eventBlockKey('ajob_1', withId, 3)).toBe('ajob_1:e42');
    expect(eventBlockKey('ajob_1', withId, 4)).toBe('ajob_1:e42');
    // Rows that predate the stored id fall back to the position, which is the
    // only identity they have.
    expect(eventBlockKey('ajob_1', withoutId, 3)).toBe('ajob_1:ei3');
  });

  it('keys the reading position by the conversation, not the newest turn', () => {
    expect(conversationNavigationScope(makeJob({ threadId: 'thread-1' }))).toEqual({
      conversationKey: 'thread-1',
      throughJobId: 'ajob_1',
    });
    expect(conversationNavigationScope(makeJob()).conversationKey).toBe('ajob_1');
  });
});

describe('sameNavigationEntries', () => {
  const entry = (
    key: string,
    promptPreview: string,
    overrides: Partial<ReturnType<typeof toNavigationEntry>> = {},
  ) => ({
    ...toNavigationEntry(describeCurrentTurn(makeJob({ id: key }))),
    key,
    promptPreview,
    ...overrides,
  });

  it('accepts the marks it was already showing', () => {
    const one = [entry('ajob_1', 'Do the thing')];
    const other = [entry('ajob_1', 'Do the thing')];
    expect(one).not.toBe(other);
    expect(sameNavigationEntries(one, other)).toBe(true);
    expect(sameNavigationEntries([], [])).toBe(true);
  });

  it('rejects any mark that would render differently', () => {
    const base = [entry('ajob_1', 'Do the thing')];
    expect(sameNavigationEntries(base, [entry('ajob_1', 'Do another thing')])).toBe(false);
    expect(
      sameNavigationEntries(base, [entry('ajob_1', 'Do the thing', { state: 'failed' })]),
    ).toBe(false);
    expect(
      sameNavigationEntries(base, [entry('ajob_1', 'Do the thing', { previewAttemptNo: 1 })]),
    ).toBe(false);
    expect(sameNavigationEntries(base, [entry('ajob_2', 'Do the thing')])).toBe(false);
    expect(sameNavigationEntries(base, [...base, entry('ajob_2', 'And then')])).toBe(false);
  });

  it('rejects a mark whose reply arrived while its turn was running', () => {
    // The one field that changes without the turn list moving: a running turn
    // settles and its reply becomes previewable.
    const running = [
      toNavigationEntry(describeCurrentTurn(makeJob({ id: 'ajob_1', state: 'running' }))),
    ];
    const settled = [
      toNavigationEntry(
        describeCurrentTurn(
          makeJob({
            id: 'ajob_1',
            state: 'done',
            events: [{ kind: 'message', text: 'the answer', attemptNo: 1, sourceEventId: 3 }],
          }),
        ),
      ),
    ];
    expect(sameNavigationEntries(running, settled)).toBe(false);
  });
});

describe('previewText work is bounded by the budget', () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('stops reading a reply once the budget is full', async () => {
    // A reply is routinely tens of thousands of characters while the preview
    // is the first line of it. Clustering all of it, on every streamed token
    // and every keystroke, is what made a long conversation slow to draw; this
    // pins the bound rather than a wall-clock threshold, so it cannot flake.
    const original = Intl.Segmenter;
    let read = 0;
    class CountingSegmenter {
      segment(text: string) {
        return {
          *[Symbol.iterator]() {
            for (let index = 0; index < text.length; index += 1) {
              read = index + 1;
              yield { segment: text[index], index, input: text };
            }
          },
        };
      }
    }
    Object.defineProperty(Intl, 'Segmenter', {
      value: CountingSegmenter,
      configurable: true,
      writable: true,
    });
    vi.resetModules();

    try {
      const fresh = await import('./conversationTurnNavigation');
      const reply = '这是一个很长的回复。'.repeat(5000);
      read = 0;

      const preview = fresh.previewText(reply, 120);

      expect([...preview]).toHaveLength(120);
      expect(preview.endsWith('…')).toBe(true);
      // One character past the budget is enough to know the ellipsis is honest.
      expect(read).toBeGreaterThanOrEqual(120);
      expect(read).toBeLessThanOrEqual(130);
    } finally {
      Object.defineProperty(Intl, 'Segmenter', {
        value: original,
        configurable: true,
        writable: true,
      });
    }
  });
});
